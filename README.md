# Home Power Meter — agent handover

Small self-hosted "what is each socket in this flat drawing right now" app
on top of Tuya LAN protocol + TimescaleDB. This file is written for the
next AI agent picking up the codebase — it covers only the parts you need
to understand to make changes safely. Long-form prose is intentionally
avoided.

## 1. Topology

```
Tuya LAN devices  ───► poll loop (asyncio task)  ──► samples table (hypertable)
                                │                  ──► samples_hourly  (continuous aggregate)
                                │                  ──► samples_daily   (cont. aggregate)
                                │                  ──► samples_monthly (cont. aggregate)
                                └──────► app.state.live_samples (dict, in-memory)
                                                │
                          poll loop also pushes ▼
                          raw_dps_latest (dict, in-memory) for trick678 devices
                                                │
                                                ▼
                FastAPI HTTP/HTML routes ── /api/devices/{id}/live etc.
                                                │
                                                ▼
                    Browser (vanilla JS, ECharts via CDN)
```

- **Container layout** (`docker-compose.yml`): `db` (TimescaleDB pg16) +
  `power-meter` (FastAPI + uvicorn). Port 8484 exposed on LAN; Cloudflare
  Tunnel handles WAN. `./deploy/server-update.sh` does `git pull` + rebuild.
- **Linux host**: `ssh powermeter@shn-linux` — hostname is stable, the
  LAN IP rotates so don't hardcode it. The deploy script lives at
  `~/power-meter/deploy/server-update.sh`.
- **Migrations** under `app/migrations/*.sql` run at startup, tracked in
  `schema_migrations`. Don't rename old ones; append new ones.

## 2. Where the data lives

| Source            | DB table            | Purpose                                            |
|-------------------|---------------------|----------------------------------------------------|
| Tuya LAN status() | `samples`           | One row per device per `SAMPLE_WRITE_INTERVAL_SECONDS` (default 5s). Columns: `device_id, captured_at, power_w, raw_dps (JSONB), source`. Hypertable, 1-year retention, 14-day compression. `source` is `'live'` (poller), `'pushed'` (active-pusher devices — §7), or `'cloud-restore'` (back-fill from Tuya cloud logs, see `tools_restore_samples_from_cloud.py`). |
| poll loop         | `live_samples` dict | Newest sample per device, in-memory only, refreshed every poll iteration. Active-pusher endpoint writes here too so the dashboard sees pushed devices in real time. |
| trick678 probes   | `raw_dps_latest` dict | Newest phase-packet bytes per device (DPS 6/7/8 for breakers), in-memory only. |
| Meter readings    | `meter_readings`    | Manual electric-meter reading entries from the main page. |
| Automations       | `automations`       | One row per registered automation class (slug, bound device, cron, last_run_status/log). See §8. |

For energy aggregation we have **two paths**, picked per device:
- `power_type='current'` (sockets with `cur_power` DPS): `samples_hourly.energy_wh = avg(power_w) * (max-min)*n/(n-1)/3600`, clamped to 1h per bucket. Daily/monthly sum the hourly.
- `power_type='total'` (breakers with `total_forward_energy` counter): per-bucket energy is `last_kwh_of_bucket - last_kwh_of_previous_bucket` (telescoping). See `_counter_bar_energies_kwh` in `app/storage.py`. Sum-of-bars equals period total.

## 3. Device profiles

Source of truth lives in `profiles/devices/<device_id>.json`. On startup
`sync_device_profiles_from_disk` re-syncs them into `devices`,
`device_connections`, `device_capabilities` (rows in `device_capabilities`
are wiped and re-inserted on every sync).

