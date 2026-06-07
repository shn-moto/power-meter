import asyncio
import base64
import json
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import bcrypt
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi import Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
import tinytuya

from app.device_registry import DEVICE_KIND_LABELS, lookup_device_local_ip
from config import AppConfig, TuyaDeviceConfig, load_app_config, load_cloud_config, load_session_secret
from app.storage import (
    DeviceSample,
    apply_migrations,
    close_connection_pool,
    create_user,
    delete_managed_device,
    get_control_device,
    get_device_capabilities,
    get_charger_day_stats,
    get_device_context_and_stats,
    get_dashboard_summary,
    get_meter_status,
    get_meter_discrepancy_periods,
    get_period_breakdown,
    list_meter_readings,
    save_meter_reading,
    delete_meter_reading,
    METER_APARTMENTS,
    METER_PREPAID_KWH,
    get_device_row,
    get_device_rows,
    get_user_by_username,
    get_recent_raw_dps_samples,
    get_recent_power_trace,
    get_solar_consumers_power_trace,
    set_device_solar_consumer,
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
from app.tuya_service import build_live_sample, build_sample, fetch_status, request_dps_by_index
from app.raw_listeners import RawListener, RawDpsSnapshot, has_trick678_request_mode, select_listener_devices


BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["static_asset_version"] = "20260506-02"

DEVICE_IMAGE_EXTENSIONS = (".png", ".webp", ".jpg", ".jpeg", ".svg")
AGGREGATE_CACHE_TTL_SECONDS = 5.0
SENSOR_CLOUD_STATUS_CACHE_SECONDS = 60.0
SENSOR_CLOUD_OK_SECONDS = 90.0
SENSOR_CLOUD_WARNING_SECONDS = 300.0
SENSOR_CLOUD_STATUS_CACHE: dict[str, dict[str, Any]] = {}
PUBLIC_PATHS = {"/health", "/login", "/logout"}
PUBLIC_PATH_PREFIXES = ("/static",)
LOGGER = logging.getLogger(__name__)

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
    "va_humidity": "Влажность",
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
    "switch_1": ("Питание", "Включение и выключение устройства"),
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
    "switch_1": "toggle",
    "switch_led": "toggle",
    "countdown_1": "timer",
    "work_mode": "enum",
    "bright_value": "slider",
    "bright_value_v2": "slider",
    "temp_value": "slider",
    "temp_value_v2": "slider",
    "colour_data": "color",
    "colour_data_v2": "color",
}

ENUM_OPTION_LABELS = {
    "work_mode": {
        "white": "Белый",
        "colour": "Цвет",
        "music": "Музыка",
        "scene": "Сцена",
    },
}


class ConnectDevicePayload(BaseModel):
    device_id: str


class DeviceSummaryConfigPayload(BaseModel):
    total_power_dps_key: str | None = None
    visualized_codes: list[str] = []
    power_type: str = "total"


class DeviceFunctionPayload(BaseModel):
    value: Any


class SolarConsumerTogglePayload(BaseModel):
    enabled: bool


def _hash_password(password: str) -> str:
    normalized_password = str(password or "")
    if not normalized_password:
        raise ValueError("Пароль не может быть пустым")
    return bcrypt.hashpw(normalized_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, password_hash: str) -> bool:
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def _safe_redirect_target(next_path: str | None) -> str:
    candidate = str(next_path or "").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    if candidate in {"/login", "/logout", "/register"}:
        return "/"
    return candidate


def _current_request_path(request: Request) -> str:
    if request.url.query:
        return f"{request.url.path}?{request.url.query}"
    return request.url.path


