const dashboardPage = document.querySelector('[data-dashboard]');

if (dashboardPage) {
    const DASHBOARD_REFRESH_INTERVAL_MS = 1000;
    const monthEnergy = dashboardPage.querySelector('[data-summary-month-energy]');
    const estimatedCost = dashboardPage.querySelector('[data-summary-estimated-cost]');
    const deviceCount = dashboardPage.querySelector('[data-summary-device-count]');
    const deviceGrid = document.querySelector('[data-device-grid]');
    const generatorGrid = document.querySelector('[data-generator-grid]');
    const generatorSection = generatorGrid?.closest('.generator-section') || null;
    const sensorPanel = document.querySelector('[data-sensor-panel]');
    const sensorGrid = document.querySelector('[data-sensor-grid]');
    let isDashboardLoading = false;
    let dashboardTimerId = null;
    let dashboardAbortController = null;
    let dashboardPollingStopped = false;

    const escapeHtml = (value) => String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');

    const applyReadingStatus = (node, status) => {
        node.classList.remove('is-ok', 'is-warning', 'is-error');
        node.classList.add('reading-status', `is-${status || 'error'}`);
    };

    const renderDeviceMedia = (device) => {
        if (device.image_url) {
            return `<img class="device-photo" src="${escapeHtml(device.image_url)}" alt="${escapeHtml(device.name)}">`;
        }
        return `<div class="device-placeholder device-placeholder-${escapeHtml(device.device_kind || 'switch')}" aria-hidden="true"></div>`;
    };

    const fmtWatts = (kw) => (kw === undefined || kw === null) ? '—' : Math.round(Number(kw) * 1000);
    const fmtWh = (kwh) => Math.round(Number(kwh || 0) * 1000);

    const postSolarConsumer = async (deviceId, enabled) => {
        const url = `/api/devices/${encodeURIComponent(deviceId)}/solar-consumer`;
        const body = JSON.stringify({ enabled });
        const opts = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, cache: 'no-store' };
        let lastError;
        for (let attempt = 0; attempt < 2; attempt += 1) {
            try {
                const response = await fetch(url, opts);
                if (response.ok) return response;
                const detail = (await response.json().catch(() => ({}))).detail || `HTTP ${response.status}`;
                throw new Error(detail);
            } catch (err) {
                lastError = err;
                if (attempt === 0) {
                    await new Promise((r) => setTimeout(r, 350));
                }
            }
        }
        throw lastError;
    };

    // Devices the user toggled but for which the server-side state hasn't
    // propagated through the next poll yet. Stops the 1-second refresh from
    // briefly flicking the checkbox back to the stale value.
    const pendingSolarToggles = new Map(); // deviceId -> {expected: bool, releaseAt: ms}
    const PENDING_TOGGLE_LOCK_MS = 4000;

    const enforcePendingSolarToggles = () => {
        const now = Date.now();
        pendingSolarToggles.forEach((entry, deviceId) => {
            if (entry.releaseAt < now) {
                pendingSolarToggles.delete(deviceId);
                return;
            }
            const card = deviceGrid?.querySelector(`[data-device-card][data-device-id="${CSS.escape(deviceId)}"]`);
            const cb = card?.querySelector('[data-solar-consumer-toggle]');
            if (cb && cb.checked !== entry.expected) {
                cb.checked = entry.expected;
            }
        });
    };

    deviceGrid?.addEventListener('change', async (event) => {
        const input = event.target;
        if (!(input instanceof HTMLInputElement) || !input.matches('[data-solar-consumer-toggle]')) {
            return;
        }
        const card = input.closest('[data-device-card]');
        const deviceId = card?.dataset.deviceId;
        if (!deviceId) return;
        const next = input.checked;
        pendingSolarToggles.set(deviceId, { expected: next, releaseAt: Date.now() + PENDING_TOGGLE_LOCK_MS });
        input.disabled = true;
        try {
            await postSolarConsumer(deviceId, next);
            // Server confirmed; let the next poll converge but keep a short
            // lock so we don't blink during the cache-warmup window.
            pendingSolarToggles.set(deviceId, { expected: next, releaseAt: Date.now() + 1500 });
        } catch (err) {
            pendingSolarToggles.delete(deviceId);
            input.checked = !next;
            window.alert(`Не удалось обновить ☀: ${err.message || err}`);
        } finally {
            input.disabled = false;
        }
    });

    const renderCardMarkup = (device) => {
        const currentPowerRow = (device.current_power_kw !== undefined && device.current_power_kw !== null)
            ? `<div>
                    <dt>Текущая мощность</dt>
                    <dd data-device-current-power>${fmtWatts(device.current_power_kw)} Вт</dd>
                </div>
                <div>
                    <dt>Энергия за день</dt>
                    <dd data-device-day-energy>${fmtWh(device.day_energy_kwh)} Вт·ч</dd>
                </div>`
            : '';
        return `
        <a class="device-image" href="/devices/${encodeURIComponent(device.device_id)}" aria-label="Открыть детали устройства ${escapeHtml(device.name)}">
            ${renderDeviceMedia(device)}
        </a>
        <div class="device-card-body">
            <div class="device-card-head">
                <div>
                    <p class="device-room" data-device-room>${escapeHtml(device.room)}</p>
                    <h3 data-device-name>${escapeHtml(device.name)}</h3>
                </div>
                <label class="solar-consumer-toggle" title="Потребляет солнечную энергию">
                    <input type="checkbox" data-solar-consumer-toggle${device.is_solar_consumer ? ' checked' : ''}>
                    <span>☀</span>
                </label>
            </div>
            <dl class="device-metrics">
                ${currentPowerRow}
                <div>
                    <dt>Энергия за месяц</dt>
                    <dd data-device-month-energy>${escapeHtml(device.month_energy_kwh)} кВт·ч</dd>
                </div>
                <div>
                    <dt>Последний замер</dt>
                    <dd class="reading-status is-${escapeHtml(device.last_seen_status || 'error')}" data-device-last-seen title="${escapeHtml(device.last_seen || 'Пока нет данных')}">${escapeHtml(device.last_seen || 'Пока нет данных')}</dd>
                </div>
            </dl>
        </div>
    `;
    };

    const renderSensorMarkup = (device) => `
        <a class="sensor-summary-media" href="/devices/${encodeURIComponent(device.device_id)}" aria-label="Открыть страницу датчика ${escapeHtml(device.name)}">
            ${renderDeviceMedia(device)}
        </a>
        <div class="sensor-summary-body">
            <div>
                <p class="device-room">${escapeHtml(device.room)}</p>
                <h3><a class="sensor-link" href="/devices/${encodeURIComponent(device.device_id)}">${escapeHtml(device.name)}</a></h3>
            </div>
            <dl class="sensor-summary-metrics">
                <div>
                    <dt>${escapeHtml(device.primary_metric?.label || 'Состояние')}</dt>
                    <dd data-sensor-primary>${escapeHtml(device.primary_metric?.value || 'Нет данных')}</dd>
                </div>
                <div>
                    <dt>${escapeHtml(device.secondary_metric?.label || 'Источник')}</dt>
                    <dd data-sensor-secondary>${escapeHtml(device.secondary_metric?.value || device.connection_label || 'Нет данных')}</dd>
                </div>
                ${device.tertiary_metric ? `
                <div>
                    <dt>${escapeHtml(device.tertiary_metric.label)}</dt>
                    <dd data-sensor-tertiary>${escapeHtml(device.tertiary_metric.value)}</dd>
                </div>` : ''}
            </dl>
        </div>
        <div class="registry-meta sensor-summary-meta">
            <span data-sensor-connection>${escapeHtml(device.connection_label || 'Облачное устройство')}</span>
            <span class="reading-status is-${escapeHtml(device.last_seen_status || 'error')}" data-sensor-last-seen title="${escapeHtml(device.last_seen || 'Пока нет данных')}">${escapeHtml(device.last_seen || 'Пока нет данных')}</span>
        </div>
    `;

    const applyCardStatus = (card, device) => {
        card.dataset.lastSeenStatus = device.last_seen_status || 'error';
    };

    const updateCard = (card, device) => {
        card.innerHTML = renderCardMarkup(device);
        applyCardStatus(card, device);
    };

    const syncSensorDevices = (devices) => {
        if (!sensorPanel || !sensorGrid) {
            return;
        }

        sensorPanel.hidden = devices.length === 0;
        const existingCards = new Map(
            [...sensorGrid.querySelectorAll('[data-sensor-card]')].map((card) => [card.dataset.deviceId, card])
        );

        if (existingCards.size !== devices.length || devices.some((device) => !existingCards.has(device.device_id))) {
            sensorGrid.innerHTML = '';
            devices.forEach((device) => {
                const card = document.createElement('article');
                card.className = 'registry-item sensor-summary-card';
                card.dataset.sensorCard = '';
                card.dataset.deviceId = device.device_id;
                card.innerHTML = renderSensorMarkup(device);
                applyCardStatus(card, device);
                sensorGrid.appendChild(card);
            });
            return;
        }

        devices.forEach((device) => {
            const card = existingCards.get(device.device_id);
            if (card) {
                card.innerHTML = renderSensorMarkup(device);
                applyCardStatus(card, device);
            }
        });
    };

    const syncDevices = (devices) => {
        const existingCards = new Map(
            [...deviceGrid.querySelectorAll('[data-device-card]')].map((card) => [card.dataset.deviceId, card])
        );

        if (existingCards.size !== devices.length || devices.some((device) => !existingCards.has(device.device_id))) {
            deviceGrid.innerHTML = '';
            devices.forEach((device) => {
                const card = document.createElement('article');
                card.className = 'device-card';
                card.dataset.deviceCard = '';
                card.dataset.deviceId = device.device_id;
                card.innerHTML = renderCardMarkup(device);
                applyCardStatus(card, device);
                deviceGrid.appendChild(card);
            });
            return;
        }

        devices.forEach((device) => updateCard(existingCards.get(device.device_id), device));
    };

    const renderGeneratorMarkup = (device) => `
        <a class="device-image generator-card-media" href="/devices/${encodeURIComponent(device.device_id)}" aria-label="Открыть детали устройства ${escapeHtml(device.name)}">
            ${renderDeviceMedia(device)}
        </a>
        <div class="device-card-body generator-card-body">
            <div class="generator-card-head">
                <div>
                    <p class="device-room" data-device-room>${escapeHtml(device.room)}</p>
                    <h3 data-device-name>${escapeHtml(device.name)}</h3>
                </div>
                <div class="generator-card-meta">
                    <div class="generator-current-power" data-device-current-power>${fmtWatts(device.current_power_kw)} Вт</div>
                    <div class="generator-month-energy" data-device-day-energy>${fmtWh(device.day_energy_kwh)} Вт·ч / день</div>
                    <div class="reading-status is-${escapeHtml(device.last_seen_status || 'error')}" data-device-last-seen title="${escapeHtml(device.last_seen || 'Пока нет данных')}">${escapeHtml(device.last_seen || 'Пока нет данных')}</div>
                </div>
            </div>
            <div class="generator-chart" data-generator-chart></div>
        </div>
    `;

    const generatorChartInstances = new Map();
    const generatorChartFetchedAt = new Map();
    const GENERATOR_TRACE_REFRESH_MS = 30000;

    const generatorChartData = new Map(); // device_id -> {gen, cons, surplus} sorted arrays

    const nearestValue = (sorted, ts) => {
        if (!sorted || !sorted.length) return null;
        let lo = 0, hi = sorted.length - 1;
        while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (sorted[mid][0] < ts) lo = mid + 1; else hi = mid;
        }
        const cand = sorted[lo];
        if (lo > 0 && Math.abs(sorted[lo - 1][0] - ts) < Math.abs(cand[0] - ts)) {
            return sorted[lo - 1][1];
        }
        return cand[1];
    };

    const initGeneratorChart = (card, deviceId) => {
        const container = card.querySelector('[data-generator-chart]');
        if (!container || !window.echarts) {
            return null;
        }
        const existing = generatorChartInstances.get(deviceId);
        if (existing) {
            try { existing.dispose(); } catch (_) {}
        }
        const chart = window.echarts.init(container);
        chart.setOption({
            grid: { left: 8, right: 8, top: 6, bottom: 6, containLabel: false },
            animation: false,
            tooltip: {
                trigger: 'axis',
                backgroundColor: 'rgba(11, 18, 26, 0.92)',
                borderColor: 'rgba(127, 208, 255, 0.22)',
                textStyle: { color: '#e8f0fa' },
                formatter: (params) => {
                    if (!params || !params.length) return '';
                    const ts = params[0].axisValue;
                    const t = new Date(ts);
                    const hh = String(t.getHours()).padStart(2, '0');
                    const mm = String(t.getMinutes()).padStart(2, '0');
                    const store = generatorChartData.get(deviceId) || {};
                    const gen = nearestValue(store.gen, ts) ?? 0;
                    const cons = nearestValue(store.cons, ts) ?? 0;
                    const surplus = Math.max(gen - cons, 0);
                    return [
                        `<strong>${hh}:${mm}</strong>`,
                        `<span style="color:#67b86b">●</span> Генерация: ${gen.toFixed(3)} кВт`,
                        `<span style="color:#e8a838">●</span> Потребление: ${cons.toFixed(3)} кВт`,
                        `<span style="color:#f04848">●</span> Профицит: ${surplus.toFixed(3)} кВт`,
                    ].join('<br/>');
                },
            },
            xAxis: { type: 'time', show: false },
            yAxis: { type: 'value', show: false },
            series: [
                {
                    name: 'Генерация',
                    type: 'line', smooth: true, symbol: 'none',
                    lineStyle: { color: '#67b86b', width: 2 },
                    areaStyle: { color: 'rgba(103, 184, 107, 0.22)' },
                    data: [],
                },
                {
                    name: 'Потребление',
                    type: 'line', smooth: true, symbol: 'none',
                    lineStyle: { color: '#e8a838', width: 1.4 },
                    areaStyle: { color: 'rgba(232, 168, 56, 0.18)' },
                    data: [],
                },
                {
                    name: 'Профицит',
                    type: 'line', smooth: true, symbol: 'none',
                    lineStyle: { color: '#f04848', width: 1.6 },
                    areaStyle: { color: 'rgba(240, 72, 72, 0.22)', origin: 0 },
                    data: [],
                },
            ],
        });
        generatorChartInstances.set(deviceId, chart);
        return chart;
    };

    const refreshGeneratorTrace = async (deviceId, force = false) => {
        const chart = generatorChartInstances.get(deviceId);
        if (!chart) return;
        const last = generatorChartFetchedAt.get(deviceId) || 0;
        if (!force && (Date.now() - last) < GENERATOR_TRACE_REFRESH_MS) {
            return;
        }
        generatorChartFetchedAt.set(deviceId, Date.now());
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(deviceId)}/power-trace?minutes=60`, { cache: 'no-store' });
            if (!r.ok) return;
            const data = await r.json();
            const genPoints = (data.series || []).map((p) => [Date.parse(p.timestamp), Number(p.power_kw || 0)]);
            const consMap = new Map();
            (data.consumers_series || []).forEach((p) => consMap.set(Date.parse(p.timestamp), Number(p.power_kw || 0)));
            // For consumption, plot as NEGATIVE so it goes below zero on the same axis.
            const consPoints = (data.consumers_series || []).map((p) => [Date.parse(p.timestamp), -Number(p.power_kw || 0)]);
            // Surplus: where generation exceeds consumption at that bucket.
            // Use nearest consumer point (within 60s) for each generator point.
            const consSorted = Array.from(consMap.entries()).sort((a, b) => a[0] - b[0]);
            const findNearest = (ts) => {
                // binary search since consSorted is sorted by ts
                if (!consSorted.length) return 0;
                let lo = 0, hi = consSorted.length - 1;
                while (lo < hi) {
                    const mid = (lo + hi) >> 1;
                    if (consSorted[mid][0] < ts) lo = mid + 1; else hi = mid;
                }
                const cand = consSorted[lo];
                if (lo > 0 && Math.abs(consSorted[lo - 1][0] - ts) < Math.abs(cand[0] - ts)) {
                    return consSorted[lo - 1][1];
                }
                return cand[1];
            };
            const surplusPoints = genPoints.map(([ts, gen]) => {
                const cons = findNearest(ts);
                const diff = gen - cons;
                return [ts, diff > 0 ? diff : null];
            });
            // Stash sorted-by-x copies for the tooltip lookup so every series
            // shows a value at whichever timestamp the cursor lands on.
            const consAbs = consPoints.map(([ts, v]) => [ts, Math.abs(v)]);
            generatorChartData.set(deviceId, {
                gen: [...genPoints].sort((a, b) => a[0] - b[0]),
                cons: consAbs.sort((a, b) => a[0] - b[0]),
                surplus: surplusPoints.filter((p) => p[1] !== null).sort((a, b) => a[0] - b[0]),
            });
            chart.setOption({
                series: [
                    { data: genPoints },
                    { data: consPoints },
                    { data: surplusPoints },
                ],
            });
        } catch (_) { /* swallow */ }
    };

    const syncGeneratorDevices = (devices) => {
        if (!generatorGrid) {
            return;
        }
        if (generatorSection) {
            generatorSection.hidden = devices.length === 0;
        }
        if (!devices.length) {
            // Tear down any chart instances
            generatorChartInstances.forEach((chart) => { try { chart.dispose(); } catch (_) {} });
            generatorChartInstances.clear();
            generatorChartData.clear();
            generatorChartFetchedAt.clear();
            generatorGrid.innerHTML = '';
            return;
        }
        const existingCards = new Map(
            [...generatorGrid.querySelectorAll('[data-generator-card]')].map((card) => [card.dataset.deviceId, card])
        );
        const rebuildAll =
            existingCards.size !== devices.length ||
            devices.some((device) => !existingCards.has(device.device_id));
        if (rebuildAll) {
            generatorChartInstances.forEach((chart) => { try { chart.dispose(); } catch (_) {} });
            generatorChartInstances.clear();
            generatorChartData.clear();
            generatorGrid.innerHTML = '';
            devices.forEach((device) => {
                const card = document.createElement('article');
                card.className = 'device-card generator-card';
                card.dataset.generatorCard = '';
                card.dataset.deviceId = device.device_id;
                card.innerHTML = renderGeneratorMarkup(device);
                applyCardStatus(card, device);
                generatorGrid.appendChild(card);
                initGeneratorChart(card, device.device_id);
                refreshGeneratorTrace(device.device_id, true);
            });
            return;
        }
        devices.forEach((device) => {
            const card = existingCards.get(device.device_id);
            if (!card) return;
            // Only update the text cells so the chart canvas stays alive.
            const room = card.querySelector('[data-device-room]');
            if (room) room.textContent = device.room || '';
            const nameEl = card.querySelector('[data-device-name]');
            if (nameEl) nameEl.textContent = device.name || '';
            const cp = card.querySelector('[data-device-current-power]');
            if (cp) cp.textContent = `${fmtWatts(device.current_power_kw)} Вт`;
            const de = card.querySelector('[data-device-day-energy]');
            if (de) de.textContent = `${fmtWh(device.day_energy_kwh)} Вт·ч / день`;
            const ls = card.querySelector('[data-device-last-seen]');
            if (ls) {
                ls.textContent = device.last_seen || 'Пока нет данных';
                ls.title = device.last_seen || 'Пока нет данных';
                ls.classList.remove('is-ok', 'is-warning', 'is-error');
                ls.classList.add(`is-${device.last_seen_status || 'error'}`);
            }
            applyCardStatus(card, device);
            // Make sure the chart still exists (e.g. after a tab visibility swap)
            if (!generatorChartInstances.has(device.device_id)) {
                initGeneratorChart(card, device.device_id);
                refreshGeneratorTrace(device.device_id, true);
            } else {
                refreshGeneratorTrace(device.device_id, false);
            }
        });
    };

    const hideOfflineToggle = document.querySelector('[data-hide-offline]');
    const HIDE_OFFLINE_KEY = 'powermeter:hideOfflineDevices';
    const applyHideOfflineState = (enabled) => {
        document.body.classList.toggle('hide-offline', !!enabled);
    };
    if (hideOfflineToggle) {
        const stored = window.localStorage?.getItem(HIDE_OFFLINE_KEY) === '1';
        hideOfflineToggle.checked = stored;
        applyHideOfflineState(stored);
        hideOfflineToggle.addEventListener('change', () => {
            applyHideOfflineState(hideOfflineToggle.checked);
            try {
                window.localStorage?.setItem(HIDE_OFFLINE_KEY, hideOfflineToggle.checked ? '1' : '0');
            } catch (_) {}
        });
    }

    const meterUnderpayment = dashboardPage.querySelector('[data-summary-meter-underpayment]');

    const applyDashboardPayload = (payload) => {
        monthEnergy.textContent = `${payload.month_energy_kwh} кВт·ч`;
        estimatedCost.textContent = `${payload.estimated_cost}`;
        deviceCount.textContent = `${payload.device_count}`;
        if (meterUnderpayment) {
            const upCost = payload.meter?.status?.underpayment_cost;
            meterUnderpayment.textContent = (upCost !== null && upCost !== undefined) ? Number(upCost).toFixed(2) : '—';
        }
        syncDevices(payload.devices || []);
        syncGeneratorDevices(payload.generator_devices || []);
        syncSensorDevices(payload.sensor_devices || []);
        enforcePendingSolarToggles();
    };

    const clearDashboardTimer = () => {
        if (dashboardTimerId) {
            clearInterval(dashboardTimerId);
            dashboardTimerId = null;
        }
    };

    const stopDashboardPolling = () => {
        dashboardPollingStopped = true;
        clearDashboardTimer();
        dashboardAbortController?.abort();
    };

    const ensureDashboardTimers = () => {
        if (dashboardPollingStopped) {
            return;
        }

        if (!dashboardTimerId) {
            dashboardTimerId = setInterval(() => {
                if (!document.hidden) {
                    loadDashboard();
                }
            }, DASHBOARD_REFRESH_INTERVAL_MS);
        }
    };

    const loadDashboard = async () => {
        if (isDashboardLoading || dashboardPollingStopped) {
            return;
        }
        isDashboardLoading = true;
        const controller = new AbortController();
        dashboardAbortController = controller;
        try {
            const response = await fetch('/api/summary', { cache: 'no-store', signal: controller.signal });
            if (!response.ok) {
                throw new Error(`Dashboard request failed: ${response.status}`);
            }
            const payload = await response.json();
            applyDashboardPayload(payload);
        } catch (error) {
            if (error?.name !== 'AbortError') {
                // Preserve the last rendered dashboard state if a refresh fails.
            }
        } finally {
            if (dashboardAbortController === controller) {
                dashboardAbortController = null;
            }
            isDashboardLoading = false;
        }
    };

    document.addEventListener('visibilitychange', () => {
        if (dashboardPollingStopped) {
            return;
        }
        if (document.hidden) {
            clearDashboardTimer();
            dashboardAbortController?.abort();
            return;
        }
        loadDashboard();
        ensureDashboardTimers();
    });

    window.addEventListener('pagehide', stopDashboardPolling);

    loadDashboard();
    ensureDashboardTimers();
}

const meterSection = document.querySelector('[data-meter-section]');
if (meterSection) {
    const form = meterSection.querySelector('[data-meter-form]');
    const formStatus = meterSection.querySelector('[data-meter-form-status]');
    const statusContainer = meterSection.querySelector('[data-meter-status]');
    const historyBody = meterSection.querySelector('[data-meter-history]');
    const summaryMeta = meterSection.querySelector('[data-meter-summary-meta]');
    const heroUnderpayment = document.querySelector('[data-summary-meter-underpayment]');

    const fmtKwhCell = (v) => v === null || v === undefined ? '—' : Number(v).toFixed(2);
    const fmtKwhFull = (v) => v === null || v === undefined ? '—' : `${Number(v).toFixed(2)} кВт·ч`;

    const renderStatusTable = (meter) => {
        if (!meter || !statusContainer) return;
        const apts = meter.status?.apartments || [];
        const aptCount = apts.length;
        const colHeaders = apts.map((apt) => `<th>Кв ${apt.apartment}</th>`).join('');
        const formatAt = (iso) => iso ? iso.slice(0, 16).replace('T', ' ') : '';
        const settlementCells = apts.map((apt) => {
            if (!apt.settlement) return '<td>—</td>';
            return `<td>${fmtKwhCell(apt.settlement.reading_kwh)} <small>(${formatAt(apt.settlement.reading_at)})</small></td>`;
        }).join('');
        const latestCells = apts.map((apt) => {
            if (!apt.latest) return '<td>—</td>';
            return `<td>${fmtKwhCell(apt.latest.reading_kwh)} <small>(${formatAt(apt.latest.reading_at)})</small></td>`;
        }).join('');
        const consumptionCells = apts.map((apt) => `<td>${fmtKwhCell(apt.consumption_kwh)}</td>`).join('');
        const totals = meter.status || {};
        const totalConsumption = totals.total_consumption_kwh !== null && totals.total_consumption_kwh !== undefined
            ? `<strong>${Number(totals.total_consumption_kwh).toFixed(2)}</strong>` : '—';
        const upKwh = totals.underpayment_kwh;
        const upCost = totals.underpayment_cost;
        const underpaymentCell = (upCost !== null && upCost !== undefined)
            ? `<strong>${Number(upCost).toFixed(2)}</strong> <small>(${Number(upKwh).toFixed(2)} кВт·ч)</small>`
            : '—';
        statusContainer.innerHTML = `<table class="meter-status-table">
            <thead>
                <tr><th></th>${colHeaders}<th>Сумма</th></tr>
            </thead>
            <tbody>
                <tr><th>Расчётное</th>${settlementCells}<td></td></tr>
                <tr><th>Последнее</th>${latestCells}<td></td></tr>
                <tr><th>Расход с расчётной</th>${consumptionCells}<td>${totalConsumption}</td></tr>
                <tr><th>Предоплачено</th><td colspan="${aptCount}"></td><td>${Number(meter.prepaid_kwh).toFixed(0)}</td></tr>
                <tr class="meter-row-underpayment"><th>Недоплата</th><td colspan="${aptCount}"></td><td>${underpaymentCell}</td></tr>
            </tbody>
        </table>`;
        if (summaryMeta) {
            summaryMeta.textContent = (upCost !== null && upCost !== undefined)
                ? `недоплата ${Number(upCost).toFixed(2)} (${Number(upKwh).toFixed(2)} кВт·ч)`
                : 'нет данных';
        }
        if (heroUnderpayment) {
            heroUnderpayment.textContent = (upCost !== null && upCost !== undefined)
                ? Number(upCost).toFixed(2)
                : '—';
        }
    };

    const formatReadingAt = (iso) => iso ? String(iso).slice(0, 16).replace('T', ' ') : '';
    const renderHistory = (rows) => {
        if (!historyBody) return;
        historyBody.innerHTML = (rows || []).map((row) => `
            <tr data-reading-id="${row.id}">
                <td>${formatReadingAt(row.reading_at)}</td>
                <td>${row.apartment}</td>
                <td>${Number(row.reading_kwh).toFixed(2)}</td>
                <td>${row.is_settlement ? '✓' : ''}</td>
                <td><button type="button" class="meter-row-delete" data-meter-delete="${row.id}" aria-label="Удалить">×</button></td>
            </tr>
        `).join('');
    };

    const discrepancyContainer = meterSection.querySelector('[data-meter-discrepancy]');
    const discrepancyBody = meterSection.querySelector('[data-meter-discrepancy-body]');

    const renderDiscrepancy = (periods) => {
        if (!discrepancyContainer || !discrepancyBody) return;
        if (!periods || !periods.length) {
            discrepancyContainer.hidden = true;
            discrepancyBody.innerHTML = '';
            return;
        }
        discrepancyContainer.hidden = false;
        discrepancyBody.innerHTML = periods.map((p) => {
            const delta = Number(p.delta_kwh);
            const cls = delta > 0 ? 'is-positive' : (delta < 0 ? 'is-negative' : '');
            const sign = delta > 0 ? '+' : '';
            return `<tr>
                <td>${formatReadingAt(p.start_at)} – ${formatReadingAt(p.end_at)}</td>
                <td>${Number(p.meter_kwh).toFixed(2)}</td>
                <td>${Number(p.device_kwh).toFixed(2)}</td>
                <td class="meter-discrepancy-delta ${cls}">${sign}${delta.toFixed(2)}</td>
            </tr>`;
        }).join('');
    };

    const refreshMeter = async () => {
        try {
            const response = await fetch('/api/meter-readings', { cache: 'no-store' });
            if (!response.ok) throw new Error('failed');
            const payload = await response.json();
            renderStatusTable(payload);
            renderHistory(payload.readings);
            renderDiscrepancy(payload.discrepancy_periods);
        } catch (error) {
            // silent
        }
    };

    const collectFormRows = () => {
        const dtEl = form.querySelector('[name="reading_at"]');
        const settlementEl = form.querySelector('[name="is_settlement"]');
        const dtValue = (dtEl?.value || '').trim();
        if (!dtValue) return [];
        const isSettlement = !!settlementEl?.checked;
        const rows = [];
        form.querySelectorAll('[data-meter-row]').forEach((wrapper) => {
            const apt = wrapper.dataset.apt;
            const valueEl = wrapper.querySelector('[name="reading_kwh"]');
            const valueRaw = (valueEl?.value || '').trim();
            if (!valueRaw) return;
            rows.push({
                apartment: apt,
                reading_at: dtValue,
                reading_kwh: Number(valueRaw),
                is_settlement: isSettlement,
            });
        });
        return rows;
    };

    const setFormStatus = (text, isError = false) => {
        if (!formStatus) return;
        formStatus.textContent = text || '';
        formStatus.classList.toggle('is-error', isError);
    };

    form?.addEventListener('submit', async (event) => {
        event.preventDefault();
        const rows = collectFormRows();
        if (!rows.length) {
            setFormStatus('Введите дату и хотя бы одно показание', true);
            return;
        }
        setFormStatus('Сохраняем…');
        try {
            for (const row of rows) {
                const response = await fetch('/api/meter-readings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(row),
                });
                if (!response.ok) {
                    const body = await response.json().catch(() => ({}));
                    throw new Error(body.detail || `HTTP ${response.status}`);
                }
            }
            setFormStatus('Сохранено');
            form.querySelectorAll('[name="reading_kwh"]').forEach((el) => { el.value = ''; });
            const settlementBox = form.querySelector('[name="is_settlement"]');
            if (settlementBox) settlementBox.checked = false;
            await refreshMeter();
            setTimeout(() => setFormStatus(''), 2000);
        } catch (error) {
            setFormStatus(`Ошибка: ${error.message}`, true);
        }
    });

    historyBody?.addEventListener('click', async (event) => {
        const btn = event.target.closest('[data-meter-delete]');
        if (!btn) return;
        const id = btn.dataset.meterDelete;
        if (!confirm('Удалить эту запись?')) return;
        try {
            const response = await fetch(`/api/meter-readings/${id}`, { method: 'DELETE' });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            await refreshMeter();
        } catch (error) {
            setFormStatus(`Ошибка удаления: ${error.message}`, true);
        }
    });

    // Pre-fill datetime-local input with NOW
    const dtInput = meterSection.querySelector('[name="reading_at"]');
    if (dtInput && !dtInput.value) {
        const now = new Date();
        const pad = (n) => String(n).padStart(2, '0');
        dtInput.value = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`;
    }
}