const sensorPage = document.querySelector('[data-sensor-page]');

if (sensorPage) {
    const SENSOR_REFRESH_INTERVAL_MS_LOCAL = 1000;
    const SENSOR_REFRESH_INTERVAL_MS_CLOUD = 60000;
    const CLOCK_REFRESH_INTERVAL_MS = 1000;
    const deviceId = sensorPage.dataset.deviceId;
    const initialPayloadNode = document.querySelector('[data-initial-sensor]');
    const sourceNode = document.querySelector('[data-sensor-source]');
    const lastUpdateNode = document.querySelector('[data-sensor-last-update]');
    const connectionNode = document.querySelector('[data-sensor-connection]');
    const metricsNode = document.querySelector('[data-sensor-metrics]');
    const emptyNode = document.querySelector('[data-sensor-empty]');
    const initialPayload = initialPayloadNode ? JSON.parse(initialPayloadNode.textContent) : null;
    let isLoading = false;
    let latestPayload = initialPayload;
    let refreshTimerId = null;
    let clockTimerId = null;
    let requestAbortController = null;
    let pollingStopped = false;
    let activeRefreshIntervalMs = null;

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
            return fallback || 'Нет данных';
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

    const renderMetrics = (metrics) => {
        if (!metricsNode) {
            return;
        }

        if (!metrics || !metrics.length) {
            metricsNode.hidden = true;
            metricsNode.innerHTML = '';
            if (emptyNode) {
                emptyNode.hidden = false;
            }
            return;
        }

        metricsNode.hidden = false;
        if (emptyNode) {
            emptyNode.hidden = true;
        }
        metricsNode.innerHTML = metrics.map((metric) => `
            <section class="sensor-metric-card" data-sensor-metric data-sensor-code="${escapeHtml(metric.code)}">
                <span class="stat-label">${escapeHtml(metric.label)}</span>
                <strong class="stat-value">${escapeHtml(metric.value)}</strong>
            </section>
        `).join('');
    };

    const renderPayload = (payload) => {
        latestPayload = payload;
        if (sourceNode) {
            sourceNode.textContent = payload.state_source || 'Нет данных';
        }
        if (connectionNode) {
            connectionNode.textContent = payload.connection_ready && payload.ip_address
                ? `LAN: ${payload.ip_address}`
                : 'Не обнаружено';
        }
        if (lastUpdateNode) {
            lastUpdateNode.textContent = formatRelativeAgeLabel(payload.last_update_age_seconds, payload.last_update || 'Нет данных');
            lastUpdateNode.title = payload.last_update || 'Нет данных';
            applyReadingStatus(lastUpdateNode, payload.last_update_status);
        }
        renderMetrics(payload.metrics || []);
        ensurePollingTimers();
    };

    const getRefreshIntervalMs = () => latestPayload?.connection_ready
        ? SENSOR_REFRESH_INTERVAL_MS_LOCAL
        : SENSOR_REFRESH_INTERVAL_MS_CLOUD;

    const loadSensor = async () => {
        if (isLoading || pollingStopped) {
            return null;
        }
        isLoading = true;
        const controller = new AbortController();
        requestAbortController = controller;
        try {
            const response = await fetch(`/api/devices/${encodeURIComponent(deviceId)}/sensor`, {
                cache: 'no-store',
                signal: controller.signal,
            });
            const payload = await response.json();
            if (!response.ok) {
                throw new Error(payload.detail || 'Не удалось обновить показания датчика.');
            }
            renderPayload(payload);
            return payload;
        } catch (error) {
            if (error?.name === 'AbortError') {
                return null;
            }
            // Keep the last rendered sensor state if a cloud refresh fails.
            return null;
        } finally {
            if (requestAbortController === controller) {
                requestAbortController = null;
            }
            isLoading = false;
        }
    };

    const tickRelativeAge = () => {
        if (!latestPayload || !Number.isFinite(latestPayload.last_update_age_seconds)) {
            return;
        }

        latestPayload = {
            ...latestPayload,
            last_update_age_seconds: Number(latestPayload.last_update_age_seconds) + 1,
        };

        if (lastUpdateNode) {
            lastUpdateNode.textContent = formatRelativeAgeLabel(latestPayload.last_update_age_seconds, latestPayload.last_update || 'Нет данных');
            lastUpdateNode.title = latestPayload.last_update || 'Нет данных';
        }
    };

    const clearRefreshTimer = () => {
        if (refreshTimerId) {
            clearInterval(refreshTimerId);
            refreshTimerId = null;
        }
        activeRefreshIntervalMs = null;
    };

    const clearClockTimer = () => {
        if (clockTimerId) {
            clearInterval(clockTimerId);
            clockTimerId = null;
        }
    };

    const ensurePollingTimers = () => {
        if (pollingStopped) {
            return;
        }

        const nextRefreshIntervalMs = getRefreshIntervalMs();
        if (!refreshTimerId || activeRefreshIntervalMs !== nextRefreshIntervalMs) {
            clearRefreshTimer();
            activeRefreshIntervalMs = nextRefreshIntervalMs;
            refreshTimerId = setInterval(() => {
                if (!document.hidden) {
                    loadSensor();
                }
            }, nextRefreshIntervalMs);
        }

        if (!clockTimerId) {
            clockTimerId = setInterval(() => {
                if (!document.hidden) {
                    tickRelativeAge();
                }
            }, CLOCK_REFRESH_INTERVAL_MS);
        }
    };

    const stopPolling = () => {
        pollingStopped = true;
        clearRefreshTimer();
        clearClockTimer();
        requestAbortController?.abort();
    };

    if (initialPayload) {
        renderPayload(initialPayload);
    }

    document.addEventListener('visibilitychange', () => {
        if (pollingStopped) {
            return;
        }
        if (document.hidden) {
            clearRefreshTimer();
            clearClockTimer();
            requestAbortController?.abort();
            return;
        }
        loadSensor();
        ensurePollingTimers();
    });

    window.addEventListener('pagehide', stopPolling);

    if (!initialPayload) {
        loadSensor();
    }
    ensurePollingTimers();
}