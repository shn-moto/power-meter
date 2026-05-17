const dashboardPage = document.querySelector('[data-dashboard]');

if (dashboardPage) {
    const DASHBOARD_REFRESH_INTERVAL_MS = 1000;
    const monthEnergy = dashboardPage.querySelector('[data-summary-month-energy]');
    const estimatedCost = dashboardPage.querySelector('[data-summary-estimated-cost]');
    const deviceCount = dashboardPage.querySelector('[data-summary-device-count]');
    const deviceGrid = document.querySelector('[data-device-grid]');
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

    const renderCardMarkup = (device) => {
        const currentPowerRow = (device.current_power_kw !== undefined && device.current_power_kw !== null)
            ? `<div>
                    <dt>Текущая мощность</dt>
                    <dd data-device-current-power>${escapeHtml(device.current_power_kw)} кВт</dd>
                </div>`
            : '';
        return `
        <a class="device-image" href="/devices/${encodeURIComponent(device.device_id)}" aria-label="Открыть детали устройства ${escapeHtml(device.name)}">
            ${renderDeviceMedia(device)}
        </a>
        <div class="device-card-body">
            <div>
                <p class="device-room" data-device-room>${escapeHtml(device.room)}</p>
                <h3 data-device-name>${escapeHtml(device.name)}</h3>
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
            </dl>
        </div>
        <div class="registry-meta sensor-summary-meta">
            <span data-sensor-connection>${escapeHtml(device.connection_label || 'Облачное устройство')}</span>
            <span class="reading-status is-${escapeHtml(device.last_seen_status || 'error')}" data-sensor-last-seen title="${escapeHtml(device.last_seen || 'Пока нет данных')}">${escapeHtml(device.last_seen || 'Пока нет данных')}</span>
        </div>
    `;

    const updateCard = (card, device) => {
        card.innerHTML = renderCardMarkup(device);
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
                sensorGrid.appendChild(card);
            });
            return;
        }

        devices.forEach((device) => {
            const card = existingCards.get(device.device_id);
            if (card) {
                card.innerHTML = renderSensorMarkup(device);
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
                deviceGrid.appendChild(card);
            });
            return;
        }

        devices.forEach((device) => updateCard(existingCards.get(device.device_id), device));
    };

    const applyDashboardPayload = (payload) => {
        monthEnergy.textContent = `${payload.month_energy_kwh} кВт·ч`;
        estimatedCost.textContent = `${payload.estimated_cost}`;
        deviceCount.textContent = `${payload.device_count}`;
        syncDevices(payload.devices || []);
        syncSensorDevices(payload.sensor_devices || []);
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

    const fmtKwh = (v) => v === null || v === undefined ? '—' : `${Number(v).toFixed(2)} кВт·ч`;
    const fmtKwhRaw = (v) => v === null || v === undefined ? '—' : Number(v).toFixed(2);

    const renderStatus = (meter) => {
        if (!meter || !statusContainer) return;
        const apts = (meter.status?.apartments || []);
        const aptParts = apts.map((apt) => {
            const settlement = apt.settlement
                ? `${fmtKwhRaw(apt.settlement.reading_kwh)} <small>(${apt.settlement.reading_date})</small>`
                : '—';
            const latest = apt.latest
                ? `${fmtKwhRaw(apt.latest.reading_kwh)} <small>(${apt.latest.reading_date})</small>`
                : '—';
            const consumption = apt.consumption_kwh !== null && apt.consumption_kwh !== undefined
                ? fmtKwh(apt.consumption_kwh)
                : '—';
            return `<div class="meter-apt">
                <h3>Квартира ${apt.apartment}</h3>
                <dl>
                    <div><dt>Расчётная</dt><dd>${settlement}</dd></div>
                    <div><dt>Последнее</dt><dd>${latest}</dd></div>
                    <div><dt>Расход с расчётной</dt><dd>${consumption}</dd></div>
                </dl>
            </div>`;
        });
        const totals = meter.status || {};
        const totalsHtml = `<div class="meter-totals" data-meter-totals>
            <dl>
                <div><dt>Суммарный расход</dt><dd>${totals.total_consumption_kwh !== null && totals.total_consumption_kwh !== undefined ? fmtKwh(totals.total_consumption_kwh) : '—'}</dd></div>
                <div><dt>Предоплачено</dt><dd>${Number(meter.prepaid_kwh).toFixed(0)} кВт·ч</dd></div>
                <div class="meter-underpayment"><dt>Недоплата</dt><dd>${totals.underpayment_kwh !== null && totals.underpayment_kwh !== undefined ? fmtKwh(totals.underpayment_kwh) : '—'}</dd></div>
            </dl>
        </div>`;
        statusContainer.innerHTML = aptParts.join('') + totalsHtml;
    };

    const renderHistory = (rows) => {
        if (!historyBody) return;
        historyBody.innerHTML = (rows || []).map((row) => `
            <tr data-reading-id="${row.id}">
                <td>${row.reading_date}</td>
                <td>${row.apartment}</td>
                <td>${Number(row.reading_kwh).toFixed(2)}</td>
                <td>${row.is_settlement ? '✓' : ''}</td>
                <td><button type="button" class="meter-row-delete" data-meter-delete="${row.id}" aria-label="Удалить">×</button></td>
            </tr>
        `).join('');
    };

    const refreshMeter = async () => {
        try {
            const response = await fetch('/api/meter-readings', { cache: 'no-store' });
            if (!response.ok) throw new Error('failed');
            const payload = await response.json();
            renderStatus(payload);
            renderHistory(payload.readings);
        } catch (error) {
            // silent
        }
    };

    const collectFormRows = () => {
        const rows = [];
        meterSection.querySelectorAll('[data-meter-row]').forEach((fieldset) => {
            const apt = fieldset.dataset.apt;
            const dateEl = fieldset.querySelector('[name="reading_date"]');
            const valueEl = fieldset.querySelector('[name="reading_kwh"]');
            const settlementEl = fieldset.querySelector('[name="is_settlement"]');
            const dateValue = (dateEl?.value || '').trim();
            const valueRaw = (valueEl?.value || '').trim();
            if (!dateValue || !valueRaw) return;
            rows.push({
                apartment: apt,
                reading_date: dateValue,
                reading_kwh: Number(valueRaw),
                is_settlement: !!settlementEl?.checked,
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
            setFormStatus('Заполните хотя бы одно показание', true);
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
            form.querySelectorAll('[name="is_settlement"]').forEach((el) => { el.checked = false; });
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

    // Pre-fill date inputs with today
    meterSection.querySelectorAll('[name="reading_date"]').forEach((el) => {
        if (!el.value) {
            const today = new Date();
            const yyyy = today.getFullYear();
            const mm = String(today.getMonth() + 1).padStart(2, '0');
            const dd = String(today.getDate()).padStart(2, '0');
            el.value = `${yyyy}-${mm}-${dd}`;
        }
    });
}