import asyncio
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

from app.device_registry import DEVICE_KIND_LABELS, connect_device, sync_config_device_capabilities
from config import AppConfig, load_app_config, load_devices
from app.storage import (
    DeviceSample,
    close_connection_pool,
    get_control_device,
    get_device_capabilities,
    get_device_context_and_stats,
    get_dashboard_summary,
    get_device_row,
    get_device_rows,
    get_device_stats,
    get_sample_age_seconds,
    get_sample_status,
    get_polling_devices,
    init_connection_pool,
    init_db,
    pick_bucket,
    save_sample,
    sync_devices,
)
from app.tuya_service import build_sample


BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["static_asset_version"] = "20260502-17"

DEVICE_IMAGE_EXTENSIONS = (".png", ".webp", ".jpg", ".jpeg", ".svg")

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

DPS_LABELS = {
    "switch": "Питание",
    "countdown_1": "Таймер отключения",
    "cur_current": "Ток",
    "cur_power": "Мощность",
    "cur_voltage": "Напряжение",
    "add_ele": "Энергия",
    "total_forward_energy": "Потребление",
    "total_reverse_energy": "Возврат энергии",
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


def _resolve_device_image_url(image_id: str | None) -> str | None:
    return _resolve_device_image_url_by_key(image_id, "device-images")


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
    image_id = str(enriched.get("image_id") or "").strip() or None
    device_id = str(enriched.get("device_id") or "").strip() or None
    enriched["image_url"] = (
        _resolve_device_image_url_by_key(device_id, "images")
        or _resolve_device_image_url(image_id)
        or _resolve_device_image_url_by_key(device_id, "device-images")
    )
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


def _format_dps_value(capability: dict[str, Any] | None, raw_value: Any) -> str:
    if raw_value is None:
        return "Нет данных"

    capability_code = str((capability or {}).get("capability_code") or "")
    value_type = str((capability or {}).get("value_type") or "")
    values_json = (capability or {}).get("values_json") or {}
    unit = UNIT_LABELS.get(str(values_json.get("unit") or "").strip(), str(values_json.get("unit") or "").strip())

    if isinstance(raw_value, bool) or value_type == "Boolean":
        return "Включено" if bool(raw_value) else "Выключено"

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


def _augment_current_summary(summary: dict[str, Any], capabilities: list[dict[str, Any]]) -> None:
    raw_dps = summary.get("latest_raw_dps") or {}
    current_ma = _read_measurement_from_capabilities(raw_dps, capabilities, "cur_current")
    power_w = _read_measurement_from_capabilities(raw_dps, capabilities, "cur_power")
    voltage_v = _read_measurement_from_capabilities(raw_dps, capabilities, "cur_voltage")

    if power_w is None:
        power_w = summary.get("latest_power_w")
    if voltage_v is None:
        voltage_v = summary.get("latest_voltage_v")

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
                captured_at, power_w, voltage_v, raw_dps = await asyncio.to_thread(build_sample, device)
                sample = DeviceSample(
                    device_id=device.device_id,
                    captured_at=captured_at,
                    power_w=power_w,
                    voltage_v=voltage_v,
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


def _apply_live_summary(config: AppConfig, summary: dict, live_samples: dict[str, DeviceSample]) -> dict:
    total_power_w = 0.0
    for device in summary.get("devices", []):
        live_sample = live_samples.get(device["device_id"])
        if live_sample:
            device["current_power_kw"] = round(live_sample.power_w / 1000.0, 3)
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
    if live_sample.voltage_v is not None:
        stats["summary"]["average_voltage_v"] = round(live_sample.voltage_v, 1)
        stats["summary"]["latest_voltage_v"] = round(live_sample.voltage_v, 1)
    return stats


def _get_cached_device_capabilities(request: Request, config: AppConfig, device_id: str) -> list[dict[str, Any]]:
    cache: dict[str, list[dict[str, Any]]] = request.app.state.device_capabilities_cache
    capabilities = cache.get(device_id)
    if capabilities is None:
        capabilities = get_device_capabilities(config, device_id)
        cache[device_id] = capabilities
    return capabilities


def _build_device_live_payload(
    config: AppConfig,
    capabilities: list[dict[str, Any]],
    live_sample: DeviceSample | None,
) -> dict[str, Any]:
    if live_sample:
        summary = {
            "latest_sample": _format_live_timestamp(config, live_sample.captured_at),
            "latest_sample_age_seconds": get_sample_age_seconds(live_sample.captured_at, datetime.now(_get_timezone(config))),
            "latest_sample_status": get_sample_status(live_sample.captured_at, datetime.now(_get_timezone(config))),
            "latest_raw_dps": live_sample.raw_dps,
            "latest_power_w": round(live_sample.power_w, 1),
            "latest_voltage_v": round(live_sample.voltage_v, 1) if live_sample.voltage_v is not None else None,
        }
    else:
        summary = {
            "latest_sample": None,
            "latest_sample_age_seconds": None,
            "latest_sample_status": "error",
            "latest_raw_dps": {},
            "latest_power_w": None,
            "latest_voltage_v": None,
        }

    _augment_current_summary(summary, capabilities)

    return {
        "summary": summary,
        "device_functions": _attach_function_state(_build_device_functions(capabilities), summary["latest_raw_dps"]),
    }


def _build_dashboard_live_payload(request: Request, config: AppConfig) -> dict[str, Any]:
    live_samples: dict[str, DeviceSample] = request.app.state.live_samples
    device_rows_by_id: dict[str, dict[str, Any]] = request.app.state.device_rows_by_id
    now = datetime.now(_get_timezone(config))
    devices: list[dict[str, Any]] = []
    total_power_w = 0.0
    online_device_count = 0

    for device_id, sample in live_samples.items():
        device = device_rows_by_id.get(device_id)
        if not device or not device.get("is_energy_meter"):
            continue

        total_power_w += float(sample.power_w)
        last_seen_status = get_sample_status(sample.captured_at, now)
        if last_seen_status == "ok":
            online_device_count += 1

        devices.append(
            {
                "device_id": device_id,
                "current_power_kw": round(float(sample.power_w) / 1000.0, 3),
                "last_seen": _format_live_timestamp(config, sample.captured_at),
                "last_seen_status": last_seen_status,
            }
        )

    return {
        "current_power_kw": round(total_power_w / 1000.0, 3),
        "device_count": online_device_count,
        "devices": devices,
    }


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
    _augment_current_summary(stats["summary"], capabilities)
    stats["device_functions"] = _attach_function_state(_build_device_functions(capabilities), stats["summary"]["latest_raw_dps"])
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
    await asyncio.to_thread(init_connection_pool, app.state.app_config.database_url)
    await asyncio.to_thread(init_db, app.state.app_config)
    configured_devices = load_devices()
    await asyncio.to_thread(sync_devices, app.state.app_config, configured_devices)
    await asyncio.to_thread(sync_config_device_capabilities, app.state.app_config, configured_devices)
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
    month_start, now = _month_window(config)
    summary = get_dashboard_summary(config, month_start, now, dict(request.app.state.live_samples))
    summary["devices"] = _decorate_devices_media(summary.get("devices", []))
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
    payload = _build_device_stats_payload(
        config,
        device_id,
        "day",
        None,
        None,
        request.app.state.live_samples.get(device_id),
    )
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
    month_start, now = _month_window(config)
    summary = get_dashboard_summary(config, month_start, now, dict(request.app.state.live_samples))
    summary["devices"] = _decorate_devices_media(summary.get("devices", []))
    return JSONResponse(jsonable_encoder(summary))


@app.get("/api/live-summary")
def live_summary_api(request: Request) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    payload = _build_dashboard_live_payload(request, config)
    return JSONResponse(jsonable_encoder(payload))


@app.post("/api/devices/connect")
def connect_device_api(request: Request, payload: ConnectDevicePayload) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    try:
        result = connect_device(config, payload.device_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return JSONResponse(jsonable_encoder(result))


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

        captured_at, power_w, voltage_v, raw_dps = build_sample(device)
        sample = DeviceSample(
            device_id=device.device_id,
            captured_at=captured_at,
            power_w=power_w,
            voltage_v=voltage_v,
            raw_dps=raw_dps,
        )
        request.app.state.live_samples[device.device_id] = sample
        save_sample(config, sample)
        request.app.state.last_saved_at[device.device_id] = captured_at
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
    payload = _build_device_stats_payload(
        config,
        device_id,
        period,
        start,
        end,
        request.app.state.live_samples.get(device_id),
    )
    if not payload:
        raise HTTPException(status_code=404, detail="Устройство не найдено")
    return JSONResponse(jsonable_encoder(payload))


@app.get("/api/devices/{device_id}/live")
def device_live_api(request: Request, device_id: str) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    capabilities = _get_cached_device_capabilities(request, config, device_id)
    payload = _build_device_live_payload(config, capabilities, request.app.state.live_samples.get(device_id))
    return JSONResponse(jsonable_encoder(payload))