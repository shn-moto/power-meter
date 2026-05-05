const dashboardPage = document.querySelector('[data-dashboard]');

if (dashboardPage) {
    const DASHBOARD_REFRESH_INTERVAL_MS = 1000;
    const currentPower = dashboardPage.querySelector('[data-summary-current-power]');
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

    const renderCardMarkup = (device) => `
        <a class="device-image" href="/devices/${encodeURIComponent(device.device_id)}" aria-label="Открыть детали устройства ${escapeHtml(device.name)}">
            ${renderDeviceMedia(device)}
        </a>
        <div class="device-card-body">
            <div>
                <p class="device-room" data-device-room>${escapeHtml(device.room)}</p>
                <h3 data-device-name>${escapeHtml(device.name)}</h3>
            </div>
            <dl class="device-metrics">
                <div>
                    <dt>Текущая мощность</dt>
                    <dd data-device-current-power>${escapeHtml(device.current_power_kw)} кВт</dd>
                </div>
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
        currentPower.textContent = `${payload.current_power_kw} кВт`;
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