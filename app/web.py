import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
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
templates.env.globals["static_asset_version"] = "20260502-2"

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