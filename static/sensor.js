const sensorPage = document.querySelector('[data-sensor-page]');

if (sensorPage) {
    const SENSOR_REFRESH_INTERVAL_MS_LOCAL = 1000;
    const SENSOR_REFRESH_INTERVAL_MS_CLOUD = 60000;
    const deviceId = sensorPage.dataset.deviceId;
    const initialPayloadNode = document.querySelector('[data-initial-sensor]');
    const sourceNode = document.querySelector('[data-sensor-source]');
    const lastUpdateNode = document.querySelector('[data-sensor-last-update]');
    const connectionNode = document.querySelector('[data-sensor-connection]');
    const metricsPanel = document.querySelector('[data-sensor-metrics-panel]');
    const metricsNode = document.querySelector('[data-sensor-metrics]');
    const photoNode = document.querySelector('[data-device-photo]');
    const functionsContainer = document.querySelector('[data-device-functions]');
    const timerDialog = document.querySelector('[data-timer-dialog]');
    const timerForm = document.querySelector('[data-timer-form]');
    const timerCancel = document.querySelector('[data-timer-cancel]');
    const initialPayload = initialPayloadNode ? JSON.parse(initialPayloadNode.textContent) : null;
    const deviceControls = (functionsContainer && window.DeviceControls)
        ? window.DeviceControls.create({
            deviceId,
            container: functionsContainer,
            timerDialog,
            timerForm,
            timerCancel,
            onAfterCommand: () => loadSensor(),
        })
        : null;
    let isLoading = false;
    let latestPayload = initialPayload;
    let refreshTimerId = null;
    let requestAbortController = null;
    let pollingStopped = false;
    let activeRefreshIntervalMs = null;

    // ---- History chart -------------------------------------------------
    const historyChartEl = document.querySelector('[data-sensor-history-chart]');
    const historyEmptyEl = document.querySelector('[data-sensor-history-empty]');
    const historyPeriodInputs = [...document.querySelectorAll('[data-sensor-period]')];
    let historyChart = null;
    let historyPeriod = 'day';
    let historyAbortController = null;

    // Distinct palette for up to ~8 simultaneous series
    const HISTORY_PALETTE = [
        '#7fd0ff', '#e8a838', '#67b86b', '#f04848',
        '#c178e0', '#3ec5c5', '#d76d6d', '#8edb95',
    ];

    const HISTORY_LABEL_BY_CODE = {
        switch: 'Питание',
        switch_led: 'Свет',
        switch_1: 'Питание',
        cur_current: 'Ток',
        cur_power: 'Мощность',
        cur_voltage: 'Напряжение',
        va_temperature: 'Температура',
        temp_current: 'Температура',
        va_humidity: 'Влажность',
        humidity_value: 'Влажность',
        va_battery: 'Батарея',
        state_of_charge: 'Заряд',
        bright_value: 'Яркость',
        temp_value: 'Тепло-холод',
        master_state: 'Состояние',
        pir: 'PIR',
        work_mode: 'Режим',
    };

    const ensureHistoryChart = () => {
        if (historyChart || !historyChartEl || !window.echarts) return historyChart;
        historyChart = window.echarts.init(historyChartEl);
        window.addEventListener('resize', () => historyChart && historyChart.resize());
        return historyChart;
    };

    const formatTooltipTime = (ts) => {
        const d = new Date(ts);
        const pad = (n) => String(n).padStart(2, '0');
        return `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    };

    const renderHistory = (payload) => {
        const chart = ensureHistoryChart();
        if (!chart) return;
        const series = payload?.series || [];
        const hasAnyPoints = series.some((s) => Array.isArray(s.data) && s.data.length);
        if (historyEmptyEl) historyEmptyEl.hidden = hasAnyPoints;
        if (!hasAnyPoints) {
            chart.clear();
            return;
        }
        // Group series by unit so each unit family gets its own yAxis.
        const unitOrder = [];
        const unitMap = new Map();
        series.forEach((s) => {
            const unit = s.unit || '';
            if (!unitMap.has(unit)) {
                unitMap.set(unit, []);
                unitOrder.push(unit);
            }
            unitMap.get(unit).push(s);
        });
        const yAxes = unitOrder.map((unit, idx) => ({
            type: 'value',
            name: unit || '',
            position: idx === 0 ? 'left' : 'right',
            offset: Math.max(0, idx - 1) * 50,
            nameTextStyle: { color: 'rgba(217, 226, 238, 0.78)' },
            axisLine: { lineStyle: { color: 'rgba(127, 208, 255, 0.22)' } },
            axisLabel: { color: 'rgba(217, 226, 238, 0.78)' },
            splitLine: { lineStyle: { color: 'rgba(127, 208, 255, 0.08)' } },
        }));
        const echartsSeries = series.map((s, i) => {
            const unitIdx = unitOrder.indexOf(s.unit || '');
            const color = HISTORY_PALETTE[i % HISTORY_PALETTE.length];
            const label = HISTORY_LABEL_BY_CODE[s.code] || s.label || s.code;
            return {
                name: s.unit ? `${label} (${s.unit})` : label,
                type: 'line',
                smooth: !s.is_boolean,
                step: s.is_boolean ? 'end' : false,
                symbol: 'none',
                yAxisIndex: unitIdx,
                lineStyle: { color, width: 1.6 },
                itemStyle: { color },
                data: s.data,
            };
        });
        chart.setOption({
            animation: false,
            tooltip: {
                trigger: 'axis',
                backgroundColor: 'rgba(11, 18, 26, 0.94)',
                borderColor: 'rgba(127, 208, 255, 0.24)',
                textStyle: { color: '#e8f0fa' },
                formatter: (params) => {
                    if (!params || !params.length) return '';
                    const ts = params[0].axisValue ?? (params[0].value && params[0].value[0]);
                    const lines = [`<strong>${formatTooltipTime(ts)}</strong>`];
                    params.forEach((p) => {
                        const value = Array.isArray(p.value) ? p.value[1] : p.value;
                        const fmt = (typeof value === 'number') ? value.toFixed(2).replace(/\.?0+$/, '') : value;
                        lines.push(`${p.marker} ${p.seriesName}: ${fmt}`);
                    });
                    return lines.join('<br/>');
                },
            },
            legend: {
                top: 0,
                textStyle: { color: 'rgba(217, 226, 238, 0.85)' },
                inactiveColor: 'rgba(127, 208, 255, 0.28)',
            },
            grid: { top: 36, left: 50, right: unitOrder.length > 1 ? 50 + (unitOrder.length - 1) * 50 : 16, bottom: 50 },
            dataZoom: [
                { type: 'inside', xAxisIndex: 0 },
                { type: 'slider', xAxisIndex: 0, height: 18, bottom: 6, borderColor: 'rgba(127, 208, 255, 0.2)' },
            ],
            xAxis: {
                type: 'time',
                axisLine: { lineStyle: { color: 'rgba(127, 208, 255, 0.4)' } },
                axisLabel: { color: 'rgba(217, 226, 238, 0.78)' },
                splitLine: { show: false },
            },
            yAxis: yAxes,
            series: echartsSeries,
        }, true);
    };

    const loadHistory = async () => {
        if (!historyChartEl) return;
        historyAbortController?.abort();
        const controller = new AbortController();
        historyAbortController = controller;
        try {
            const url = `/api/devices/${encodeURIComponent(deviceId)}/sensor-history?period=${encodeURIComponent(historyPeriod)}`;
            const r = await fetch(url, { cache: 'no-store', signal: controller.signal });
            if (!r.ok) return;
            const payload = await r.json();
            if (controller !== historyAbortController) return;
            renderHistory(payload);
        } catch (_) { /* swallow */ }
    };

    historyPeriodInputs.forEach((input) => {
        input.addEventListener('change', () => {
            if (!input.checked) return;
            historyPeriod = input.value;
            loadHistory();
        });
    });
    // ---- end history chart ----

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
            metricsNode.innerHTML = '';
            if (metricsPanel) {
                metricsPanel.hidden = true;
            }
            return;
        }

        if (metricsPanel) {
            metricsPanel.hidden = false;
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
            connectionNode.textContent = payload.connection_label || 'Не обнаружено';
        }
        const stateNode = document.querySelector('[data-sensor-state]');
        if (stateNode && payload.state_label) {
            stateNode.textContent = payload.state_label;
        }
        if (lastUpdateNode) {
            lastUpdateNode.textContent = payload.last_update || 'Нет данных';
            lastUpdateNode.title = payload.last_update || 'Нет данных';
            applyReadingStatus(lastUpdateNode, payload.last_update_status);
        }
        renderMetrics(payload.metrics || []);
        if (photoNode && payload.image_url && photoNode.getAttribute('src') !== payload.image_url) {
            photoNode.src = payload.image_url;
        }
        if (deviceControls) {
            deviceControls.sync(payload.device_functions || []);
        }
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

    const clearRefreshTimer = () => {
        if (refreshTimerId) {
            clearInterval(refreshTimerId);
            refreshTimerId = null;
        }
        activeRefreshIntervalMs = null;
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
    };

    const stopPolling = () => {
        pollingStopped = true;
        clearRefreshTimer();
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

    // Kick off the history chart once ECharts CDN script loads.
    const startHistory = () => {
        if (!historyChartEl) return;
        if (window.echarts) {
            loadHistory();
        } else {
            setTimeout(startHistory, 100);
        }
    };
    startHistory();
}