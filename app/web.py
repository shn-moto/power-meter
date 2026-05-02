import asyncio
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
    get_control_device,
    get_device_capabilities,
    get_dashboard_summary,
    get_device_row,
    get_device_rows,
    get_device_stats,
    get_polling_devices,
    init_db,
    pick_bucket,
    save_sample,
    sync_devices,
)
from app.tuya_service import build_sample


BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["static_asset_version"] = "20260502-10"

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
                    device_slug=device.slug,
                    captured_at=captured_at,
                    power_w=power_w,
                    voltage_v=voltage_v,
                    raw_dps=raw_dps,
                )
                app.state.live_samples[device.slug] = sample

                last_saved_at = app.state.last_saved_at.get(device.slug)
                should_save = last_saved_at is None or (captured_at - last_saved_at).total_seconds() >= config.sample_write_interval_seconds
                if should_save:
                    await asyncio.to_thread(save_sample, config, sample)
                    app.state.last_saved_at[device.slug] = captured_at
            except Exception:
                continue

        elapsed = monotonic() - started_at
        await asyncio.sleep(max(config.poll_interval_seconds - elapsed, 0.0))


def _format_live_timestamp(config: AppConfig, value: datetime) -> str:
    return value.astimezone(_get_timezone(config)).strftime("%d.%m.%Y %H:%M:%S")


def _apply_live_summary(config: AppConfig, summary: dict, live_samples: dict[str, DeviceSample]) -> dict:
    total_power_w = 0.0
    for device in summary.get("devices", []):
        live_sample = live_samples.get(device["slug"])
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

    stats["summary"]["latest_sample"] = _format_live_timestamp(config, live_sample.captured_at)
    stats["summary"]["latest_raw_dps"] = live_sample.raw_dps
    if live_sample.voltage_v is not None:
        stats["summary"]["average_voltage_v"] = round(live_sample.voltage_v, 1)
    return stats


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.app_config = load_app_config()
    app.state.live_samples = {}
    app.state.last_saved_at = {}
    await asyncio.to_thread(init_db, app.state.app_config)
    configured_devices = load_devices()
    await asyncio.to_thread(sync_devices, app.state.app_config, configured_devices)
    await asyncio.to_thread(sync_config_device_capabilities, app.state.app_config, configured_devices)
    app.state.poller = asyncio.create_task(_poll_loop(app))
    yield
    app.state.poller.cancel()
    with suppress(asyncio.CancelledError):
        await app.state.poller


app = FastAPI(title="Учет электроэнергии", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    config: AppConfig = request.app.state.app_config
    month_start, now = _month_window(config)
    summary = await asyncio.to_thread(get_dashboard_summary, config, month_start, now)
    summary = _apply_live_summary(config, summary, request.app.state.live_samples)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "summary": summary,
            "page_title": "Учет электроэнергии",
            "month_label": _format_month_label(now),
        },
    )


@app.get("/devices/{slug}", response_class=HTMLResponse)
async def device_details(request: Request, slug: str) -> HTMLResponse:
    config: AppConfig = request.app.state.app_config
    device = await asyncio.to_thread(get_device_row, config, slug)
    if not device:
        raise HTTPException(status_code=404, detail="Устройство не найдено")

    return templates.TemplateResponse(
        request=request,
        name="device.html",
        context={
            "device": dict(device),
            "page_title": f"{device['name']} - детали",
        },
    )


@app.get("/connect-device", response_class=HTMLResponse)
async def connect_device_page(request: Request) -> HTMLResponse:
    config: AppConfig = request.app.state.app_config
    devices = await asyncio.to_thread(get_device_rows, config)
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
async def summary_api(request: Request) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    month_start, now = _month_window(config)
    summary = await asyncio.to_thread(get_dashboard_summary, config, month_start, now)
    summary = _apply_live_summary(config, summary, request.app.state.live_samples)
    return JSONResponse(jsonable_encoder(summary))


@app.post("/api/devices/connect")
async def connect_device_api(request: Request, payload: ConnectDevicePayload) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    try:
        result = await asyncio.to_thread(connect_device, config, payload.device_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return JSONResponse(jsonable_encoder(result))


@app.post("/api/devices/{slug}/functions/{function_code}")
async def device_function_api(request: Request, slug: str, function_code: str, payload: DeviceFunctionPayload) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    device = await asyncio.to_thread(get_control_device, config, slug)
    if not device:
        raise HTTPException(status_code=404, detail="Устройство не найдено")
    if not device.ip_address:
        raise HTTPException(status_code=400, detail="Для устройства не найден локальный адрес")

    capabilities = await asyncio.to_thread(get_device_capabilities, config, slug)
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
        await asyncio.to_thread(_apply_device_command, tinytuya_device, function_code, function["dp_id"], value)

        captured_at, power_w, voltage_v, raw_dps = await asyncio.to_thread(build_sample, device)
        sample = DeviceSample(
            device_slug=device.slug,
            captured_at=captured_at,
            power_w=power_w,
            voltage_v=voltage_v,
            raw_dps=raw_dps,
        )
        request.app.state.live_samples[device.slug] = sample
        await asyncio.to_thread(save_sample, config, sample)
        request.app.state.last_saved_at[device.slug] = captured_at
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Не удалось выполнить команду: {error}") from error

    return JSONResponse({"status": "ok"})


@app.get("/api/devices/{slug}/stats")
async def device_stats_api(
    request: Request,
    slug: str,
    period: str = Query(default="month"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> JSONResponse:
    config: AppConfig = request.app.state.app_config
    device = await asyncio.to_thread(get_device_row, config, slug)
    if not device:
        raise HTTPException(status_code=404, detail="Устройство не найдено")

    range_start, range_end = _resolve_period(config, period, start, end)
    bucket = pick_bucket(range_start, range_end, period)
    stats = await asyncio.to_thread(get_device_stats, config, slug, range_start, range_end, period, bucket)
    stats = _apply_live_stats(config, stats, request.app.state.live_samples.get(slug))
    capabilities = await asyncio.to_thread(get_device_capabilities, config, slug)
    stats["device_functions"] = _attach_function_state(_build_device_functions(capabilities), stats["summary"]["latest_raw_dps"])
    return JSONResponse(
        jsonable_encoder({
            "device": dict(device),
            "period": {
                "name": period,
                "start": range_start.isoformat(),
                "end": range_end.isoformat(),
                "bucket": bucket,
            },
            **stats,
        })
    )