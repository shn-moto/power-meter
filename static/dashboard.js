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
    const summaryMeta = meterSection.querySelector('[data-meter-summary-meta]');
    const heroUnderpayment = document.querySelector('[data-summary-meter-underpayment]');

    const fmtKwhCell = (v) => v === null || v === undefined ? '—' : Number(v).toFixed(2);
    const fmtKwhFull = (v) => v === null || v === undefined ? '—' : `${Number(v).toFixed(2)} кВт·ч`;

    const renderStatusTable = (meter) => {
        if (!meter || !statusContainer) return;
        const apts = meter.status?.apartments || [];
        const aptCount = apts.length;
        const colHeaders = apts.map((apt) => `<th>Кв ${apt.apartment}</th>`).join('');
        const settlementCells = apts.map((apt) => {
            if (!apt.settlement) return '<td>—</td>';
            return `<td>${fmtKwhCell(apt.settlement.reading_kwh)} <small>(${apt.settlement.reading_date})</small></td>`;
        }).join('');
        const latestCells = apts.map((apt) => {
            if (!apt.latest) return '<td>—</td>';
            return `<td>${fmtKwhCell(apt.latest.reading_kwh)} <small>(${apt.latest.reading_date})</small></td>`;
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
                <td>${p.start_date} – ${p.end_date}</td>
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
        const dateEl = form.querySelector('[name="reading_date"]');
        const settlementEl = form.querySelector('[name="is_settlement"]');
        const dateValue = (dateEl?.value || '').trim();
        if (!dateValue) return [];
        const isSettlement = !!settlementEl?.checked;
        const rows = [];
        form.querySelectorAll('[data-meter-row]').forEach((wrapper) => {
            const apt = wrapper.dataset.apt;
            const valueEl = wrapper.querySelector('[name="reading_kwh"]');
            const valueRaw = (valueEl?.value || '').trim();
            if (!valueRaw) return;
            rows.push({
                apartment: apt,
                reading_date: dateValue,
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

    // Pre-fill date input with today
    const dateInput = meterSection.querySelector('[name="reading_date"]');
    if (dateInput && !dateInput.value) {
        const today = new Date();
        const yyyy = today.getFullYear();
        const mm = String(today.getMonth() + 1).padStart(2, '0');
        const dd = String(today.getDate()).padStart(2, '0');
        dateInput.value = `${yyyy}-${mm}-${dd}`;
    }
}