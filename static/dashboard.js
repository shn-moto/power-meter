const dashboardPage = document.querySelector('[data-dashboard]');

if (dashboardPage) {
    const DASHBOARD_REFRESH_INTERVAL_MS = 1000;
    const CLOCK_REFRESH_INTERVAL_MS = 1000;
    const currentPower = dashboardPage.querySelector('[data-summary-current-power]');
    const monthEnergy = dashboardPage.querySelector('[data-summary-month-energy]');
    const estimatedCost = dashboardPage.querySelector('[data-summary-estimated-cost]');
    const deviceCount = dashboardPage.querySelector('[data-summary-device-count]');
    const deviceGrid = document.querySelector('[data-device-grid]');
    const sensorPanel = document.querySelector('[data-sensor-panel]');
    const sensorGrid = document.querySelector('[data-sensor-grid]');
    let isDashboardLoading = false;
    let dashboardTimerId = null;
    let clockTimerId = null;
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

    const formatRelativeAgeLabel = (ageSeconds, fallback) => {
        if (!Number.isFinite(ageSeconds)) {
            return fallback || 'Пока нет данных';
        }

        const rounded = Math.max(0, Math.floor(ageSeconds));
        if (rounded <= 0) {
            return 'только что';
        }
        if (rounded < 60) {
            return `${rounded} сек назад`;
        }

        const minutes = Math.floor(rounded / 60);
        const seconds = rounded % 60;
        if (minutes < 60) {
            return seconds > 0
                ? `${minutes} мин ${seconds} сек назад`
                : `${minutes} мин назад`;
        }

        const hours = Math.floor(minutes / 60);
        const remainMinutes = minutes % 60;
        return remainMinutes > 0
            ? `${hours} ч ${remainMinutes} мин назад`
            : `${hours} ч назад`;
    };

    const buildRelativeTimeAttrs = (ageSeconds, fallback) => {
        const fallbackValue = escapeHtml(fallback || 'Пока нет данных');
        const ageValue = Number.isFinite(ageSeconds) ? String(Math.max(0, Math.floor(ageSeconds))) : '';
        return `data-relative-age-seconds="${escapeHtml(ageValue)}" data-relative-age-fallback="${fallbackValue}" title="${fallbackValue}"`;
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
                    <dd class="reading-status is-${escapeHtml(device.last_seen_status || 'error')}" data-device-last-seen ${buildRelativeTimeAttrs(device.last_seen_age_seconds, device.last_seen || 'Пока нет данных')}>${escapeHtml(formatRelativeAgeLabel(device.last_seen_age_seconds, device.last_seen || 'Пока нет данных'))}</dd>
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
            <span class="reading-status is-${escapeHtml(device.last_seen_status || 'error')}" data-sensor-last-seen ${buildRelativeTimeAttrs(device.last_seen_age_seconds, device.last_seen || 'Пока нет данных')}>${escapeHtml(formatRelativeAgeLabel(device.last_seen_age_seconds, device.last_seen || 'Пока нет данных'))}</span>
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

    const tickRelativeAgeNodes = () => {
        dashboardPage.querySelectorAll('[data-relative-age-seconds]').forEach((node) => {
            const currentAge = Number(node.dataset.relativeAgeSeconds);
            if (!Number.isFinite(currentAge)) {
                return;
            }
            const nextAge = currentAge + 1;
            node.dataset.relativeAgeSeconds = String(nextAge);
            node.textContent = formatRelativeAgeLabel(nextAge, node.dataset.relativeAgeFallback || 'Пока нет данных');
        });
    };

    const clearDashboardTimer = () => {
        if (dashboardTimerId) {
            clearInterval(dashboardTimerId);
            dashboardTimerId = null;
        }
    };

    const clearClockTimer = () => {
        if (clockTimerId) {
            clearInterval(clockTimerId);
            clockTimerId = null;
        }
    };

    const stopDashboardPolling = () => {
        dashboardPollingStopped = true;
        clearDashboardTimer();
        clearClockTimer();
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

        if (!clockTimerId) {
            clockTimerId = setInterval(() => {
                if (!document.hidden) {
                    tickRelativeAgeNodes();
                }
            }, CLOCK_REFRESH_INTERVAL_MS);
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
            clearClockTimer();
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