def _is_local_address(host: str | None) -> bool:
    normalized_host = str(host or "").strip()
    if not normalized_host:
        return False
    if normalized_host == "localhost":
        return True
    try:
        address = ip_address(normalized_host.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def _resolve_client_host(request: Request) -> str:
    peer_host = request.client.host if request.client else ""
    if _is_local_address(peer_host):
        forwarded_for = str(request.headers.get("x-forwarded-for") or "").strip()
        if forwarded_for:
            first_host = forwarded_for.split(",", 1)[0].strip()
            if first_host:
                return first_host
        real_ip = str(request.headers.get("x-real-ip") or "").strip()
        if real_ip:
            return real_ip
    return peer_host


def _is_local_network_request(request: Request) -> bool:
    return _is_local_address(_resolve_client_host(request))


def _is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or any(path.startswith(prefix) for prefix in PUBLIC_PATH_PREFIXES)


class AuthGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.is_local_request = _is_local_network_request(request)
        request.state.current_user = str(request.session.get("username") or "").strip().lower() or None

        path = request.url.path
        if path.startswith("/register") and not request.state.is_local_request:
            return HTMLResponse("Регистрация доступна только из локальной сети", status_code=403)

        if request.state.is_local_request:
            return await call_next(request)

        if _is_public_path(path) or (path.startswith("/register") and request.state.is_local_request):
            return await call_next(request)

        if request.state.current_user:
            return await call_next(request)

        if path.startswith("/api/"):
            return JSONResponse({"detail": "Требуется авторизация"}, status_code=401)

        login_url = "/login"
        next_path = _current_request_path(request)
        if next_path and next_path != "/":
            login_url = f"/login?{urlencode({'next': next_path})}"
        return RedirectResponse(url=login_url, status_code=303)


def _build_auth_template_context(
    *,
    page_title: str,
    username: str = "",
    error_message: str | None = None,
    success_message: str | None = None,
    next_path: str = "/",
) -> dict[str, Any]:
    return {
        "page_title": page_title,
        "username": username,
        "error_message": error_message,
        "success_message": success_message,
        "next_path": _safe_redirect_target(next_path),
    }


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


STATE_AWARE_TOGGLE_CODES = ("switch_led", "switch", "switch_1")


def _find_toggle_state(capabilities: list[dict[str, Any]], raw_dps: dict[str, Any]) -> bool | None:
    if not isinstance(raw_dps, dict) or not raw_dps:
        return None
    for capability in capabilities:
        code = str(capability.get("capability_code") or "")
        if code not in STATE_AWARE_TOGGLE_CODES:
            continue
        dp_id = capability.get("dp_id")
        if dp_id is None:
            continue
        value = raw_dps.get(str(dp_id))
        if value is None:
            continue
        return bool(value)
    return None


def _state_image_suffix(capabilities: list[dict[str, Any]], raw_dps: dict[str, Any]) -> str | None:
    state = _find_toggle_state(capabilities, raw_dps)
    if state is None:
        return None
    return "_on" if state else "_off"


def _toggle_preview_metric(capabilities: list[dict[str, Any]], raw_dps: dict[str, Any]) -> dict[str, str] | None:
    state = _find_toggle_state(capabilities, raw_dps)
    if state is None:
        return None
    return {"code": "switch_led", "label": "Состояние", "value": "Включено" if state else "Выключено"}


def _apply_state_aware_image(
    device: dict[str, Any],
    capabilities: list[dict[str, Any]] | None,
    raw_dps: dict[str, Any] | None,
) -> dict[str, Any]:
    device_id = str(device.get("device_id") or "").strip()
    if not device_id:
        return device
    suffix = _state_image_suffix(capabilities or [], raw_dps or {})
    if not suffix:
        return device
    state_url = _resolve_device_image_url_by_key(f"{device_id}{suffix}", "images")
    if state_url:
        device["image_url"] = state_url
    return device


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

    voltage_values: list[float] = []
    for key in ("107", "108", "109"):
        raw_value = raw_dps.get(key)
        if raw_value is None:
            continue
        try:
            voltage_value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if voltage_value >= 1000:
            voltage_value /= 10.0
        voltage_values.append(voltage_value)

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
        enum_options: list[dict[str, str]] = []
        if control_type == "enum":
            option_labels = ENUM_OPTION_LABELS.get(code, {})
            for raw_option in values_json.get("range") or []:
                option = str(raw_option)
                enum_options.append({"value": option, "label": option_labels.get(option, option.capitalize())})

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
                "options": enum_options,
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
        elif item["control_type"] == "enum":
            current_value = str(current_raw) if current_raw is not None else ""
            option_labels = {opt["value"]: opt["label"] for opt in item.get("options") or []}
            current_label = option_labels.get(current_value, current_value or "Нет данных")
        elif item["control_type"] == "slider":
            try:
                current_value = int(current_raw or 0)
            except (TypeError, ValueError):
                current_value = 0
            current_label = f"{current_value}"
        elif item["control_type"] == "color":
            current_value = str(current_raw) if current_raw is not None else ""
            current_label = _format_colour_hex(current_value)

        enriched.append({
            **item,
            "current_value": current_value,
            "current_label": current_label,
        })

    return enriched


def _apply_device_command(device: tinytuya.Device, function_code: str, dp_id: int, value: Any) -> None:
    control_type = SUPPORTED_CONTROL_TYPES.get(function_code)
    if control_type == "toggle":
        device.set_status(bool(value), switch=dp_id)
        return
    if control_type == "timer":
        device.set_value(dp_id, int(value))
        return
    if control_type == "enum":
        device.set_value(dp_id, str(value))
        return
    if control_type == "slider":
        device.set_value(dp_id, int(value))
        return
    if control_type == "color":
        # Lamp expects the Tuya HSV-encoded payload — 12 hex chars: HHHH SSSS VVVV
        # Hue 0..360, saturation/value 0..1000. Accept either pre-formatted hex or
        # a "#RRGGBB" picker value and convert.
        device.set_value(dp_id, _coerce_colour_payload(value))
        return
    raise ValueError("Функция пока не поддерживается")


def _format_colour_hex(value: str) -> str:
    parsed = _parse_tuya_colour(value)
    if not parsed:
        return value or "Нет данных"
    hue, sat, val = parsed
    return f"H {hue}° S {sat // 10}% V {val // 10}%"


def _parse_tuya_colour(value: str) -> tuple[int, int, int] | None:
    if not isinstance(value, str) or len(value) < 12:
        return None
    try:
        hue = int(value[0:4], 16)
        sat = int(value[4:8], 16)
        val = int(value[8:12], 16)
    except ValueError:
        return None
    return hue, sat, val


def _coerce_colour_payload(value: Any) -> str:
    if isinstance(value, str) and value:
        if value.startswith("#") and len(value) == 7:
            hue, sat, val = _hex_rgb_to_tuya_hsv(value)
            return f"{hue:04x}{sat:04x}{val:04x}"
        if len(value) >= 12 and all(ch in "0123456789abcdefABCDEF" for ch in value[:12]):
            return value[:12].lower()
    raise ValueError("Неверный формат цвета")


def _hex_rgb_to_tuya_hsv(hex_rgb: str) -> tuple[int, int, int]:
    r = int(hex_rgb[1:3], 16) / 255.0
    g = int(hex_rgb[3:5], 16) / 255.0
    b = int(hex_rgb[5:7], 16) / 255.0
    cmax = max(r, g, b)
    cmin = min(r, g, b)
    delta = cmax - cmin
    if delta == 0:
        hue = 0.0
    elif cmax == r:
        hue = 60.0 * (((g - b) / delta) % 6.0)
    elif cmax == g:
        hue = 60.0 * (((b - r) / delta) + 2.0)
    else:
        hue = 60.0 * (((r - g) / delta) + 4.0)
    sat = 0.0 if cmax == 0 else delta / cmax
    return int(round(hue)) % 360, int(round(sat * 1000)), int(round(cmax * 1000))


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


def _device_lan_thread_lock(app: FastAPI, device_id: str):
    """Per-device threading.Lock — shared between the asyncio poll loop
    (acquired inside to_thread) and the raw listener thread."""
    import threading
    locks: dict[str, threading.Lock] = app.state.device_lan_thread_locks
    lock = locks.get(device_id)
    if lock is None:
        lock = threading.Lock()
        locks[device_id] = lock
    return lock


def _device_lan_lock(app: FastAPI, device_id: str) -> asyncio.Lock:
    locks: dict[str, asyncio.Lock] = app.state.device_lan_locks
    lock = locks.get(device_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[device_id] = lock
    return lock


async def _poll_loop(app: FastAPI) -> None:
    config: AppConfig = app.state.app_config

    while True:
        started_at = monotonic()
        try:
            devices = await asyncio.to_thread(get_polling_devices, config)
            for device in devices:
                try:
                    thread_lock = _device_lan_thread_lock(app, device.device_id)

                    def _locked_build_sample(d=device, lock=thread_lock):
                        with lock:
                            return build_sample(d)

                    async with _device_lan_lock(app, device.device_id):
                        captured_at, power_w, raw_dps = await asyncio.to_thread(_locked_build_sample)
                    sample = DeviceSample(
                        device_id=device.device_id,
                        captured_at=captured_at,
                        power_w=power_w,
                        raw_dps=raw_dps,
                    )
                    app.state.live_samples[device.device_id] = sample
                    if has_trick678_request_mode(device):
                        app.state.raw_dps_latest[device.device_id] = RawDpsSnapshot(
                            raw_dps=dict(raw_dps),
                            captured_at=captured_at,
                        )

                    last_saved_at = app.state.last_saved_at.get(device.device_id)
                    should_save = last_saved_at is None or (captured_at - last_saved_at).total_seconds() >= config.sample_write_interval_seconds
                    if should_save:
                        await asyncio.to_thread(save_sample, config, sample)
                        app.state.last_saved_at[device.device_id] = captured_at
                except Exception:
                    LOGGER.exception("Polling device %s failed", device.device_id)
                    continue
        except Exception:
            LOGGER.exception("Polling loop iteration failed")

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


def _normalize_json_field(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    return {}


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
    for device in summary.get("devices", []):
        live_sample = live_samples.get(device["device_id"])
        if live_sample:
            device["last_seen"] = _format_live_timestamp(config, live_sample.captured_at)
            device["raw_dps"] = live_sample.raw_dps
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


def _merge_live_visualized_cache(
    raw_dps: dict[str, Any] | None,
    visualized_codes: list[str] | tuple[str, ...],
    cached_visualized_dps: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(cached_visualized_dps or {})
    merged.update(raw_dps or {})
    return merged


def _missing_visualized_codes(raw_dps: dict[str, Any], visualized_codes: list[str] | tuple[str, ...]) -> list[int]:
    missing: list[int] = []
    for code in visualized_codes:
        key = str(code).strip()
        if not key or not key.isdigit():
            continue
        if raw_dps.get(key) in (None, ""):
            missing.append(int(key))
    return missing


async def _refresh_live_visualized_dps(app: FastAPI, device: TuyaDeviceConfig, requested_indices: list[int]) -> None:
    if not requested_indices:
        app.state.live_visualized_tasks.pop(device.device_id, None)
        return

    try:
        async with _device_lan_lock(app, device.device_id):
            extra_dps, _ = await asyncio.to_thread(
                request_dps_by_index,
                device.device_id,
                device.ip_address,
                device.local_key,
                requested_indices,
                device.version,
                "default",
                5.0,
            )
        exact_matches = {
            str(index): extra_dps.get(str(index))
            for index in requested_indices
            if extra_dps.get(str(index)) not in (None, "")
        }
        if exact_matches:
            cached = dict(app.state.live_visualized_cache.get(device.device_id) or {})
            cached.update(exact_matches)
            app.state.live_visualized_cache[device.device_id] = cached
    except Exception:
        LOGGER.exception("Live visualized DPS refresh for device %s failed", device.device_id)
    finally:
        app.state.live_visualized_tasks.pop(device.device_id, None)


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
    generator_devices: list[dict[str, Any]] = []
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
            is_generator = bool(device.get("is_generator"))
            entry["is_generator"] = is_generator
            entry["is_solar_consumer"] = bool(device.get("is_solar_consumer"))
            entry["current_power_kw"] = round(float(current_power_w or 0.0) / 1000.0, 3)
            if is_generator:
                total_power_w -= float(current_power_w or 0.0)
                generator_devices.append(entry)
            else:
                total_power_w += float(current_power_w or 0.0)
                devices.append(entry)
            continue

        sensor_devices.append(entry)

    return {
        "current_power_kw": round(total_power_w / 1000.0, 3),
        "device_count": online_device_count,
        "devices": devices,
        "generator_devices": generator_devices,
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
    cached_entry = SENSOR_CLOUD_STATUS_CACHE.get(device_id)
    if cached_entry and (monotonic() - float(cached_entry.get("cached_at") or 0.0) <= SENSOR_CLOUD_STATUS_CACHE_SECONDS):
        return (
            list(cached_entry.get("status_items") or []),
            cached_entry.get("fetched_at"),
            cached_entry.get("source"),
        )

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
                fetched_at = datetime.now(timezone.utc)
                SENSOR_CLOUD_STATUS_CACHE[device_id] = {
                    "cached_at": monotonic(),
                    "status_items": list(status_items),
                    "fetched_at": fetched_at,
                    "source": "Tuya Cloud",
                }
                return status_items, fetched_at, "Tuya Cloud"
        except Exception:
            pass

    if cached_entry:
        return (
            list(cached_entry.get("status_items") or []),
            cached_entry.get("fetched_at"),
            cached_entry.get("source"),
        )

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

    preview_priority = ("va_temperature", "temp_current", "va_humidity", "humidity_value", "va_battery")
    preview_metrics = sorted(
        (metric for metric in metrics if metric.get("code") in preview_priority),
        key=lambda metric: preview_priority.index(str(metric.get("code"))),
    )
    if not preview_metrics:
        preview_metrics = [metric for metric in metrics if metric.get("code") != "temp_unit_convert"]

    primary_metric = preview_metrics[0] if preview_metrics else _toggle_preview_metric(capabilities, local_raw_dps)
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

    entry = {
        **device,
        "primary_metric": primary_metric,
        "secondary_metric": secondary_metric,
        "connection_label": connection_label,
        "last_seen": last_update,
        "last_seen_status": last_update_status,
    }
    return _apply_state_aware_image(entry, capabilities, local_raw_dps)


def _build_sensor_dashboard_entry_from_cache(
    request: Request,
    config: AppConfig,
    device: dict[str, Any],
) -> dict[str, Any]:
    device_id = str(device.get("device_id") or "")
    capabilities = _get_cached_device_capabilities(request, config, device_id)
    raw_dps = device.get("raw_dps")
    local_raw_dps = raw_dps if isinstance(raw_dps, dict) else {}
    metrics = _build_sensor_metrics(capabilities, local_raw_dps, [])

    preview_priority = ("va_temperature", "temp_current", "va_humidity", "humidity_value", "va_battery")
    preview_metrics = sorted(
        (metric for metric in metrics if metric.get("code") in preview_priority),
        key=lambda metric: preview_priority.index(str(metric.get("code"))),
    )
    if not preview_metrics:
        preview_metrics = [metric for metric in metrics if metric.get("code") != "temp_unit_convert"]

    primary_metric = preview_metrics[0] if preview_metrics else _toggle_preview_metric(capabilities, local_raw_dps)
    secondary_metric = preview_metrics[1] if len(preview_metrics) > 1 else None

    if device.get("connection_ready") and device.get("ip_address"):
        connection_label = f"LAN: {device['ip_address']}"
    else:
        connection_label = "Облачное устройство"

    entry = {
        **device,
        "primary_metric": primary_metric,
        "secondary_metric": secondary_metric,
        "connection_label": connection_label,
        "last_seen": device.get("last_seen"),
        "last_seen_status": device.get("last_seen_status") or "error",
    }
    return _apply_state_aware_image(entry, capabilities, local_raw_dps)


def _decorate_sensor_dashboard_entries(request: Request, config: AppConfig, devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _build_sensor_dashboard_entry(request, config, device)
        for device in devices
    ]


def _decorate_sensor_dashboard_entries_from_cache(
    request: Request,
    config: AppConfig,
    devices: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        _build_sensor_dashboard_entry_from_cache(request, config, device)
        for device in devices
    ]


def _get_dashboard_sensor_payload(request: Request, config: AppConfig) -> dict[str, Any]:
    summary_cache_key = _get_aggregate_cache_key("summary", "sensors")
    summary = _get_cached_aggregate_payload(request, summary_cache_key)
    if summary is None:
        month_start, now = _month_window(config)
        summary = get_dashboard_summary(config, month_start, now)
        summary = _set_cached_aggregate_payload(request, summary_cache_key, summary)

    sensor_devices = _decorate_sensor_dashboard_entries_from_cache(
        request,
        config,
        _decorate_devices_media(summary.get("sensor_devices", [])),
    )
    return {"sensor_devices": sensor_devices}


def _copy_dashboard_summary_payload(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        **summary,
        "devices": [dict(device) for device in summary.get("devices", [])],
        "generator_devices": [dict(device) for device in summary.get("generator_devices", [])],
        "sensor_devices": [dict(device) for device in summary.get("sensor_devices", [])],
    }


def _apply_live_dashboard_overlay(request: Request, config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    live_payload = _build_dashboard_live_payload(request, config)
    live_samples: dict[str, DeviceSample] = request.app.state.live_samples
    now = datetime.now(_get_timezone(config))

    payload["current_power_kw"] = live_payload.get("current_power_kw", payload.get("current_power_kw", 0.0))
    payload["device_count"] = live_payload.get("device_count", payload.get("device_count", 0))

    live_devices_by_id = {
        str(device.get("device_id") or ""): device
        for device in (*live_payload.get("devices", []), *live_payload.get("generator_devices", []))
        if device.get("device_id")
    }
    for device in (*payload.get("devices", []), *payload.get("generator_devices", [])):
        live_device = live_devices_by_id.get(str(device.get("device_id") or ""))
        if not live_device:
            continue
        device["current_power_kw"] = live_device.get("current_power_kw", device.get("current_power_kw"))
        device["last_seen"] = live_device.get("last_seen", device.get("last_seen"))
        device["last_seen_status"] = live_device.get("last_seen_status", device.get("last_seen_status"))
        live_sample = live_samples.get(str(device.get("device_id") or ""))
        if live_sample is not None:
            capabilities = _get_cached_device_capabilities(request, config, device["device_id"])
            _apply_state_aware_image(device, capabilities, live_sample.raw_dps)

    for sensor in payload.get("sensor_devices", []):
        device_id = str(sensor.get("device_id") or "")
        live_sample = live_samples.get(device_id)
        if not live_sample:
            continue
        sensor["raw_dps"] = live_sample.raw_dps
        sensor["last_seen"] = _format_live_timestamp(config, live_sample.captured_at)
        sensor["last_seen_status"] = get_sample_status(live_sample.captured_at, now)
        sensor["last_seen_age_seconds"] = get_sample_age_seconds(live_sample.captured_at, now)
        sensor["connection_ready"] = True

    return payload


def _build_dashboard_page_payload(request: Request, config: AppConfig) -> dict[str, Any]:
    summary_cache_key = _get_aggregate_cache_key("summary", "api")
    summary = _get_cached_aggregate_payload(request, summary_cache_key)
    if summary is None:
        month_start, now = _month_window(config)
        summary = get_dashboard_summary(config, month_start, now)
        summary = _set_cached_aggregate_payload(request, summary_cache_key, summary)

    payload = _copy_dashboard_summary_payload(summary)
    payload["devices"] = _decorate_devices_media(payload.get("devices", []))
    payload["generator_devices"] = _decorate_devices_media(payload.get("generator_devices", []))
    payload["sensor_devices"] = _decorate_sensor_dashboard_entries_from_cache(
        request,
        config,
        _decorate_devices_media(payload.get("sensor_devices", [])),
    )
    payload["meter"] = _build_meter_overview(config)
    return payload


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
    direct_local_raw_dps: dict[str, Any] = {}
    direct_local_captured_at: datetime | None = None
    if device.get("connection_ready") and device.get("ip_address"):
        control_device = get_control_device(config, str(device.get("device_id") or ""))
        if control_device:
            try:
                local_payload = fetch_status(control_device)
                direct_local_raw_dps = _normalize_json_field(local_payload.get("dps")) if isinstance(local_payload, dict) else {}
                if direct_local_raw_dps:
                    direct_local_captured_at = datetime.now(timezone.utc)
            except Exception:
                direct_local_raw_dps = {}
                direct_local_captured_at = None

    latest_sample = live_sample
    latest_sample_row = None
    if latest_sample is None:
        latest_sample_row = get_latest_sample(config, str(device.get("device_id") or ""))

    local_raw_dps = (
        direct_local_raw_dps
        if direct_local_raw_dps
        else latest_sample.raw_dps
        if latest_sample is not None
        else _normalize_json_field(latest_sample_row.get("raw_dps")) if latest_sample_row else {}
    )
    local_captured_at = (
        direct_local_captured_at
        if direct_local_captured_at is not None
        else latest_sample.captured_at
        if latest_sample is not None
        else _parse_dt(latest_sample_row.get("captured_at")) if latest_sample_row and latest_sample_row.get("captured_at") else None
    )

    cloud_status_items: list[dict[str, Any]] = []
    cloud_fetched_at: datetime | None = None
    cloud_source: str | None = None
    if not local_raw_dps:
        cloud_status_items, cloud_fetched_at, cloud_source = _fetch_sensor_cloud_status(config, str(device.get("device_id") or ""))
    metrics = _build_sensor_metrics(capabilities, local_raw_dps, cloud_status_items)
    last_update = local_captured_at or cloud_fetched_at
    last_update_status = (
        get_sample_status(last_update, datetime.now(_get_timezone(config)))
        if local_captured_at
        else _get_sensor_cloud_status_style(cloud_fetched_at) if cloud_status_items else "error"
    )

    device_functions = _attach_function_state(_build_device_functions(capabilities), local_raw_dps)
    state_aware_image = _apply_state_aware_image(
        {"device_id": device.get("device_id"), "image_url": device.get("image_url")},
        capabilities,
        local_raw_dps,
    ).get("image_url")
    state_metric = _toggle_preview_metric(capabilities, local_raw_dps)

    gateway_device_id = str(device.get("gateway_device_id") or "").strip() or None
    if gateway_device_id:
        gateway_name = str(device.get("gateway_name") or "").strip() or "шлюз"
        connection_label = f"Zigbee через {gateway_name}"
    elif device.get("connection_ready") and device.get("ip_address"):
        connection_label = f"LAN: {device['ip_address']}"
    else:
        connection_label = "Не обнаружено"

    return {
        "metrics": metrics,
        "state_source": "Локальное устройство" if local_raw_dps else (cloud_source or "Нет данных"),
        "state_label": state_metric.get("value") if state_metric else None,
        "last_update": _format_live_timestamp(config, last_update) if last_update else None,
        "last_update_age_seconds": get_sample_age_seconds(last_update, datetime.now(_get_timezone(config))) if last_update else None,
        "last_update_status": last_update_status,
        "connection_ready": bool(device.get("connection_ready")),
        "ip_address": str(device.get("ip_address") or "").strip() or None,
        "connection_label": connection_label,
        "device_functions": device_functions,
        "image_url": state_aware_image,
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
    raw_dps_snapshot: RawDpsSnapshot | None = None,
) -> dict[str, Any] | None:
    range_start, range_end = _resolve_period(config, period, start_raw, end_raw)
    bucket = pick_bucket(range_start, range_end, period)
    device, capabilities, stats = get_device_context_and_stats(config, device_id, range_start, range_end, period, bucket)
    if not device:
        return None
    is_current_power_charger = (
        (bool(device.get("is_charger")) or bool(device.get("is_generator")))
        and str(device.get("power_type") or "total").strip().lower() == "current"
    )
    charger_day_eligible = is_current_power_charger and (
        period == "day"
        or (period == "custom" and (range_end - range_start) <= timedelta(hours=36))
    )
    if charger_day_eligible:
        charger_stats = get_charger_day_stats(config, device_id, range_start, range_end)
        existing_summary = stats.get("summary") or {}
        merged_summary = {**existing_summary, **charger_stats.get("summary", {})}
        stats = {
            "summary": merged_summary,
            "series": charger_stats["series"],
            "sessions": charger_stats["sessions"],
            "chart": charger_stats["chart"],
        }
        if bool(device.get("is_generator")):
            stats["solar_consumers_series"] = get_solar_consumers_power_trace(
                config, range_start, range_end, bucket_seconds=30, max_points=720,
            )
    stats = _apply_live_stats(config, stats, live_sample)
    stats["summary"]["latest_raw_dps"] = _hydrate_recent_visualized_dps(
        config,
        device_id,
        stats["summary"].get("latest_raw_dps") or {},
        device.get("visualized_codes") or [],
    )
    if raw_dps_snapshot is not None:
        merged_raw = dict(stats["summary"].get("latest_raw_dps") or {})
        merged_raw.update(raw_dps_snapshot.raw_dps)
        stats["summary"]["latest_raw_dps"] = merged_raw
        if live_sample is None or raw_dps_snapshot.captured_at > live_sample.captured_at:
            now = datetime.now(_get_timezone(config))
            stats["summary"]["latest_sample"] = _format_live_timestamp(config, raw_dps_snapshot.captured_at)
            stats["summary"]["latest_sample_age_seconds"] = get_sample_age_seconds(raw_dps_snapshot.captured_at, now)
            stats["summary"]["latest_sample_status"] = get_sample_status(raw_dps_snapshot.captured_at, now)
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
    app.state.live_visualized_cache = {}
    app.state.live_visualized_tasks = {}
    app.state.device_lan_locks = {}
    app.state.device_lan_thread_locks = {}
    app.state.last_saved_at = {}
    app.state.device_capabilities_cache = {}
    app.state.aggregate_cache = {}
    app.state.raw_dps_latest: dict[str, RawDpsSnapshot] = {}
    app.state.raw_listeners: list[RawListener] = []
    await asyncio.to_thread(apply_migrations, app.state.app_config.database_url)
    await asyncio.to_thread(init_connection_pool, app.state.app_config.database_url)
    await asyncio.to_thread(sync_device_profiles_from_disk, app.state.app_config)
    app.state.device_rows_by_id = {
        str(device["device_id"]): device
        for device in await asyncio.to_thread(get_device_rows, app.state.app_config)
    }
    # Phase probes for trick678 devices now happen inside the poll loop's
    # status() session (see _piggyback_phase_probes in tuya_service.py).
    # The standalone listener thread is no longer needed; raw_dps_latest is
    # populated directly by the poll loop after each successful build_sample.
    _ = select_listener_devices  # noqa: F841 — kept for future re-introduction
    app.state.poller = asyncio.create_task(_poll_loop(app))
    yield
    app.state.poller.cancel()
    with suppress(asyncio.CancelledError):
        await app.state.poller
    for listener in app.state.raw_listeners:
        listener.stop()
    for listener in app.state.raw_listeners:
        listener.join(timeout=5.0)
    await asyncio.to_thread(close_connection_pool)


app = FastAPI(title="Учет электроэнергии", lifespan=lifespan)
app.add_middleware(AuthGateMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=load_session_secret(),
    same_site="lax",
    max_age=60 * 60 * 24 * 14,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    next_path: str | None = Query(default=None, alias="next"),
    registered: int = Query(default=0),
) -> HTMLResponse:
    if request.state.current_user:
        return RedirectResponse(url=_safe_redirect_target(next_path), status_code=303)

    success_message = "Пользователь создан. Теперь можно войти." if registered else None
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context=_build_auth_template_context(
            page_title="Вход",
            next_path=next_path or "/",
            success_message=success_message,
        ),
    )


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next_path: str = Form(default="/", alias="next"),
) -> Response:
    config: AppConfig = request.app.state.app_config
    user = get_user_by_username(config, username)
    if not user or not _verify_password(password, str(user.get("password_hash") or "")):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context=_build_auth_template_context(
                page_title="Вход",
                username=str(username or "").strip(),
                error_message="Неверный логин или пароль",
                next_path=next_path,
            ),
            status_code=400,
        )

    request.session.clear()
    request.session["username"] = str(user["username"])
    return RedirectResponse(url=_safe_redirect_target(next_path), status_code=303)


@app.post("/logout")
def logout_submit(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request) -> HTMLResponse:
    if not request.state.is_local_request:
        raise HTTPException(status_code=403, detail="Регистрация доступна только из локальной сети")

    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context=_build_auth_template_context(page_title="Регистрация"),
    )


@app.post("/register")
def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> Response:
    if not request.state.is_local_request:
        raise HTTPException(status_code=403, detail="Регистрация доступна только из локальной сети")

    config: AppConfig = request.app.state.app_config
    normalized_username = str(username or "").strip().lower()
    if not normalized_username:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context=_build_auth_template_context(
                page_title="Регистрация",
                error_message="Укажите имя пользователя",
            ),
            status_code=400,
        )
    if not password:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context=_build_auth_template_context(
                page_title="Регистрация",
                username=normalized_username,
                error_message="Пароль не может быть пустым",
            ),
            status_code=400,
        )
    if get_user_by_username(config, normalized_username):
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context=_build_auth_template_context(
                page_title="Регистрация",
                username=normalized_username,
                error_message="Пользователь уже существует",
            ),
            status_code=400,
        )

    create_user(
        config,
        username=normalized_username,
        password_hash=_hash_password(password),
        is_admin=False,
    )
    return RedirectResponse(url="/login?registered=1", status_code=303)


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
    summary["generator_devices"] = _decorate_devices_media(summary.get("generator_devices", []))
    summary["sensor_devices"] = _decorate_sensor_dashboard_entries(
        request,
        config,
        _decorate_devices_media(summary.get("sensor_devices", [])),
    )
    summary["meter"] = _build_meter_overview(config)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "summary": summary,
            "page_title": "Учет электроэнергии",
            "month_label": _format_month_label(now),
        },
    )


