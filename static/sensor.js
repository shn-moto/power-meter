const sensorPage = document.querySelector('[data-sensor-page]');

if (sensorPage) {
    const SENSOR_REFRESH_INTERVAL_MS = 60000;
    const deviceId = sensorPage.dataset.deviceId;
    const initialPayloadNode = document.querySelector('[data-initial-sensor]');
    const sourceNode = document.querySelector('[data-sensor-source]');
    const lastUpdateNode = document.querySelector('[data-sensor-last-update]');
    const connectionNode = document.querySelector('[data-sensor-connection]');
    const metricsNode = document.querySelector('[data-sensor-metrics]');
    const emptyNode = document.querySelector('[data-sensor-empty]');
    const initialPayload = initialPayloadNode ? JSON.parse(initialPayloadNode.textContent) : null;
    let isLoading = false;

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
        if (sourceNode) {
            sourceNode.textContent = payload.state_source || 'Нет данных';
        }
        if (connectionNode) {
            connectionNode.textContent = payload.connection_ready && payload.ip_address
                ? `LAN: ${payload.ip_address}`
                : 'Не обнаружено';
        }
        if (lastUpdateNode) {
            lastUpdateNode.textContent = payload.last_update || 'Нет данных';
            applyReadingStatus(lastUpdateNode, payload.last_update_status);
        }
        renderMetrics(payload.metrics || []);
    };

    const loadSensor = async () => {
        if (isLoading) {
            return;
        }
        isLoading = true;
        try {
            const response = await fetch(`/api/devices/${encodeURIComponent(deviceId)}/sensor`, { cache: 'no-store' });
            const payload = await response.json();
            if (!response.ok) {
                throw new Error(payload.detail || 'Не удалось обновить показания датчика.');
            }
            renderPayload(payload);
        } catch {
            // Keep the last rendered sensor state if a cloud refresh fails.
        } finally {
            isLoading = false;
        }
    };

    if (initialPayload) {
        renderPayload(initialPayload);
    }

    setInterval(() => {
        if (document.hidden) {
            return;
        }
        loadSensor();
    }, SENSOR_REFRESH_INTERVAL_MS);
}