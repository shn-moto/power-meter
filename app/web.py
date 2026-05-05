import asyncio
import base64
import json
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import tinytuya

from app.device_registry import DEVICE_KIND_LABELS, lookup_device_local_ip
from config import AppConfig, load_app_config, load_cloud_config
from app.storage import (
    DeviceSample,
    apply_migrations,
    close_connection_pool,
    delete_managed_device,
    get_control_device,
    get_device_capabilities,
    get_device_context_and_stats,
    get_dashboard_summary,
    get_device_row,
    get_device_rows,
    get_recent_raw_dps_samples,
    get_device_stats,
    get_latest_sample,
    get_sample_age_seconds,
    get_sample_status,
    get_polling_devices,
    init_connection_pool,
    pick_bucket,
    save_sample,
    sync_device_profiles_from_disk,
    update_device_summary_config,
)
from app.tuya_service import build_sample, request_dps_by_index


BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["static_asset_version"] = "20260505-13"

DEVICE_IMAGE_EXTENSIONS = (".png", ".webp", ".jpg", ".jpeg", ".svg")
AGGREGATE_CACHE_TTL_SECONDS = 5.0
SENSOR_CLOUD_STATUS_CACHE_SECONDS = 60.0
SENSOR_CLOUD_OK_SECONDS = 90.0
SENSOR_CLOUD_WARNING_SECONDS = 300.0

TUYA_CATEGORY_LABELS = {
    "cz": "Розетка",
    "dlq": "Автомат / выключатель нагрузки",
    "wsdcg": "Датчик температуры и влажности",
}

RUSSIAN_MONTHS = {
    1: "Январь",
    2: "Февраль",
    3: "Март",
    4: "Апрель",
    5: "Май",
    6: "Июнь",
    7: "Июль",
    8: "Август",
    9: "Сентябрь",
    10: "Октябрь",
    11: "Ноябрь",
    12: "Декабрь",
}


def format_tuya_category(category_code: Any) -> str:
    code = str(category_code or "").strip().lower()
    if not code:
        return "не указана"
    label = TUYA_CATEGORY_LABELS.get(code)
    if not label:
        return code
    return f"{code} · {label}"


templates.env.globals["format_tuya_category"] = format_tuya_category

DPS_LABELS = {
    "switch": "Питание",
    "countdown_1": "Таймер отключения",
    "cur_current": "Ток",
    "cur_power": "Мощность",
    "cur_voltage": "Напряжение",
    "add_ele": "Энергия",
    "total_forward_energy": "Потребление",
    "total_reverse_energy": "Возврат энергии",
    "va_temperature": "Температура",
    "temp_current": "Температура",
    "humidity_value": "Влажность",
    "va_battery": "Батарея",
    "temp_unit_convert": "Единицы температуры",
}

UNIT_LABELS = {
    "V": "В",
    "W": "Вт",
    "mA": "мА",
    "A": "А",
    "s": "с",
    "秒": "с",
}

FUNCTION_LABELS = {
    "switch": ("Питание", "Включение и выключение устройства"),
    "countdown_1": ("Таймер", "Отложенное отключение по таймеру"),
    "bright_value": ("Яркость", "Регулировка яркости"),
    "temp_value": ("Цветовая температура", "Настройка теплоты света"),
    "colour_data": ("Цвет", "Выбор цвета освещения"),
    "colour_data_v2": ("Цвет", "Выбор цвета освещения"),
    "scene_data": ("Сцены", "Переключение световых сцен"),
    "work_mode": ("Режим", "Переключение режимов работы"),
}

SUPPORTED_CONTROL_TYPES = {
    "switch": "toggle",
    "countdown_1": "timer",
}


class ConnectDevicePayload(BaseModel):
    device_id: str


class DeviceSummaryConfigPayload(BaseModel):
    total_power_dps_key: str | None = None
    visualized_codes: list[str] = []
    power_type: str = "total"


class DeviceFunctionPayload(BaseModel):
    value: Any


def _get_timezone(config: AppConfig) -> ZoneInfo:
    try:
        return ZoneInfo(config.timezone)
    except ZoneInfoNotFoundError:
        return timezone.utc


def _month_window(config: AppConfig) -> tuple[datetime, datetime]:
    now = datetime.now(_get_timezone(config))
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return month_start, now


def _format_month_label(value: datetime) -> str:
    return f"{RUSSIAN_MONTHS[value.month]} {value.year}"


def _resolve_device_image_url_by_key(image_key: str | None, directory_name: str) -> str | None:
    if not image_key:
        return None

    safe_key = Path(image_key).name
    image_directory = BASE_DIR / "static" / directory_name
    for extension in DEVICE_IMAGE_EXTENSIONS:
        candidate = image_directory / f"{safe_key}{extension}"
        if candidate.exists():
            return f"/static/{directory_name}/{safe_key}{extension}"
    return None