@app.get("/report", response_class=HTMLResponse)
def monthly_report(
    request: Request,
    year: int | None = Query(default=None),
    month: int | None = Query(default=None),
) -> HTMLResponse:
    config: AppConfig = request.app.state.app_config
    tz = _get_timezone(config)
    now = datetime.now(tz)

    target_year = int(year) if year else now.year
    target_month = int(month) if month else now.month
    target_month = max(1, min(12, target_month))
    target_year = max(2020, min(now.year + 1, target_year))

    month_start = datetime(target_year, target_month, 1, tzinfo=tz)
    if target_month == 12:
        month_end = datetime(target_year + 1, 1, 1, tzinfo=tz)
    else:
        month_end = datetime(target_year, target_month + 1, 1, tzinfo=tz)
    # For the in-flight month don't reach into the future
    window_end = min(month_end, now)
    is_current_month = (target_year == now.year and target_month == now.month)

    prev_month_end = month_start
    prev_month_start = (prev_month_end - timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year_start = datetime(target_year, 1, 1, tzinfo=tz)
    year_end_for_chart = datetime(target_year + 1, 1, 1, tzinfo=tz)
    year_end_for_chart = min(year_end_for_chart, now) if target_year >= now.year else year_end_for_chart

    daily = get_period_breakdown(config, month_start, window_end, "day")
    monthly_year_series = get_period_breakdown(config, year_start, year_end_for_chart, "month")

    # Past 12 full months (before the selected month) for the average baseline.
    avg_window_start = (month_start - timedelta(days=365)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    avg_window = get_period_breakdown(config, avg_window_start, month_start, "month")
    avg_net_kwh = (
        sum(item["net_kwh"] for item in avg_window) / len(avg_window)
        if avg_window
        else 0.0
    )

    def _sum(items: list[dict[str, Any]], key: str) -> float:
        return round(sum(float(item.get(key) or 0.0) for item in items), 3)

    current_consumed = _sum(daily, "consumed_kwh")
    current_generated = _sum(daily, "generated_kwh")
    current_net = round(current_consumed - current_generated, 3)

    prev_daily = get_period_breakdown(config, prev_month_start, prev_month_end, "day")
    prev_consumed = _sum(prev_daily, "consumed_kwh")
    prev_generated = _sum(prev_daily, "generated_kwh")
    prev_net = round(prev_consumed - prev_generated, 3)

    delta_vs_prev = round(current_net - prev_net, 3)
    delta_vs_prepaid = round(current_net - METER_PREPAID_KWH, 3)
    delta_vs_avg = round(current_net - avg_net_kwh, 3)

    tariff = float(config.tariff_per_kwh or 0.0)

    # Build year choices: current ±4 (cap at current year)
    year_choices = list(range(max(2020, now.year - 4), now.year + 1))
    month_choices = [
        {"value": idx + 1, "label": RUSSIAN_MONTHS[idx + 1]}
        for idx in range(12)
    ]

    payload = {
        "selected_year": target_year,
        "selected_month": target_month,
        "is_current_month": is_current_month,
        "month_label": _format_month_label(month_start),
        "previous_label": _format_month_label(prev_month_start),
        "year_label": str(target_year),
        "year_choices": year_choices,
        "month_choices": month_choices,
        "current": {
            "consumed_kwh": current_consumed,
            "generated_kwh": current_generated,
            "net_kwh": current_net,
            "cost": round(current_net * tariff, 2),
        },
        "previous": {
            "consumed_kwh": prev_consumed,
            "generated_kwh": prev_generated,
            "net_kwh": prev_net,
            "cost": round(prev_net * tariff, 2),
        },
        "delta_prev": {
            "kwh": delta_vs_prev,
            "cost": round(delta_vs_prev * tariff, 2),
        },
        "prepaid": {
            "kwh": METER_PREPAID_KWH,
            "cost": round(METER_PREPAID_KWH * tariff, 2),
        },
        "delta_prepaid": {
            "kwh": delta_vs_prepaid,
            "cost": round(delta_vs_prepaid * tariff, 2),
        },
        "average": {
            "kwh": round(avg_net_kwh, 3),
            "months_in_window": len(avg_window),
        },
        "delta_avg": {
            "kwh": delta_vs_avg,
            "cost": round(delta_vs_avg * tariff, 2),
        },
        "daily_series": daily,
        "monthly_series": monthly_year_series,
        "tariff_per_kwh": tariff,
    }
    return templates.TemplateResponse(
        request=request,
        name="report.html",
        context={
            "page_title": "Отчёт за период",
            "report": payload,
            "report_json": json.dumps(jsonable_encoder(payload), ensure_ascii=False),
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
            request.app.state.raw_dps_latest.get(device_id),
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
    payload = _build_dashboard_page_payload(request, config)
    return JSONResponse(jsonable_encoder(payload))


@app.get("/api/listener-stats")
def listener_stats_api(request: Request) -> JSONResponse:
    listeners: list[RawListener] = request.app.state.raw_listeners
    return JSONResponse({
        "now": datetime.now(timezone.utc).isoformat(),
        "listeners": [listener.stats() for listener in listeners],
    })


class MeterReadingPayload(BaseModel):
    apartment: str
    reading_at: str
    reading_kwh: float
    is_settlement: bool = False
    note: str | None = None


def _build_meter_overview(config: AppConfig) -> dict[str, Any]:
    status = get_meter_status(config)
    readings = list_meter_readings(config, limit=24)
    periods = get_meter_discrepancy_periods(config)
    return {
        "status": status,
        "apartments": list(METER_APARTMENTS),
        "prepaid_kwh": METER_PREPAID_KWH,
        "readings": readings,
        "discrepancy_periods": periods,
    }


@app.get("/api/meter-readings")
def meter_readings_api(request: Request) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    return JSONResponse(jsonable_encoder(_build_meter_overview(config)))


@app.post("/api/meter-readings")
def submit_meter_reading_api(request: Request, payload: MeterReadingPayload) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    try:
        reading_at = datetime.fromisoformat(payload.reading_at)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=f"Некорректное время: {payload.reading_at!r}") from error
    if reading_at.tzinfo is None:
        reading_at = reading_at.replace(tzinfo=_get_timezone(config))
    try:
        save_meter_reading(
            config,
            apartment=payload.apartment,
            reading_at=reading_at,
            reading_kwh=payload.reading_kwh,
            is_settlement=payload.is_settlement,
            note=payload.note,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return JSONResponse(jsonable_encoder(_build_meter_overview(config)))


@app.delete("/api/meter-readings/{reading_id}")
def delete_meter_reading_api(request: Request, reading_id: int) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    delete_meter_reading(config, reading_id=reading_id)
    return JSONResponse(jsonable_encoder(_build_meter_overview(config)))


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


@app.get("/api/devices/{device_id}/power-trace")
def device_power_trace_api(request: Request, device_id: str, minutes: int = 60) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    minutes = max(1, min(int(minutes or 60), 1440))
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=minutes)
    series = get_recent_power_trace(config, device_id, start, now)
    payload: dict[str, Any] = {"minutes": minutes, "series": series}

    device_row = get_device_row(config, device_id)
    if device_row and device_row.get("is_generator"):
        payload["consumers_series"] = get_solar_consumers_power_trace(config, start, now)
    return JSONResponse(payload)


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


@app.post("/api/devices/{device_id}/solar-consumer")
def device_solar_consumer_api(request: Request, device_id: str, payload: SolarConsumerTogglePayload) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    device = get_device_row(config, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Устройство не найдено")
    if not device.get("is_energy_meter"):
        raise HTTPException(status_code=400, detail="Только устройства учёта энергии могут быть помечены как потребитель солнца")
    updated = set_device_solar_consumer(config, device_id, payload.enabled)
    if not updated:
        raise HTTPException(status_code=404, detail="Устройство не найдено")
    refreshed = get_device_row(config, device_id)
    if refreshed:
        request.app.state.device_rows_by_id[device_id] = refreshed
    _invalidate_aggregate_cache(request, device_id=device_id)
    return JSONResponse({"status": "ok", "is_solar_consumer": bool(payload.enabled)})


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
        elif function["control_type"] == "slider":
            value = int(payload.value)
            if value < function["min"] or (function["max"] and value > function["max"]):
                raise ValueError("Значение вне допустимого диапазона")
        elif function["control_type"] == "enum":
            value = str(payload.value)
            allowed = {opt["value"] for opt in function.get("options") or []}
            if allowed and value not in allowed:
                raise ValueError("Недопустимое значение режима")
        elif function["control_type"] == "color":
            value = payload.value
        else:
            raise ValueError("Функция пока не поддерживается")

        if device.gateway_device_id:
            tinytuya_device = tinytuya.OutletDevice(
                dev_id=device.gateway_device_id,
                address=device.ip_address,
                local_key=device.local_key,
                cid=device.cid or device.device_id,
            )
        else:
            tinytuya_device = tinytuya.Device(device.device_id, device.ip_address, device.local_key)
        tinytuya_device.set_version(device.version)
        tinytuya_device.set_socketTimeout(1.5)
        tinytuya_device.set_socketRetryLimit(1)
        _apply_device_command(tinytuya_device, function_code, function["dp_id"], value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Не удалось выполнить команду: {error}") from error

    # Post-apply: refresh the cached DPS so the UI shows the new state on its
    # next poll. The device often refuses an immediate second LAN session
    # (especially Zigbee sub-devices sharing the gateway's socket), so we
    # treat failure here as non-fatal — the regular poll loop will catch up.
    try:
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
    except Exception as refresh_error:
        LOGGER.info("Post-command refresh skipped for %s: %s", device.device_id, refresh_error)

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
            request.app.state.raw_dps_latest.get(device_id),
        )
        if payload:
            payload = _set_cached_aggregate_payload(request, stats_cache_key, payload)
    if not payload:
        raise HTTPException(status_code=404, detail="Устройство не найдено")
    return JSONResponse(jsonable_encoder(payload))


@app.get("/api/devices/{device_id}/live")
async def device_live_api(request: Request, device_id: str) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    capabilities = _get_cached_device_capabilities(request, config, device_id)
    device = get_device_row(config, device_id)
    live_sample = request.app.state.live_samples.get(device_id)
    visualized_codes = tuple(str(code) for code in ((device or {}).get("visualized_codes") or []))

    control_device = get_control_device(config, device_id)
    listener_snapshot: RawDpsSnapshot | None = request.app.state.raw_dps_latest.get(device_id) if control_device else None
    listener_owned = control_device is not None and has_trick678_request_mode(control_device)

    if listener_owned:
        # Listener-owned devices: never fight the listener for the LAN
        # socket here. Merge the freshest status() snapshot the poll loop
        # left in live_samples with whatever DPS the listener pushed into
        # raw_dps_latest. Pure dict lookups — milliseconds total.
        if live_sample is not None and listener_snapshot is not None:
            merged_raw = dict(live_sample.raw_dps)
            merged_raw.update(listener_snapshot.raw_dps)
            live_sample = DeviceSample(
                device_id=live_sample.device_id,
                captured_at=max(live_sample.captured_at, listener_snapshot.captured_at),
                power_w=live_sample.power_w,
                raw_dps=merged_raw,
            )
        elif listener_snapshot is not None and live_sample is None:
            live_sample = DeviceSample(
                device_id=device_id,
                captured_at=listener_snapshot.captured_at,
                power_w=0.0,
                raw_dps=dict(listener_snapshot.raw_dps),
            )
    elif control_device:
        # Non-listener devices keep the existing on-demand fetch path with
        # trick678 fallback.
        try:
            thread_lock_live = _device_lan_thread_lock(request.app, control_device.device_id)

            def _locked_build_live_sample(d=control_device, lock=thread_lock_live):
                with lock:
                    return build_live_sample(d)

            async with _device_lan_lock(request.app, control_device.device_id):
                captured_at, power_w, raw_dps = await asyncio.to_thread(_locked_build_live_sample)
            cache_updates = {
                str(code): raw_dps.get(str(code))
                for code in visualized_codes
                if raw_dps.get(str(code)) not in (None, "")
            }
            if cache_updates:
                cached = dict(
                    request.app.state.live_visualized_cache.get(control_device.device_id) or {}
                )
                cached.update(cache_updates)
                request.app.state.live_visualized_cache[control_device.device_id] = cached
            raw_dps = _merge_live_visualized_cache(
                raw_dps,
                visualized_codes,
                request.app.state.live_visualized_cache.get(control_device.device_id),
            )
            live_sample = DeviceSample(
                device_id=control_device.device_id,
                captured_at=captured_at,
                power_w=power_w,
                raw_dps=raw_dps,
            )
            request.app.state.live_samples[control_device.device_id] = live_sample

            missing_indices = _missing_visualized_codes(raw_dps, visualized_codes)
            if missing_indices:
                tasks: dict[str, asyncio.Task[Any]] = request.app.state.live_visualized_tasks
                if device_id not in tasks or tasks[device_id].done():
                    tasks[device_id] = asyncio.create_task(
                        _refresh_live_visualized_dps(request.app, control_device, missing_indices)
                    )
        except Exception:
            LOGGER.exception("Live fetch for device %s failed", device_id)

    payload = _build_device_live_payload(
        config,
        device_id,
        capabilities,
        visualized_codes,
        live_sample,
    )
    return JSONResponse(jsonable_encoder(payload))