Fields the rest of the code reads:
- `device.is_energy_meter` — sockets/breakers vs sensors.
- `device.is_charger` — devices that get the charger UI (line chart + sessions list on day view) instead of hourly bars. Session energy uses `add_ele` telescoping when the device exposes it (the Tuya counter is the device's own integral, matches the Atorch number), see `_get_add_ele_meta_from_capabilities` + the override loop in `get_charger_day_stats`.
- `device.is_generator` — feeds into the dashboard's "Генерация" section and the monthly report's "генерация" leg. Both polled solar sockets and active-pusher current sensors set this.
- `device.is_solar_consumer` — user-toggleable from the dashboard checkbox (not synced from the profile after first seed). Surfaced as a power trace under the generator chart.
- `device.is_gateway` — Zigbee hub. Has its own `local_key` and `local_ip`; sub-devices live behind it.
- `connection.gateway_device_id` — set on sub-devices (Zigbee child) instead of `local_key`/`local_ip`. At runtime the SQL JOIN in `get_polling_devices`/`get_control_device` inherits the gateway's endpoint and `tuya_service.fetch_status` opens an `OutletDevice` with `cid=device_id` so the gateway routes the query.
- `connection.power_correction_factor` — per-device multiplier applied to power in `extract_metrics` (LAN poll) and in `/api/ingest/...` (pusher). Default `1.0`. Used to compensate for cheap Tuya plugs that mis-meter switch-mode loads. Migration `012`.
- `connection.ingest_token` — non-NULL marks the device as an **active pusher** (§7). The LAN poll loop skips it (`local_key`/`ip_address` stay empty); samples arrive via HTTP POST. Migration `013`.
- `summary.default_power_mode` → `power_type` (`current` | `total`). For active-pusher devices the DPS-key validation is relaxed since they ship `power_w` (or `current_a`) directly.
- `summary.default_power_dps_key` → which DPS to read for the device's power value (current power for `current`, energy counter for `total`).
- `summary.default_visualized_codes` → DPS shown on the device detail page.
- For each entry in `dps[]`: `lan.request_mode` may be `"trick678_1P"` (Tesla wallbox, 1 phase) or `"trick678_3P"` (3-phase stove). This flags the device for the persistent-socket piggyback (see §5).

If a key rotates on the Tuya side (happens when the device is moved to a different access point), update `connection.local_key` in the profile **and** `UPDATE device_connections SET local_key=... WHERE device_id=...` in the DB. The running server picks up the new key on the next poll iteration.

## 4. Polling pipeline (`app/web.py::_poll_loop`)

- Runs as an asyncio task started in `lifespan`.
- Every `POLL_INTERVAL_SECONDS` (default 1s):
  - `get_polling_devices(config)` returns all devices with a local IP+key.
  - For each device, acquire **two** locks: an `asyncio.Lock` (event-loop-level concurrency) and a `threading.Lock` (serialises LAN access against any other thread, e.g. the LIVE endpoint). Both per-device, kept in `app.state.device_lan_locks` and `app.state.device_lan_thread_locks`.
  - Call `build_sample(device)` inside `asyncio.to_thread`. This wraps `fetch_status(device)` → `extract_metrics(...)` → returns `(captured_at, power_w, raw_dps)`.
  - Update `app.state.live_samples[device_id]`.
  - If device has any `request_mode` starting with `trick678_`, also update `app.state.raw_dps_latest[device_id]` with the same `raw_dps` (poll-loop is the only writer here now).
  - If `(now − last_saved_at) ≥ SAMPLE_WRITE_INTERVAL_SECONDS`, write the sample to the `samples` table and refresh continuous aggregates around the captured time.

### 4.1 `fetch_status` and the piggyback (`app/tuya_service.py`)

- Always: `_is_tuya_host_reachable` (0.15s × 3 ports) → fresh `tinytuya.Device` → `device.status()`.
- For trick678 devices (`_has_trick678_modes` returns True):
  - `device.set_socketPersistent(True)` **before** `status()` so the next call reuses the same TCP socket.
  - After `status()` succeeds, call `_piggyback_phase_probes(device, dps)` which does `device.updatedps(index=[6])`, `[7]`, `[8]` in turn on the same session and merges any returned `dps` keys into the status payload.
  - `device.close()` in `finally`.
- The phase probes either return `{'dps': {...}}` (success → bytes go into `raw_dps`), or `None`/`Err 905`/`Err 914` when the device isn't emitting. Failures are silent at this layer; we just don't update the bytes. The breaker only emits phase packets reliably when it has a real load — Tesla wallbox idle ≠ data.

### 4.2 Why we ditched the standalone listener thread

Earlier iteration ran a `RawListener(threading.Thread)` per breaker that
opened its own socket every 15 seconds. That always lost the race against
the poll loop's per-second `status()` — Tuya breakers refuse the second
LAN session that arrives within ~1s of a closed one (`Err 905`). The
listener code is still in `app/raw_listeners.py` for reference but is
**not spawned** any more (see the `_ = select_listener_devices` no-op
inside `lifespan`). Don't re-enable it unless you're rewriting the
piggyback approach; you'll fight the LAN socket again.

## 5. LIVE endpoint (`/api/devices/{device_id}/live`)

This is what `static/device.js` polls every 5s on the device page.

- For **listener-owned** devices (trick678): no LAN traffic — pure dict
  merge of `live_samples[device_id]` (status() fresh from poll loop) with
  `raw_dps_latest[device_id]` (phase bytes pushed by poll loop). Total
  latency is milliseconds.
- For other devices: still does an on-demand `build_live_sample` (which
  includes the older `_merge_missing_visualized_codes_once` trick678
  fallback — kept for devices without a profile request_mode hint).
- Phase packet bytes are decoded in the **frontend** by
  `_decode_phase_packet_parts` in `app/web.py` (server-side, then sent as
  pre-decoded I/U/P parts in `live_metrics`) — JS just renders them.

## 5a. Active-pusher devices and `/api/ingest/{device_id}`

Some gear can't be LAN-polled — the user's DC-current sensor on the MPPT
output, ESP32 home-rolled meters, things on the wrong subnet. Those POST
their readings to us instead.

- Profile JSON sets `connection.ingest_token` (any string, unique across
  the project — there's a unique partial index). `local_key`/`local_ip`
  stay empty so `get_polling_devices` filters the device out and the LAN
  loop never touches it.
- The endpoint expects a **JSON array** of `{current_a, captured_at, raw_dps?}`
  items. `current_a` only — voltage is resolved server-side once per
  request from the battery monitor's latest in-memory live sample
  (`_resolve_battery_voltage`, dp 20 / 100 of the device that has a
  `state_of_charge` capability). Power saved as
  `current_a * voltage_v * power_correction_factor`.
- Auth: `X-Ingest-Token` header. The path `/api/ingest/` is in
  `PUBLIC_PATH_PREFIXES` so the cookie middleware lets it through; the
  endpoint then enforces the token. Wrong token → 403. Missing battery
  monitor reading → 503 (don't silently substitute a nominal voltage).
- `save_samples_batch` swallows `LockNotAvailable` and
  `InvalidParameterValue` on the cagg refresh so a concurrent poll-loop
  refresh or a one-bucket-wide monthly window never poisons the
  endpoint's response. The sample is already committed by then; the cagg
  catches up on the next write.
- Pushed devices ride past the `hide-offline` dashboard toggle: the
  payload carries `is_pushed=true`, the JS adds `data-device-pushed` on
  the card, and the CSS rule excludes them via `:not([data-device-pushed])`.
  Solar that quiets out for the night should still leave its card
  visible.

## 6. Frontend

- `templates/index.html` (main page): 5-card hero row (month energy,
  estimated cost, **monthly solar (implicit)**, devices online,
  **undercharge**), device grid (for
  `is_energy_meter`), sensor grid. Bottom of the page there's a
  collapsible `<details>` "Учёт показаний счётчиков" — that's the manual
  meter reading form + status table + history table. `static/dashboard.js`
  polls `/api/summary` every 1s for the dashboard, and `/api/meter-readings`
  whenever the user saves/deletes.
- `templates/device.html` (per-device page): chart toolbar with
  day/week/month/year + custom range, ECharts chart, summary panel,
  functions panel (toggle, timer). `static/device.js` (~1000 lines) handles
  it all. Two chart modes:
  - **Bars** (default): hourly bars for `period=day`, daily/monthly
    aggregates otherwise. Comes from `samples_*` continuous aggregates via
    `_build_chart_series_from_aggregate`.
  - **Charger line + sessions** (when `device.is_charger` AND
    `power_type='current'` AND period is one day): line chart of
    instantaneous power from raw samples + below it a session breakdown
    (start, end, duration, energy, avg). Built by `get_charger_day_stats`
    in `app/storage.py`. Sessions are split on idle gaps > 5 min.
- Live data updates on the device page run at **5s**, not 1s (see
  `LIVE_REFRESH_INTERVAL_MS` in `static/device.js`). The poll loop's
  `status() + piggyback probes` cycle for trick678 devices is the
  slowest path; matching the UI to that cadence keeps things honest.
- **Unit display convention** (the user is on a sub-kW two-panel setup):
  every place that shows **instantaneous power** renders in **Вт**.
  **Accumulated energy** stays in **кВт·ч** (day_energy, session
  energy, month totals). `static/device.js::renderLineChart` rewrites
  `chartConfig.unit === 'кВт'` → `'Вт'` and × 1000 the series at the
  display boundary; the dashboard's generator card and the report bars
  do the same. Don't ship raw `power_kw` to a chart without that
  scaling.

## 7. Charger view specifics

- `is_charger` on the profile flips the chart between bars and a line+sessions view.
- The line chart series is the raw `samples.power_w` for the day, plotted as a step-end line with subtle area fill. ECharts dataZoom (toolbox + slider + inside) is wired so the user can drag-select a region to zoom — needed because charging sessions usually only occupy a slice of the 24h window.
- Session detection thresholds in `app/storage.py`:
  - `CHARGER_IDLE_THRESHOLD_W = 50.0` (anything below this counts as idle)
  - `CHARGER_SESSION_GAP_SECONDS = 300.0` (idle gap > 5 min = new session)
  - `CHARGER_SAMPLE_GAP_SECONDS = 60.0` (trapezoid skips intervals wider than this so a Wi-Fi dropout doesn't inflate energy)
- **Session energy** comes from `add_ele` telescoping (Tuya's own internal counter) when the device exposes it, **not** the trapezoidal integral of `cur_power`. Atorch on the battery side reads the same physical quantity so the two numbers agree to within rounding. See the override loop in `get_charger_day_stats` and `_counter_value_at_or_before`.
- Total-type chargers (Tesla) fall back to bars — the counter doesn't give enough resolution for a smooth line.

## 7a. Monthly report (`/report`)

`monthly_report` view (`app/web.py`) renders `templates/report.html`.
Shows a stacked daily breakdown for the selected month, a per-month chart
for the year, prev-month delta, 12-month average, prepaid undercharge,
and an "implicit solar" estimate.

Implicit solar = `max(0, Atorch dp 127 discharged_kWh - sum(wall charger energy))`
per UTC-aligned bucket, computed in `storage.get_implicit_solar_by_bucket`.
Battery monitor (the device with a `state_of_charge` capability) provides
the discharge integral via Δdp127 × measured dp 20 voltage on each
interval; chargers are summed from `samples_daily/_monthly`. Conservative
(ignores charger and inverter efficiencies) — the number is **a lower
bound** until a direct meter sits on the MPPT line.

## 8. Automations (`app/automations/`)

Cron-driven automation framework. Subclasses of `BaseAutomation` declare
`slug`, `name`, `device_type`, `default_cron`, `default_config`, and a
single async `run(ctx)`. They're registered into a module-level `REGISTRY`
via the `@register` decorator at import time; `__init__.py` imports each
file so registration happens at app start. Rows in the `automations`
table store the user-tunable parts: `bound_device_id`, `cron_schedule`,
`enabled`, `config_json`, plus `last_run_at`/`last_run_status`/`last_run_log`.

Scheduler (`app/scheduler.py`):
- 30s tick loop started in `lifespan`.
- For each row: croniter computes `next_run_at`, when fired the runner
  builds an `AutomationContext` (config, bound_device_id, config_json,
  `invoke_device_function` callback) and awaits `run(ctx)`.
- `invoke_device_function` retries the LAN dispatch up to 3 times with
  1.5s/3.0s back-off — devices occasionally drop the first packet right
  after waking up the relay.
- In-memory `app.state.automation_running_slugs` set prevents a slow
  cron-fire from doubling up if it spans the next tick.

Registered automations as of June 2026:
- `charger-sunrise` (default `0 2 * * *`): pulls weather forecast from
  open-meteo, computes expected solar Wh vs expected daily load Wh,
  sets a target SoC accordingly and runs the charger until reached or
  timeout.
- `battery-emergency-charge` (default `*/10 * * * *`): polls battery SoC;
  if below `emergency_threshold_soc` (default 30 %), turns the charger
  on and holds until SoC ≥ `target_soc_after_emergency` (default 40 %)
  or duration cap (default 3 h). Hysteresis prevents relay chatter.

The function code (`switch` vs `switch_1`) used by both is auto-resolved
from the bound device's capabilities — `resolve_switch_function_code`
in `app/automations/base.py`. Don't hardcode the legacy `switch_1` again.

UI: `templates/automations.html` lists them with run-now / bind-device /
toggle-enabled / cron-edit controls. Endpoints under
`/api/automations/...`.

## 9. Meter readings ledger

- Independent of Tuya. Two physical meters, apartments "2" and "3", combined billing with 250 kWh prepaid quota per settlement period (`METER_PREPAID_KWH` in `app/storage.py`).
- One row per (apartment, reading_date) in `meter_readings`. The "settlement" flag marks a reading as the new baseline. The combined undercharge is `(sum of (latest − settlement) across apartments) − 250` clamped to ≥0, converted to currency by `config.tariff_per_kwh`.
- Surfaced as a stat card in the hero row (currency, same format as estimated cost) and a collapsible status table + form below the device grid. JS lives at the bottom of `static/dashboard.js`.

## 10. Auth

Cookie-based session via `starlette.middleware.sessions`. The
`AuthGateMiddleware` (`app/web.py`) lets LAN traffic through unauthenticated
(based on `LOCAL_DISCOVERY_SUBNETS` and `X-Forwarded-For` from the
Cloudflare tunnel) and requires login for anyone coming from the public
hostname. Registration is permitted only from the LAN. Passwords are
bcrypt-hashed via `app_users` table (migration 002).

## 11. Operational notes you'll hit

- The poll loop logs noisy `RuntimeError: Device X is offline` for every
  off-LAN device, every second. That's expected — the user knows.
- `_is_tuya_host_reachable` uses a 0.15s TCP-connect probe across 3 known
  Tuya ports (6668/6669/7000). When the device's WiFi access point
  changes, the **local_key rotates server-side** even without unpairing —
  symptom is suddenly `Err 914 — Check device key or version`. Fix by
  pulling the new key from Tuya Cloud and updating
  `device_connections.local_key` in the DB (changes take effect on next
  poll iteration; no restart needed). There is a project memory note for
  this; see `MEMORY.md`.
- The dashboard JS polls `/api/summary` every 1 second; the device page
  polls `/api/devices/.../live` every 5 seconds (purposely slower for
  trick678 devices — see §4.1).
- "Текущая нагрузка дома" stat was removed. For counter-type devices we
  intentionally don't display instantaneous power on the main page; users
  click into the device page if they need it.
- For `power_type='current'` devices, `samples.power_w` is instantaneous
  watts. For `power_type='total'`, it is the **accumulated counter** value
  divided by `total_power_scale` (so units = kWh, not watts). Be careful
  not to mix them when computing instantaneous power.

## 12. Tools that exist but are off in prod

- `app/raw_listeners.py` — see §4.2. Imports kept so flipping the
  feature back on is one line.
- `app/device_registry.py` + `connect-device` page — onboarding wizard
  that pulls a device from Tuya Cloud, picks a power-DPS, writes a
  profile JSON. Used rarely; profiles are normally edited by hand.
- `app/tuya_model.py` — diagnostic-only schema for cloud artifacts.
  Doesn't drive runtime semantics.

## 13. Deploy cheat sheet

```bash
# from your workstation, in repo root
git add ... && git commit -m "..."
git push
ssh powermeter@192.168.1.107 "cd power-meter && bash deploy/server-update.sh"
```

Force-push only after explicit "yes" from the user. The Linux clone has
`git pull --ff-only` inside the deploy script; if you rewrote history,
the script aborts. Recover via
`git fetch origin main && git reset --hard origin/main` on the server,
then re-run `server-update.sh`.

DB inspection from your workstation:

```bash
python -c "import psycopg; conn = psycopg.connect('postgresql://power_meter:<PWD>@192.168.1.107:5433/home_power_meter'); ..."
```

The full DSN is in the project's `.env` on the Linux host (`/home/powermeter/power-meter/.env`).