def _decorate_device_media(device: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(device)
    device_id = str(enriched.get("device_id") or "").strip() or None
    enriched["image_url"] = _resolve_device_image_url_by_key(device_id, "images")
    return enriched


def _decorate_devices_media(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_decorate_device_media(device) for device in devices]


def _format_decimal(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0 с"

    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if secs and not hours:
        parts.append(f"{secs} с")
    return " ".join(parts) or "0 с"


def _normalize_power_type(value: str | None) -> str:
    power_type = str(value or "").strip().lower() or "total"
    if power_type not in {"total", "current"}:
        raise HTTPException(status_code=400, detail="Неверный тип мощности")
    return power_type


def _normalize_unit_token(unit: str | None) -> str:
    return str(unit or "").strip().lower().replace(" ", "")


def _find_selected_power_metadata(
    capabilities: list[dict[str, Any]],
    dp_key: str,
) -> dict[str, Any] | None:
    preferred: dict[str, Any] | None = None
    fallback: dict[str, Any] | None = None

    for capability in capabilities:
        if str(capability.get("dp_id") or "") != dp_key:
            continue
        values_json = capability.get("values_json") or {}
        candidate = {
            "code": str(capability.get("capability_code") or ""),
            "name": str(capability.get("capability_name") or capability.get("capability_code") or ""),
            "value_type": str(capability.get("value_type") or ""),
            "unit": str(values_json.get("unit") or ""),
        }
        if capability.get("capability_source") == "status":
            preferred = candidate
            break
        fallback = candidate

    return preferred or fallback


def _looks_like_phase_measurement_packet(raw_value: Any) -> bool:
    if not isinstance(raw_value, str) or not raw_value:
        return False
    try:
        payload = base64.b64decode(raw_value)
    except Exception:
        return False
    return len(payload) in {8, 10}


def _validate_selected_power_metadata(
    metadata: dict[str, Any] | None,
    power_type: str,
    raw_value: Any,
) -> None:
    if not metadata:
        raise HTTPException(status_code=400, detail="Не найдены метаданные выбранного DPS")

    value_type = str(metadata.get("value_type") or "").strip().lower()
    code = str(metadata.get("code") or "").strip().lower()
    name = str(metadata.get("name") or "").strip().lower()
    unit = _normalize_unit_token(metadata.get("unit"))
    is_numeric = value_type in {"integer", "value", "number", ""}
    is_energy_unit = any(token in unit for token in ("wh", "w·h", "kwh", "kw·h"))
    is_power_unit = unit in {"w", "kw", "mw"}

    if power_type == "total":
        if is_numeric and is_energy_unit:
            return
        raise HTTPException(status_code=400, detail="Для накопленного режима нужен числовой DPS с единицей энергии")

    if _looks_like_phase_measurement_packet(raw_value):
        return

    if is_numeric and (is_power_unit or "power" in code or "power" in name or "мощ" in name):
        return

    raise HTTPException(status_code=400, detail="Для мгновенного режима нужен DPS мощности или raw phase packet")


def _validate_lan_power_dps(control_device: Any, dp_key: str, *, power_type: str) -> None:
    if not dp_key or not str(dp_key).isdigit():
        raise HTTPException(status_code=400, detail="Не задан DPS код мощности")
    if not control_device or not control_device.ip_address or not control_device.local_key:
        raise HTTPException(status_code=400, detail="Устройство не готово к LAN-проверке")

    try:
        payload, _ = request_dps_by_index(
            device_id=control_device.device_id,
            ip_address=control_device.ip_address,
            local_key=control_device.local_key,
            dps_indices=[int(dp_key)],
            version=control_device.version,
            dev_type="default",
            timeout=5.0,
        )
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"LAN-проверка DPS завершилась ошибкой: {error}") from error

    if str(dp_key) not in {str(key) for key in payload}:
        raise HTTPException(status_code=400, detail="Выбранный DPS не читается по LAN")

    return payload[str(dp_key)]


def _format_dps_value(capability: dict[str, Any] | None, raw_value: Any) -> str:
    if raw_value is None:
        return "Нет данных"

    if isinstance(raw_value, (dict, list)):
        return json.dumps(raw_value, ensure_ascii=False, separators=(",", ":"))

    capability_code = str((capability or {}).get("capability_code") or "")
    value_type = str((capability or {}).get("value_type") or "")
    values_json = (capability or {}).get("values_json") or {}
    unit = UNIT_LABELS.get(str(values_json.get("unit") or "").strip(), str(values_json.get("unit") or "").strip())

    if isinstance(raw_value, bool) or value_type == "Boolean":
        return "Включено" if bool(raw_value) else "Выключено"

    if capability_code == "temp_unit_convert":
        normalized = str(raw_value).strip().lower()
        if normalized == "c":
            return "Celsius"
        if normalized == "f":
            return "Fahrenheit"
        return str(raw_value)

    if capability_code.startswith("countdown"):
        try:
            return _format_duration(int(raw_value))
        except (TypeError, ValueError):
            return str(raw_value)

    if isinstance(raw_value, (int, float)) and value_type in {"Integer", "value", ""}:
        number = float(raw_value)
        scale = int(values_json.get("scale", 0) or 0)
        if scale > 0:
            number /= 10 ** scale
        elif capability_code == "cur_voltage" and abs(number) >= 1000:
            number /= 10.0

        rendered = _format_decimal(number)
        return f"{rendered} {unit}".strip()

    return str(raw_value)


def _read_measurement_from_capabilities(
    raw_dps: dict[str, Any],
    capabilities: list[dict[str, Any]],
    capability_code: str,
) -> float | None:
    preferred: dict[str, Any] | None = None
    fallback: dict[str, Any] | None = None

    for capability in capabilities:
        if str(capability.get("capability_code") or "") != capability_code:
            continue
        if capability.get("capability_source") == "status":
            preferred = capability
            break
        fallback = capability

    capability = preferred or fallback
    if not capability:
        return None

    dp_id = capability.get("dp_id")
    if dp_id is None:
        return None

    raw_value = raw_dps.get(str(dp_id))
    if raw_value is None:
        return None

    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None

    values_json = capability.get("values_json") or {}
    scale = int(values_json.get("scale", 0) or 0)
    if scale > 0:
        value /= 10 ** scale
    elif capability_code == "cur_voltage" and abs(value) >= 1000:
        value /= 10.0

    unit = str(values_json.get("unit") or "").strip()
    if capability_code == "cur_current" and unit == "A":
        value *= 1000.0

    if capability_code == "cur_power":
        current_ma = _read_measurement_from_capabilities(raw_dps, capabilities, "cur_current")
        voltage_v = _read_measurement_from_capabilities(raw_dps, capabilities, "cur_voltage")
        if current_ma is not None and voltage_v is not None and current_ma > 0 and voltage_v > 0:
            apparent_power_w = (current_ma / 1000.0) * voltage_v
            if value > apparent_power_w * 3 and (value / 10.0) <= apparent_power_w * 1.6:
                value /= 10.0

    return value


def _read_breaker_fallback_measurements(raw_dps: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    try:
        power_w = (float(raw_dps.get("102")) / 100.0) if raw_dps.get("102") is not None else None
    except (TypeError, ValueError):
        power_w = None

    voltage_values = [
        float(raw_dps.get(key)) / 10.0 if float(raw_dps.get(key)) >= 1000 else float(raw_dps.get(key))
        for key in ("107", "108", "109")
        if raw_dps.get(key) is not None
    ]
    voltage_values = [value for value in voltage_values if value is not None and value > 0]
    voltage_v = sum(voltage_values) / len(voltage_values) if voltage_values else None

    try:
        current_raw = float(raw_dps.get("103")) if raw_dps.get("103") is not None else None
    except (TypeError, ValueError):
        current_raw = None
    current_ma = current_raw if current_raw is not None and current_raw > 0 else None

    return current_ma, power_w, voltage_v


def _augment_current_summary(summary: dict[str, Any], capabilities: list[dict[str, Any]]) -> None:
    raw_dps = summary.get("latest_raw_dps") or {}
    has_total_counter = any(
        str(capability.get("capability_code") or "") in {"total_forward_energy", "add_ele"}
        for capability in capabilities
    )
    current_ma = _read_measurement_from_capabilities(raw_dps, capabilities, "cur_current")
    power_w = _read_measurement_from_capabilities(raw_dps, capabilities, "cur_power")
    voltage_v = _read_measurement_from_capabilities(raw_dps, capabilities, "cur_voltage")

    breaker_current_ma, breaker_power_w, breaker_voltage_v = _read_breaker_fallback_measurements(raw_dps)
    if current_ma is None:
        current_ma = breaker_current_ma
    if power_w is None:
        power_w = breaker_power_w
    if voltage_v is None:
        voltage_v = breaker_voltage_v

    if power_w is None:
        power_w = None if has_total_counter else summary.get("latest_power_w")

    summary["current_power_w"] = round(power_w, 1) if power_w is not None else None
    summary["current_voltage_v"] = round(voltage_v, 1) if voltage_v is not None else None
    summary["current_current_ma"] = round(current_ma, 1) if current_ma is not None else None


def _build_device_functions(capabilities: list[dict[str, Any]]) -> list[dict[str, str]]:
    functions: list[dict[str, Any]] = []
    seen_codes: set[str] = set()

    for capability in capabilities:
        if capability.get("capability_source") != "functions":
            continue

        code = str(capability.get("capability_code") or "").strip()
        if not code or code in seen_codes:
            continue
        control_type = SUPPORTED_CONTROL_TYPES.get(code)
        if not control_type:
            continue
        seen_codes.add(code)

        label, description = FUNCTION_LABELS.get(code, ("", ""))
        if not label:
            label = str(capability.get("capability_name") or code).replace("_", " ").strip().capitalize()
        if not description:
            value_type = str(capability.get("value_type") or "").strip()
            if value_type == "Boolean":
                description = "Переключение состояния"
            elif value_type == "Integer":
                description = "Настраиваемый числовой параметр"
            elif value_type == "Enum":
                description = "Выбор режима из списка"
            elif value_type == "String":
                description = "Передача текстового значения"
            else:
                description = "Доступная функция устройства"

        values_json = capability.get("values_json") or {}
        functions.append(
            {
                "code": code,
                "label": label,
                "description": description,
                "control_type": control_type,
                "dp_id": int(capability.get("dp_id") or 0),
                "min": int(values_json.get("min", 0) or 0),
                "max": int(values_json.get("max", 0) or 0),
                "step": int(values_json.get("step", 1) or 1),
                "unit": UNIT_LABELS.get(str(values_json.get("unit") or "").strip(), str(values_json.get("unit") or "").strip()),
            }
        )

    return functions


def _get_capability_by_dp_id(capabilities: list[dict[str, Any]], dp_id: str) -> dict[str, Any] | None:
    for capability in capabilities:
        if str(capability.get("dp_id") or "") == dp_id:
            return capability
    return None


def _format_metric_number(value: float, digits: int = 3) -> str:
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def _decode_phase_packet_parts(raw_value: Any) -> list[dict[str, str]]:
    parts = [
        {"short_label": "I", "label": "Ток", "unit": "А", "value": "--"},
        {"short_label": "U", "label": "Напряжение", "unit": "В", "value": "--"},
        {"short_label": "P", "label": "Мощность", "unit": "кВт", "value": "--"},
        {"short_label": "L", "label": "Утечка", "unit": "А", "value": "--"},
    ]
    if not isinstance(raw_value, str) or not raw_value:
        return parts

    try:
        payload = base64.b64decode(raw_value)
    except Exception:
        return parts

    if len(payload) not in {8, 10}:
        return parts

    voltage_v = int.from_bytes(payload[0:2], byteorder="big", signed=False) / 10.0
    current_a = int.from_bytes(payload[2:5], byteorder="big", signed=False) / 1000.0
    power_kw = int.from_bytes(payload[5:8], byteorder="big", signed=False) / 1000.0
    parts[0]["value"] = _format_metric_number(current_a)
    parts[1]["value"] = _format_metric_number(voltage_v, 1)
    parts[2]["value"] = _format_metric_number(power_kw)
    if len(payload) == 10:
        leakage_a = int.from_bytes(payload[8:10], byteorder="big", signed=False) / 1000.0
        parts[3]["value"] = _format_metric_number(leakage_a)
    return parts


def _build_metric_tooltip(capability: dict[str, Any] | None, parts: list[dict[str, str]] | None = None) -> str:
    capability_name = str((capability or {}).get("capability_name") or "").strip() or "Параметр"
    value_type = str((capability or {}).get("value_type") or "").strip().lower()
    values_json = (capability or {}).get("values_json") or {}
    unit = str(values_json.get("unit") or "").strip()

    if value_type == "raw" and unit == "phase_packet":
        type_label = "raw phase packet"
    elif value_type in {"value", "integer"}:
        type_label = "число"
    elif value_type == "boolean":
        type_label = "логический"
    else:
        type_label = value_type or "не указан"

    lines = [capability_name, f"Тип: {type_label}"]
    if parts:
        lines.extend(f"{part['label']} ({part['short_label']}): {part['unit']}" for part in parts)
    elif unit:
        lines.append(f"Единицы: {unit}")
    return "\n".join(lines)


def _build_live_metrics(
    visualized_codes: list[str] | tuple[str, ...],
    capabilities: list[dict[str, Any]],
    raw_dps: dict[str, Any],
) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    for code in visualized_codes:
        key = str(code)
        capability = _get_capability_by_dp_id(capabilities, key)
        raw_value = raw_dps.get(key)
        capability_code = str((capability or {}).get("capability_code") or "")
        tooltip = _build_metric_tooltip(capability)
        metric: dict[str, Any] = {
            "code": key,
            "label": str((capability or {}).get("capability_name") or capability_code or f"DPS {key}"),
            "display_kind": "text",
            "tooltip": tooltip,
        }
        if capability_code in {"phase_a", "phase_b", "phase_c"}:
            parts = _decode_phase_packet_parts(raw_value)
            metric.update(
                {
                    "display_kind": "phase_packet",
                    "parts": parts,
                    "value": " ".join(f"{part['short_label']} {part['value']} {part['unit']}" for part in parts if part["value"] != "--") or "Нет данных",
                    "tooltip": _build_metric_tooltip(capability, parts),
                }
            )
        else:
            metric["value"] = _format_dps_value(capability, raw_value)
        metrics.append(metric)
    return metrics


def _attach_function_state(functions: list[dict[str, Any]], raw_dps: dict[str, Any]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for item in functions:
        current_raw = raw_dps.get(str(item["dp_id"])) if item.get("dp_id") else None
        current_value = current_raw
        current_label = "Нет данных"

        if item["control_type"] == "toggle":
            current_value = bool(current_raw) if current_raw is not None else False
            current_label = "Включено" if current_value else "Выключено"
        elif item["control_type"] == "timer":
            try:
                current_value = int(current_raw or 0)
            except (TypeError, ValueError):
                current_value = 0
            current_label = _format_duration(current_value) if current_value else "Не задан"

        enriched.append({
            **item,
            "current_value": current_value,
            "current_label": current_label,
        })

    return enriched


def _apply_device_command(device: tinytuya.Device, function_code: str, dp_id: int, value: Any) -> None:
    if function_code == "switch":
        device.set_status(bool(value), switch=dp_id)
        return
    if function_code == "countdown_1":
        device.set_value(dp_id, int(value))
        return
    raise ValueError("Функция пока не поддерживается")


def _resolve_period(config: AppConfig, period: str, start_raw: str | None, end_raw: str | None) -> tuple[datetime, datetime]:
    now = datetime.now(_get_timezone(config))
    if period == "custom":
        if not start_raw or not end_raw:
            raise HTTPException(status_code=400, detail="Для произвольного периода нужны начальная и конечная даты")
        start = datetime.fromisoformat(start_raw).replace(tzinfo=now.tzinfo)
        end = datetime.fromisoformat(end_raw).replace(tzinfo=now.tzinfo) + timedelta(days=1)
        return start, end
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now
    if period == "week":
        start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now
    if period == "year":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, now
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return month_start, now


async def _poll_loop(app: FastAPI) -> None:
    config: AppConfig = app.state.app_config

    while True:
        started_at = monotonic()
        devices = await asyncio.to_thread(get_polling_devices, config)
        for device in devices:
            try:
                captured_at, power_w, raw_dps = await asyncio.to_thread(build_sample, device)
                sample = DeviceSample(
                    device_id=device.device_id,
                    captured_at=captured_at,
                    power_w=power_w,
                    raw_dps=raw_dps,
                )
                app.state.live_samples[device.device_id] = sample

                last_saved_at = app.state.last_saved_at.get(device.device_id)
                should_save = last_saved_at is None or (captured_at - last_saved_at).total_seconds() >= config.sample_write_interval_seconds
                if should_save:
                    await asyncio.to_thread(save_sample, config, sample)
                    app.state.last_saved_at[device.device_id] = captured_at
            except Exception:
                continue

        elapsed = monotonic() - started_at
        await asyncio.sleep(max(config.poll_interval_seconds - elapsed, 0.0))


def _format_live_timestamp(config: AppConfig, value: datetime) -> str:
    return value.astimezone(_get_timezone(config)).strftime("%d.%m.%Y %H:%M:%S")


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _get_sensor_cloud_status_style(fetched_at: datetime | None) -> str:
    if fetched_at is None:
        return "error"

    age_seconds = max((datetime.now(timezone.utc) - fetched_at.astimezone(timezone.utc)).total_seconds(), 0.0)
    if age_seconds <= SENSOR_CLOUD_OK_SECONDS:
        return "ok"
    if age_seconds <= SENSOR_CLOUD_WARNING_SECONDS:
        return "warning"
    return "error"


def _apply_live_summary(config: AppConfig, summary: dict, live_samples: dict[str, DeviceSample]) -> dict:
    total_power_w = 0.0
    for device in summary.get("devices", []):
        live_sample = live_samples.get(device["device_id"])
        if live_sample:
            device["last_seen"] = _format_live_timestamp(config, live_sample.captured_at)
            device["raw_dps"] = live_sample.raw_dps
        total_power_w += float(device.get("current_power_kw") or 0.0) * 1000.0

    summary["current_power_kw"] = round(total_power_w / 1000.0, 3)
    return summary


def _apply_live_stats(config: AppConfig, stats: dict, live_sample: DeviceSample | None) -> dict:
    if not live_sample:
        return stats

    now = datetime.now(_get_timezone(config))
    stats["summary"]["latest_sample"] = _format_live_timestamp(config, live_sample.captured_at)
    stats["summary"]["latest_sample_age_seconds"] = get_sample_age_seconds(live_sample.captured_at, now)
    stats["summary"]["latest_sample_status"] = get_sample_status(live_sample.captured_at, now)
    stats["summary"]["latest_raw_dps"] = live_sample.raw_dps
    stats["summary"]["latest_power_w"] = round(live_sample.power_w, 1)
    return stats


def _hydrate_recent_visualized_dps(
    config: AppConfig,
    device_id: str,
    raw_dps: dict[str, Any],
    visualized_codes: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    required_keys = [str(code) for code in visualized_codes if str(code)]
    if not required_keys:
        return dict(raw_dps or {})

    merged = dict(raw_dps or {})
    missing = {key for key in required_keys if merged.get(key) in (None, "")}
    if not missing:
        return merged

    for row in get_recent_raw_dps_samples(config, device_id, limit=16):
        candidate = row.get("raw_dps") if isinstance(row, dict) else None
        if not isinstance(candidate, dict):
            continue
        for key in list(missing):
            value = candidate.get(key)
            if value in (None, ""):
                continue
            merged[key] = value
            missing.discard(key)
        if not missing:
            break

    return merged


def _get_cached_device_capabilities(request: Request, config: AppConfig, device_id: str) -> list[dict[str, Any]]:
    cache: dict[str, list[dict[str, Any]]] = request.app.state.device_capabilities_cache
    capabilities = cache.get(device_id)
    if capabilities is None:
        capabilities = get_device_capabilities(config, device_id)
        cache[device_id] = capabilities
    return capabilities


def _build_device_live_payload(
    config: AppConfig,
    device_id: str,
    capabilities: list[dict[str, Any]],
    visualized_codes: list[str] | tuple[str, ...],
    live_sample: DeviceSample | None,
) -> dict[str, Any]:
    if live_sample:
        summary = {
            "latest_sample": _format_live_timestamp(config, live_sample.captured_at),
            "latest_sample_age_seconds": get_sample_age_seconds(live_sample.captured_at, datetime.now(_get_timezone(config))),
            "latest_sample_status": get_sample_status(live_sample.captured_at, datetime.now(_get_timezone(config))),
            "latest_raw_dps": _hydrate_recent_visualized_dps(config, device_id, live_sample.raw_dps, visualized_codes),
            "latest_power_w": round(live_sample.power_w, 1),
        }
    else:
        summary = {
            "latest_sample": None,
            "latest_sample_age_seconds": None,
            "latest_sample_status": "error",
            "latest_raw_dps": {},
            "latest_power_w": None,
        }

    summary["latest_raw_dps"] = _hydrate_recent_visualized_dps(
        config,
        device_id,
        summary.get("latest_raw_dps") or {},
        visualized_codes,
    )

    _augment_current_summary(summary, capabilities)

    return {
        "summary": summary,
        "live_metrics": _build_live_metrics(visualized_codes, capabilities, summary["latest_raw_dps"]),
        "device_functions": _attach_function_state(_build_device_functions(capabilities), summary["latest_raw_dps"]),
    }


def _build_dashboard_live_payload(request: Request, config: AppConfig) -> dict[str, Any]:
    live_samples: dict[str, DeviceSample] = request.app.state.live_samples
    device_rows_by_id: dict[str, dict[str, Any]] = request.app.state.device_rows_by_id
    now = datetime.now(_get_timezone(config))
    devices: list[dict[str, Any]] = []
    sensor_devices: list[dict[str, Any]] = []
    total_power_w = 0.0
    online_device_count = 0

    for device_id, sample in live_samples.items():
        device = device_rows_by_id.get(device_id)
        if not device:
            continue

        last_seen_status = get_sample_status(sample.captured_at, now)
        if last_seen_status == "ok":
            online_device_count += 1

        entry = {
            "device_id": device_id,
            "last_seen": _format_live_timestamp(config, sample.captured_at),
            "last_seen_status": last_seen_status,
            "connection_ready": True,
        }

        if device.get("is_energy_meter"):
            if str(device.get("power_type") or "total").strip().lower() == "current":
                current_power_w = float(sample.power_w or 0.0)
            else:
                capabilities = _get_cached_device_capabilities(request, config, device_id)
                current_power_w = _read_measurement_from_capabilities(sample.raw_dps, capabilities, "cur_power")
                if current_power_w is None:
                    current_power_w = 0.0
            total_power_w += float(current_power_w or 0.0)
            devices.append(
                {
                    **entry,
                    "current_power_kw": round(float(current_power_w or 0.0) / 1000.0, 3),
                }
            )
            continue

        sensor_devices.append(entry)

    return {
        "current_power_kw": round(total_power_w / 1000.0, 3),
        "device_count": online_device_count,
        "devices": devices,
        "sensor_devices": sensor_devices,
    }


def _extract_cloud_status_result(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    result = payload.get("result")
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        return [item for item in (result.get("status") or []) if isinstance(item, dict)]
    return []


def _fetch_sensor_cloud_status(config: AppConfig, device_id: str) -> tuple[list[dict[str, Any]], datetime | None, str | None]:
    cloud_config = load_cloud_config(required=False)
    if cloud_config:
        try:
            cloud = tinytuya.Cloud(
                apiRegion=cloud_config.region,
                apiKey=cloud_config.api_key,
                apiSecret=cloud_config.api_secret,
                apiDeviceID=cloud_config.api_device_id or device_id,
            )
            payload = cloud.cloudrequest(f"/v1.0/devices/{device_id}/status")
            status_items = _extract_cloud_status_result(payload)
            if status_items:
                return status_items, datetime.now(timezone.utc), "Tuya Cloud"
        except Exception:
            pass

    return [], None, None


def _build_sensor_dashboard_entry(
    request: Request,
    config: AppConfig,
    device: dict[str, Any],
) -> dict[str, Any]:
    device_id = str(device.get("device_id") or "")
    capabilities = _get_cached_device_capabilities(request, config, device_id)
    raw_dps = device.get("raw_dps")
    local_raw_dps = raw_dps if isinstance(raw_dps, dict) else {}
    cloud_status_items, cloud_fetched_at, cloud_source = _fetch_sensor_cloud_status(config, device_id)
    metrics = _build_sensor_metrics(capabilities, local_raw_dps, cloud_status_items)

    preview_metrics = [
        metric for metric in metrics
        if metric.get("code") in {"va_temperature", "temp_current", "humidity_value", "va_battery"}
    ]
    if not preview_metrics:
        preview_metrics = [metric for metric in metrics if metric.get("code") != "temp_unit_convert"]

    primary_metric = preview_metrics[0] if preview_metrics else None
    secondary_metric = preview_metrics[1] if len(preview_metrics) > 1 else None

    if device.get("connection_ready") and device.get("last_seen"):
        last_update = device.get("last_seen")
        last_update_status = device.get("last_seen_status") or "error"
    else:
        last_update = _format_live_timestamp(config, cloud_fetched_at) if cloud_fetched_at else None
        last_update_status = _get_sensor_cloud_status_style(cloud_fetched_at) if cloud_status_items else "error"

    if device.get("connection_ready") and device.get("ip_address"):
        connection_label = f"LAN: {device['ip_address']}"
    elif cloud_status_items:
        connection_label = cloud_source or "Tuya Cloud"
    else:
        connection_label = "Облачное устройство"

    return {
        **device,
        "primary_metric": primary_metric,
        "secondary_metric": secondary_metric,
        "connection_label": connection_label,
        "last_seen": last_update,
        "last_seen_status": last_update_status,
    }


def _decorate_sensor_dashboard_entries(request: Request, config: AppConfig, devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _build_sensor_dashboard_entry(request, config, device)
        for device in devices
    ]


def _get_dashboard_sensor_payload(request: Request, config: AppConfig) -> dict[str, Any]:
    summary_cache_key = _get_aggregate_cache_key("summary", "sensors")
    summary = _get_cached_aggregate_payload(request, summary_cache_key)
    if summary is None:
        month_start, now = _month_window(config)
        summary = get_dashboard_summary(config, month_start, now, dict(request.app.state.live_samples))
        summary = _set_cached_aggregate_payload(request, summary_cache_key, summary)

    sensor_devices = _decorate_sensor_dashboard_entries(
        request,
        config,
        _decorate_devices_media(summary.get("sensor_devices", [])),
    )
    return {"sensor_devices": sensor_devices}


def _build_sensor_metrics(
    capabilities: list[dict[str, Any]],
    raw_dps: dict[str, Any],
    cloud_status_items: list[dict[str, Any]],
) -> list[dict[str, str]]:
    values_by_code = {
        str(item.get("code") or ""): item.get("value")
        for item in cloud_status_items
        if isinstance(item, dict) and item.get("code")
    }
    metrics: list[dict[str, str]] = []
    seen_codes: set[str] = set()

    for capability in capabilities:
        if capability.get("capability_source") != "status":
            continue

        code = str(capability.get("capability_code") or "").strip()
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)

        dp_id = capability.get("dp_id")
        raw_value = raw_dps.get(str(dp_id)) if dp_id is not None else None
        if raw_value is None:
            raw_value = values_by_code.get(code)

        label = DPS_LABELS.get(code) or str(capability.get("capability_name") or code).replace("_", " ").strip().capitalize()
        metrics.append(
            {
                "code": code,
                "label": label,
                "value": _format_dps_value(capability, raw_value),
            }
        )

    return metrics


def _build_sensor_page_payload(
    config: AppConfig,
    device: dict[str, Any],
    capabilities: list[dict[str, Any]],
    live_sample: DeviceSample | None,
) -> dict[str, Any]:
    latest_sample = live_sample
    latest_sample_row = None
    if latest_sample is None:
        latest_sample_row = get_latest_sample(config, str(device.get("device_id") or ""))

    local_raw_dps = (
        latest_sample.raw_dps
        if latest_sample is not None
        else _normalize_json_field(latest_sample_row.get("raw_dps")) if latest_sample_row else {}
    )
    local_captured_at = (
        latest_sample.captured_at
        if latest_sample is not None
        else _parse_dt(latest_sample_row.get("captured_at")) if latest_sample_row and latest_sample_row.get("captured_at") else None
    )

    cloud_status_items, cloud_fetched_at, cloud_source = _fetch_sensor_cloud_status(config, str(device.get("device_id") or ""))
    metrics = _build_sensor_metrics(capabilities, local_raw_dps, cloud_status_items)
    last_update = local_captured_at or cloud_fetched_at
    last_update_status = (
        get_sample_status(last_update, datetime.now(_get_timezone(config)))
        if local_captured_at
        else _get_sensor_cloud_status_style(cloud_fetched_at) if cloud_status_items else "error"
    )

    return {
        "metrics": metrics,
        "state_source": "Локальное устройство" if local_raw_dps else (cloud_source or "Нет данных"),
        "last_update": _format_live_timestamp(config, last_update) if last_update else None,
        "last_update_status": last_update_status,
        "connection_ready": bool(device.get("connection_ready")),
        "ip_address": str(device.get("ip_address") or "").strip() or None,
    }


def _get_aggregate_cache_key(*parts: str) -> tuple[str, ...]:
    return tuple(parts)


def _get_cached_aggregate_payload(request: Request, key: tuple[str, ...]) -> dict[str, Any] | None:
    cache: dict[tuple[str, ...], dict[str, Any]] = request.app.state.aggregate_cache
    entry = cache.get(key)
    if not entry:
        return None
    if monotonic() - float(entry.get("created_at") or 0.0) > AGGREGATE_CACHE_TTL_SECONDS:
        cache.pop(key, None)
        return None
    return entry.get("payload")


def _set_cached_aggregate_payload(request: Request, key: tuple[str, ...], payload: dict[str, Any]) -> dict[str, Any]:
    cache: dict[tuple[str, ...], dict[str, Any]] = request.app.state.aggregate_cache
    cache[key] = {
        "created_at": monotonic(),
        "payload": payload,
    }
    return payload


def _invalidate_aggregate_cache(request: Request, *, device_id: str | None = None) -> None:
    cache: dict[tuple[str, ...], dict[str, Any]] = request.app.state.aggregate_cache
    keys_to_remove = []
    for key in cache:
        if key[:1] == ("summary",):
            keys_to_remove.append(key)
            continue
        if device_id and len(key) > 1 and key[1] == device_id:
            keys_to_remove.append(key)
    for key in keys_to_remove:
        cache.pop(key, None)


def _build_device_stats_payload(
    config: AppConfig,
    device_id: str,
    period: str,
    start_raw: str | None,
    end_raw: str | None,
    live_sample: DeviceSample | None,
) -> dict[str, Any] | None:
    range_start, range_end = _resolve_period(config, period, start_raw, end_raw)
    bucket = pick_bucket(range_start, range_end, period)
    device, capabilities, stats = get_device_context_and_stats(config, device_id, range_start, range_end, period, bucket)
    if not device:
        return None
    stats = _apply_live_stats(config, stats, live_sample)
    stats["summary"]["latest_raw_dps"] = _hydrate_recent_visualized_dps(
        config,
        device_id,
        stats["summary"].get("latest_raw_dps") or {},
        device.get("visualized_codes") or [],
    )
    _augment_current_summary(stats["summary"], capabilities)
    stats["device_functions"] = _attach_function_state(_build_device_functions(capabilities), stats["summary"]["latest_raw_dps"])
    stats["live_metrics"] = _build_live_metrics(device.get("visualized_codes") or [], capabilities, stats["summary"]["latest_raw_dps"])
    return {
        "device": dict(device),
        "period": {
            "name": period,
            "start": range_start.isoformat(),
            "end": range_end.isoformat(),
            "bucket": bucket,
        },
        **stats,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.app_config = load_app_config()
    app.state.live_samples = {}
    app.state.last_saved_at = {}
    app.state.device_capabilities_cache = {}
    app.state.aggregate_cache = {}
    await asyncio.to_thread(apply_migrations, app.state.app_config.database_url)
    await asyncio.to_thread(init_connection_pool, app.state.app_config.database_url)
    await asyncio.to_thread(sync_device_profiles_from_disk, app.state.app_config)
    app.state.device_rows_by_id = {
        str(device["device_id"]): device
        for device in await asyncio.to_thread(get_device_rows, app.state.app_config)
    }
    app.state.poller = asyncio.create_task(_poll_loop(app))
    yield
    app.state.poller.cancel()
    with suppress(asyncio.CancelledError):
        await app.state.poller
    await asyncio.to_thread(close_connection_pool)


app = FastAPI(title="Учет электроэнергии", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    config: AppConfig = request.app.state.app_config
    summary_cache_key = _get_aggregate_cache_key("summary", "dashboard")
    summary = _get_cached_aggregate_payload(request, summary_cache_key)
    if summary is None:
        month_start, now = _month_window(config)
        summary = get_dashboard_summary(config, month_start, now, dict(request.app.state.live_samples))
        summary = _set_cached_aggregate_payload(request, summary_cache_key, summary)
    summary["devices"] = _decorate_devices_media(summary.get("devices", []))
    summary["sensor_devices"] = _decorate_sensor_dashboard_entries(
        request,
        config,
        _decorate_devices_media(summary.get("sensor_devices", [])),
    )
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "summary": summary,
            "page_title": "Учет электроэнергии",
            "month_label": _format_month_label(now),
        },
    )


@app.get("/devices/{device_id}", response_class=HTMLResponse)
def device_details(request: Request, device_id: str) -> HTMLResponse:
    config: AppConfig = request.app.state.app_config
    device = get_device_row(config, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Устройство не найдено")

    decorated_device = _decorate_device_media(dict(device))
    if not device.get("is_energy_meter"):
        capabilities = get_device_capabilities(config, device_id)
        sensor_payload = _build_sensor_page_payload(
            config,
            decorated_device,
            capabilities,
            request.app.state.live_samples.get(device_id),
        )
        return templates.TemplateResponse(
            request=request,
            name="sensor.html",
            context={
                "device": decorated_device,
                "sensor": sensor_payload,
                "initial_sensor_json": json.dumps(jsonable_encoder(sensor_payload), ensure_ascii=False),
                "page_title": f"{device['name']} - датчик",
            },
        )

    stats_cache_key = _get_aggregate_cache_key("device-stats", device_id, "day", "", "")
    payload = _get_cached_aggregate_payload(request, stats_cache_key)
    if payload is None:
        payload = _build_device_stats_payload(
            config,
            device_id,
            "day",
            None,
            None,
            request.app.state.live_samples.get(device_id),
        )
        if payload:
            payload = _set_cached_aggregate_payload(request, stats_cache_key, payload)
    if not payload:
        raise HTTPException(status_code=404, detail="Устройство не найдено")
    device = payload["device"]

    return templates.TemplateResponse(
        request=request,
        name="device.html",
        context={
            "device": _decorate_device_media(dict(device)),
            "initial_stats_json": json.dumps(jsonable_encoder(payload), ensure_ascii=False),
            "page_title": f"{device['name']} - детали",
        },
    )


@app.get("/connect-device", response_class=HTMLResponse)
def connect_device_page(request: Request) -> HTMLResponse:
    config: AppConfig = request.app.state.app_config
    devices = get_device_rows(config)
    return templates.TemplateResponse(
        request=request,
        name="connect_device.html",
        context={
            "devices": devices,
            "kind_labels": DEVICE_KIND_LABELS,
            "page_title": "Подключить устройство",
        },
    )


@app.get("/health")
async def healthcheck() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/api/summary")
def summary_api(request: Request) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    summary_cache_key = _get_aggregate_cache_key("summary", "api")
    summary = _get_cached_aggregate_payload(request, summary_cache_key)
    if summary is None:
        month_start, now = _month_window(config)
        summary = get_dashboard_summary(config, month_start, now, dict(request.app.state.live_samples))
        summary = _set_cached_aggregate_payload(request, summary_cache_key, summary)
    summary["devices"] = _decorate_devices_media(summary.get("devices", []))
    summary["sensor_devices"] = _decorate_sensor_dashboard_entries(
        request,
        config,
        _decorate_devices_media(summary.get("sensor_devices", [])),
    )
    return JSONResponse(jsonable_encoder(summary))


@app.get("/api/sensors/summary")
def sensor_summary_api(request: Request) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    payload = _get_dashboard_sensor_payload(request, config)
    return JSONResponse(jsonable_encoder(payload))


@app.get("/api/live-summary")
def live_summary_api(request: Request) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    payload = _build_dashboard_live_payload(request, config)
    return JSONResponse(jsonable_encoder(payload))


@app.post("/api/devices/connect")
def connect_device_api(request: Request, payload: ConnectDevicePayload) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    try:
        result = lookup_device_local_ip(config, payload.device_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    device_row = get_device_row(config, result["device_id"])
    if device_row:
        request.app.state.device_rows_by_id[result["device_id"]] = device_row
    _invalidate_aggregate_cache(request, device_id=result["device_id"])
    return JSONResponse(jsonable_encoder(result))


@app.delete("/api/devices/{device_id}")
def delete_device_api(request: Request, device_id: str) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    device = get_device_row(config, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Устройство не найдено")

    delete_managed_device(config, device_id)
    request.app.state.live_samples.pop(device_id, None)
    request.app.state.device_rows_by_id.pop(device_id, None)
    request.app.state.device_capabilities_cache.pop(device_id, None)
    _invalidate_aggregate_cache(request, device_id=device_id)
    return JSONResponse({"status": "ok", "device_id": device_id})


@app.post("/api/devices/{device_id}/summary-config")
def device_summary_config_api(
    request: Request,
    device_id: str,
    payload: DeviceSummaryConfigPayload,
) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    device = get_device_row(config, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Устройство не найдено")
    control_device = get_control_device(config, device_id)

    capabilities = get_device_capabilities(config, device_id)
    allowed_codes = {
        str(capability.get("dp_id") or "")
        for capability in capabilities
        if capability.get("dp_id") is not None
    }
    visualized_codes = [str(code) for code in payload.visualized_codes if str(code) in allowed_codes]
    power_type = _normalize_power_type(payload.power_type)
    total_power_dps_key = str(payload.total_power_dps_key or "").strip() or None
    if total_power_dps_key is not None and total_power_dps_key not in allowed_codes:
        raise HTTPException(status_code=400, detail="Выбранный DPS отсутствует в спецификации устройства")

    if total_power_dps_key is None:
        raise HTTPException(status_code=400, detail="Нужно выбрать DPS код мощности")

    selected_metadata = _find_selected_power_metadata(
        capabilities,
        total_power_dps_key,
    )

    total_power_scale = 0.0
    for capability in capabilities:
        if str(capability.get("dp_id") or "") != total_power_dps_key:
            continue
        values_json = capability.get("values_json") or {}
        scale = int(values_json.get("scale", 0) or 0)
        total_power_scale = float(10 ** scale) if scale > 0 else 1.0
        break

    if total_power_scale <= 0.0:
        raise HTTPException(status_code=400, detail="Не удалось определить scale для выбранного DPS")

    raw_value = _validate_lan_power_dps(control_device, total_power_dps_key, power_type=power_type)
    _validate_selected_power_metadata(selected_metadata, power_type, raw_value)

    update_device_summary_config(
        config,
        device_id,
        total_power_dps_key=total_power_dps_key,
        total_power_scale=total_power_scale,
        visualized_codes=visualized_codes,
        power_type=power_type,
    )
    updated_device = get_device_row(config, device_id)
    if updated_device:
        request.app.state.device_rows_by_id[device_id] = updated_device
    _invalidate_aggregate_cache(request, device_id=device_id)
    return JSONResponse(
        {
            "status": "ok",
            "total_power_dps_key": total_power_dps_key,
            "visualized_codes": visualized_codes,
            "power_type": power_type,
        }
    )


@app.get("/api/devices/{device_id}/sensor")
def sensor_device_api(request: Request, device_id: str) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    device = get_device_row(config, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Устройство не найдено")
    if device.get("is_energy_meter"):
        raise HTTPException(status_code=400, detail="Маршрут доступен только для датчиков")

    capabilities = get_device_capabilities(config, device_id)
    payload = _build_sensor_page_payload(
        config,
        _decorate_device_media(dict(device)),
        capabilities,
        request.app.state.live_samples.get(device_id),
    )
    return JSONResponse(jsonable_encoder(payload))


@app.post("/api/devices/{device_id}/functions/{function_code}")
def device_function_api(request: Request, device_id: str, function_code: str, payload: DeviceFunctionPayload) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    device = get_control_device(config, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Устройство не найдено")
    if not device.ip_address:
        raise HTTPException(status_code=400, detail="Для устройства не найден локальный адрес")

    capabilities = get_device_capabilities(config, device_id)
    functions = _build_device_functions(capabilities)
    function = next((item for item in functions if item["code"] == function_code), None)
    if not function:
        raise HTTPException(status_code=404, detail="Функция не найдена")

    try:
        if function["control_type"] == "toggle":
            value = bool(payload.value)
        elif function["control_type"] == "timer":
            value = int(payload.value)
            if value < function["min"] or (function["max"] and value > function["max"]):
                raise ValueError("Значение таймера вне допустимого диапазона")
        else:
            raise ValueError("Функция пока не поддерживается")

        tinytuya_device = tinytuya.Device(device.device_id, device.ip_address, device.local_key)
        tinytuya_device.set_version(device.version)
        tinytuya_device.set_socketTimeout(1.5)
        tinytuya_device.set_socketRetryLimit(1)
        _apply_device_command(tinytuya_device, function_code, function["dp_id"], value)

        captured_at, power_w, raw_dps = build_sample(device)
        sample = DeviceSample(
            device_id=device.device_id,
            captured_at=captured_at,
            power_w=power_w,
            raw_dps=raw_dps,
        )
        request.app.state.live_samples[device.device_id] = sample
        save_sample(config, sample)
        request.app.state.last_saved_at[device.device_id] = captured_at
        _invalidate_aggregate_cache(request, device_id=device.device_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Не удалось выполнить команду: {error}") from error

    return JSONResponse({"status": "ok"})


@app.get("/api/devices/{device_id}/stats")
def device_stats_api(
    request: Request,
    device_id: str,
    period: str = Query(default="day"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    stats_cache_key = _get_aggregate_cache_key("device-stats", device_id, period, start or "", end or "")
    payload = _get_cached_aggregate_payload(request, stats_cache_key)
    if payload is None:
        payload = _build_device_stats_payload(
            config,
            device_id,
            period,
            start,
            end,
            request.app.state.live_samples.get(device_id),
        )
        if payload:
            payload = _set_cached_aggregate_payload(request, stats_cache_key, payload)
    if not payload:
        raise HTTPException(status_code=404, detail="Устройство не найдено")
    return JSONResponse(jsonable_encoder(payload))


@app.get("/api/devices/{device_id}/live")
def device_live_api(request: Request, device_id: str) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    capabilities = _get_cached_device_capabilities(request, config, device_id)
    device = get_device_row(config, device_id)
    payload = _build_device_live_payload(
        config,
        device_id,
        capabilities,
        tuple(str(code) for code in ((device or {}).get("visualized_codes") or [])),
        request.app.state.live_samples.get(device_id),
    )
    return JSONResponse(jsonable_encoder(payload))