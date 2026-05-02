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

from app.device_registry import DEVICE_KIND_LABELS, connect_device
from config import AppConfig, load_app_config, load_devices
from app.storage import (
    DeviceSample,
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
templates.env.globals["static_asset_version"] = "20260502-3"

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


class ConnectDevicePayload(BaseModel):
    device_id: str


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


def _build_interpreted_dps(raw_dps: dict[str, Any], capabilities: list[dict[str, Any]]) -> list[dict[str, str]]:
    capability_map: dict[str, dict[str, Any]] = {}
    for capability in capabilities:
        dp_id = capability.get("dp_id")
        if dp_id is None:
            continue
        key = str(dp_id)
        current = capability_map.get(key)
        if current is None or capability.get("capability_source") == "status":
            capability_map[key] = capability

    def sort_key(item: tuple[str, Any]) -> tuple[int, str]:
        key = str(item[0])
        return (0, f"{int(key):08d}") if key.isdigit() else (1, key)

    interpreted: list[dict[str, str]] = []
    for dp_key, raw_value in sorted(raw_dps.items(), key=sort_key):
        capability = capability_map.get(str(dp_key))
        capability_code = str((capability or {}).get("capability_code") or "")
        label = DPS_LABELS.get(capability_code) or str((capability or {}).get("capability_name") or "").strip() or f"DP {dp_key}"
        interpreted.append(
            {
                "label": label,
                "value": _format_dps_value(capability, raw_value),
            }
        )

    return interpreted


def _build_device_functions(capabilities: list[dict[str, Any]]) -> list[dict[str, str]]:
    functions: list[dict[str, str]] = []
    seen_codes: set[str] = set()

    for capability in capabilities:
        if capability.get("capability_source") != "functions":
            continue

        code = str(capability.get("capability_code") or "").strip()
        if not code or code in seen_codes:
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

        functions.append({
            "label": label,
            "description": description,
        })

    return functions


def _resolve_period(config: AppConfig, period: str, start_raw: str | None, end_raw: str | None) -> tuple[datetime, datetime]:
    now = datetime.now(_get_timezone(config))
    if period == "custom":
        if not start_raw or not end_raw:
            raise HTTPException(status_code=400, detail="Для произвольного периода нужны начальная и конечная даты")
        start = datetime.fromisoformat(start_raw).replace(tzinfo=now.tzinfo)
        end = datetime.fromisoformat(end_raw).replace(tzinfo=now.tzinfo) + timedelta(days=1)
        return start, end
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now
    if period == "7d":
        return now - timedelta(days=7), now
    if period == "30d":
        return now - timedelta(days=30), now
    if period == "90d":
        return now - timedelta(days=90), now
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
    await asyncio.to_thread(sync_devices, app.state.app_config, load_devices())
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
    capabilities = await asyncio.to_thread(get_device_capabilities, config, slug)

    return templates.TemplateResponse(
        request=request,
        name="device.html",
        context={
            "device": dict(device),
            "device_functions": _build_device_functions(capabilities),
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
    bucket = pick_bucket(range_start, range_end)
    stats = await asyncio.to_thread(get_device_stats, config, slug, range_start, range_end, bucket)
    stats = _apply_live_stats(config, stats, request.app.state.live_samples.get(slug))
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