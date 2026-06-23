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
    const historyNavEl = document.querySelector('[data-sensor-history-nav]');
    const historyNavPrev = document.querySelector('[data-sensor-history-prev]');
    const historyNavNext = document.querySelector('[data-sensor-history-next]');
    const historyNavLabel = document.querySelector('[data-sensor-history-label]');
    let historyChart = null;
    let historyPeriod = 'day';
    let historyAbortController = null;
    let selectedDayKey = null;     // 'YYYY-MM-DD' or null = today
    let selectedWeekKey = null;    // 'YYYY-MM-DD' of Monday or null = current week
    let selectedMonthKey = null;   // 'YYYY-MM' or null = current month
    let selectedYearKey = null;    // 'YYYY' or null = current year

    const pad2 = (n) => String(n).padStart(2, '0');
    const toDateKey = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
    const toMonthKey = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}`;
    const parseDateKey = (v) => {
        const [y, m, d] = String(v || '').split('-').map(Number);
        return new Date(y, (m || 1) - 1, d || 1, 12, 0, 0, 0);
    };
    const parseMonthKey = (v) => {
        const [y, m] = String(v || '').split('-').map(Number);
        return new Date(y, (m || 1) - 1, 1, 12, 0, 0, 0);
    };
    const getTodayKey = () => toDateKey(new Date());
    const getWeekStart = (date) => {
        const d = new Date(date.getFullYear(), date.getMonth(), date.getDate(), 12, 0, 0, 0);
        const delta = (d.getDay() + 6) % 7; // Monday = 0
        d.setDate(d.getDate() - delta);
        return d;
    };
    const getCurrentWeekKey = () => toDateKey(getWeekStart(new Date()));
    const getCurrentMonthKey = () => toMonthKey(new Date());
    const getCurrentYearKey = () => String(new Date().getFullYear());
    const getEffectiveDayKey = () => selectedDayKey || getTodayKey();
    const getEffectiveWeekKey = () => selectedWeekKey || getCurrentWeekKey();
    const getEffectiveMonthKey = () => selectedMonthKey || getCurrentMonthKey();
    const getEffectiveYearKey = () => selectedYearKey || getCurrentYearKey();

    const getWeekRange = (weekKey) => {
        const start = parseDateKey(weekKey);
        const end = new Date(start.getFullYear(), start.getMonth(), start.getDate() + 6, 12, 0, 0, 0);
        return { start: toDateKey(start), end: toDateKey(end) };
    };
    const getMonthRange = (monthKey) => {
        const d = parseMonthKey(monthKey);
        const start = toDateKey(new Date(d.getFullYear(), d.getMonth(), 1, 12, 0, 0, 0));
        const end = toDateKey(new Date(d.getFullYear(), d.getMonth() + 1, 0, 12, 0, 0, 0));
        return { start, end };
    };
    const getYearRange = (yearKey) => {
        const y = Number(yearKey);
        return { start: `${y}-01-01`, end: `${y}-12-31` };
    };
    const formatWeekLabel = (weekKey) => {
        const start = parseDateKey(weekKey);
        const end = new Date(start.getFullYear(), start.getMonth(), start.getDate() + 6, 12, 0, 0, 0);
        const fmt = new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: 'short' });
        return `${fmt.format(start)} — ${fmt.format(end)}`;
    };
    const formatMonthLabel = (monthKey) => {
        const d = parseMonthKey(monthKey);
        return new Intl.DateTimeFormat('ru-RU', { month: 'long', year: 'numeric' }).format(d);
    };
    const formatDayLabel = (dayKey) => {
        return new Intl.DateTimeFormat('ru-RU', { weekday: 'long', day: '2-digit', month: 'long' }).format(parseDateKey(dayKey));
    };

    const updateHistoryNav = () => {
        if (!historyNavEl) return;
        let label = '';
        let canGoNext = false;
        if (historyPeriod === 'day') {
            const k = getEffectiveDayKey();
            label = formatDayLabel(k);
            canGoNext = k < getTodayKey();
        } else if (historyPeriod === 'week') {
            const k = getEffectiveWeekKey();
            label = formatWeekLabel(k);
            canGoNext = k < getCurrentWeekKey();
        } else if (historyPeriod === 'month') {
            const k = getEffectiveMonthKey();
            label = formatMonthLabel(k);
            canGoNext = k < getCurrentMonthKey();
        } else if (historyPeriod === 'year') {
            const k = getEffectiveYearKey();
            label = k;
            canGoNext = k < getCurrentYearKey();
        }
        historyNavLabel.textContent = label;
        historyNavNext.disabled = !canGoNext;
    };

    const buildHistoryQuery = () => {
        if (historyPeriod === 'day') {
            const k = getEffectiveDayKey();
            return { period: 'custom', start: k, end: k };
        }
        if (historyPeriod === 'week') return { period: 'custom', ...getWeekRange(getEffectiveWeekKey()) };
        if (historyPeriod === 'month') return { period: 'custom', ...getMonthRange(getEffectiveMonthKey()) };
        if (historyPeriod === 'year') return { period: 'custom', ...getYearRange(getEffectiveYearKey()) };
        return { period: historyPeriod, start: null, end: null };
    };

    const shiftHistoryPeriod = (direction) => {
        if (historyPeriod === 'day') {
            const d = parseDateKey(getEffectiveDayKey());
            d.setDate(d.getDate() + direction);
            const k = toDateKey(d);
            if (direction > 0 && k > getTodayKey()) return;
            selectedDayKey = (direction > 0 && k === getTodayKey()) ? null : k;
        } else if (historyPeriod === 'week') {
            const d = parseDateKey(getEffectiveWeekKey());
            d.setDate(d.getDate() + 7 * direction);
            const k = toDateKey(d);
            const cur = getCurrentWeekKey();
            if (direction > 0 && k > cur) return;
            selectedWeekKey = (direction > 0 && k === cur) ? null : k;
        } else if (historyPeriod === 'month') {
            const d = parseMonthKey(getEffectiveMonthKey());
            d.setMonth(d.getMonth() + direction);
            const k = toMonthKey(d);
            const cur = getCurrentMonthKey();
            if (direction > 0 && k > cur) return;
            selectedMonthKey = (direction > 0 && k === cur) ? null : k;
        } else if (historyPeriod === 'year') {
            const y = Number(getEffectiveYearKey()) + direction;
            const k = String(y);
            const cur = getCurrentYearKey();
            if (direction > 0 && k > cur) return;
            selectedYearKey = (direction > 0 && k === cur) ? null : k;
        }
        updateHistoryNav();
        loadHistory();
    };

    // Distinct palette for up to ~8 simultaneous series (fallback when a
    // specific quantity doesn't have a fixed colour mapped below).
    const HISTORY_PALETTE = [
        '#7fd0ff', '#e8a838', '#67b86b', '#f04848',
        '#c178e0', '#3ec5c5', '#d76d6d', '#8edb95',
    ];

    // Fixed colour per physical quantity so the same metric reads the same
    // wherever it shows up — Atorch page, inverter page, dashboard cards.
    // Solar always yellow (matches the dashboard generator-card chart),
    // power orange, voltage green, current blue.
    const HISTORY_COLOR_BY_CODE = {
        solar_estimate: '#f5c542',
        cur_power: '#e8a838',
        cur_voltage: '#67b86b',
        cur_current: '#7fd0ff',
        state_of_charge: '#c178e0',
        va_temperature: '#f04848',
        temp_current: '#f04848',
        va_humidity: '#3ec5c5',
        humidity_value: '#3ec5c5',
    };

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

    let lastHistoryPayload = null;
    const isNarrowViewport = () => window.matchMedia('(max-width: 720px)').matches;

    const ensureHistoryChart = () => {
        if (historyChart || !historyChartEl || !window.echarts) return historyChart;
        historyChart = window.echarts.init(historyChartEl);
        // On viewport changes the mobile flag flips, which means the axis
        // visibility + grid margins need to be recomputed. Cheaper than
        // re-fetching: re-render the cached payload.
        window.addEventListener('resize', () => {
            if (!historyChart) return;
            historyChart.resize();
            if (lastHistoryPayload) renderHistory(lastHistoryPayload);
        });
        return historyChart;
    };

    // Snapshot the user's current dataZoom so the periodic refresh doesn't
    // throw their selection away every 15 seconds. Same pattern as the
    // energy device chart.
    const captureHistoryZoom = () => {
        if (!historyChart) return null;
        try {
            const opt = historyChart.getOption();
            const zooms = Array.isArray(opt?.dataZoom) ? opt.dataZoom : [];
            const captured = zooms.map((dz) => ({
                start: typeof dz.start === 'number' ? dz.start : null,
                end: typeof dz.end === 'number' ? dz.end : null,
                startValue: dz.startValue ?? null,
                endValue: dz.endValue ?? null,
            }));
            const userTouched = captured.some((c) => (
                (c.start !== null && c.start > 0)
                || (c.end !== null && c.end < 100)
                || c.startValue !== null
                || c.endValue !== null
            ));
            return userTouched ? captured : null;
        } catch (_) {
            return null;
        }
    };

    const restoreHistoryZoom = (captured) => {
        if (!historyChart || !captured || !captured.length) return;
        captured.forEach((dz, idx) => {
            const action = { type: 'dataZoom', dataZoomIndex: idx };
            if (dz.startValue !== null && dz.endValue !== null) {
                action.startValue = dz.startValue;
                action.endValue = dz.endValue;
            } else if (dz.start !== null && dz.end !== null) {
                action.start = dz.start;
                action.end = dz.end;
            } else {
                return;
            }
            try { historyChart.dispatchAction(action); } catch (_) {}
        });
    };

    // How often to fetch fresh data per period. Day → tight; longer periods
    // change slowly so we don't need to hammer the server.
    const HISTORY_REFRESH_MS = {
        day: 15000,
        week: 60000,
        month: 300000,
        year: 1800000,
    };
    let historyRefreshTimerId = null;

    const clearHistoryRefreshTimer = () => {
        if (historyRefreshTimerId) {
            clearInterval(historyRefreshTimerId);
            historyRefreshTimerId = null;
        }
    };

    const viewingCurrentBucket = () => {
        if (historyPeriod === 'day') return !selectedDayKey;
        if (historyPeriod === 'week') return !selectedWeekKey;
        if (historyPeriod === 'month') return !selectedMonthKey;
        if (historyPeriod === 'year') return !selectedYearKey;
        return false;
    };

    const ensureHistoryRefreshTimer = () => {
        clearHistoryRefreshTimer();
        if (pollingStopped || !historyChartEl) return;
        // Auto-refresh only when looking at the current period bucket — when
        // the user pages back to an older day/week/month/year there's
        // nothing changing in that window.
        if (!viewingCurrentBucket()) return;
        const interval = HISTORY_REFRESH_MS[historyPeriod] || 60000;
        historyRefreshTimerId = setInterval(() => {
            if (!document.hidden) loadHistory();
        }, interval);
    };

    const formatTooltipTime = (ts) => {
        const d = new Date(ts);
        const pad = (n) => String(n).padStart(2, '0');
        return `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    };

    const SHORT_WEEKDAY_FMT = new Intl.DateTimeFormat('ru-RU', { weekday: 'short' });
    const SHORT_MONTH_FMT = new Intl.DateTimeFormat('ru-RU', { month: 'short' });

    const buildAxisLabelFormatter = (period) => {
        return (value) => {
            const d = new Date(value);
            if (period === 'week') {
                // Only label at midnight; sub-day ticks render unlabeled
                if (d.getHours() === 0 && d.getMinutes() === 0) {
                    return SHORT_WEEKDAY_FMT.format(d);
                }
                return '';
            }
            if (period === 'month') {
                if (d.getHours() === 0 && d.getMinutes() === 0) {
                    return String(d.getDate());
                }
                return '';
            }
            if (period === 'year') {
                if (d.getDate() === 1 && d.getHours() === 0 && d.getMinutes() === 0) {
                    return SHORT_MONTH_FMT.format(d);
                }
                return '';
            }
            // day
            return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
        };
    };

    const majorSplitFor = (period) => {
        if (period === 'week') return 7;
        if (period === 'month') return 10;
        if (period === 'year') return 12;
        return 6; // day
    };
    const minorTicksFor = (period) => {
        if (period === 'week') return 24;  // hours within a day
        if (period === 'month') return 4;  // quarter-day ticks
        if (period === 'year') return 4;   // quarter-month
        return 0;
    };

    const renderHistory = (payload) => {
        const chart = ensureHistoryChart();
        if (!chart) return;
        lastHistoryPayload = payload;
        const savedZoom = captureHistoryZoom();
        const series = payload?.series || [];
        const hasAnyPoints = series.some((s) => Array.isArray(s.data) && s.data.length);
        if (historyEmptyEl) historyEmptyEl.hidden = hasAnyPoints;
        if (!hasAnyPoints) {
            chart.clear();
            return;
        }
        const narrow = isNarrowViewport();
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
        // On a narrow viewport we collapse all non-primary axes (Voltage,
        // Current, SoC etc) — they eat more horizontal space than the chart
        // itself otherwise. Precise values stay accessible via the tooltip
        // on tap. The yAxis entries still have to exist so series can map
        // to them via yAxisIndex; we just hide their visuals.
        const yAxes = unitOrder.map((unit, idx) => ({
            type: 'value',
            name: unit || '',
            position: idx === 0 ? 'left' : 'right',
            offset: Math.max(0, idx - 1) * 50,
            show: !narrow || idx === 0,
            nameTextStyle: { color: 'rgba(217, 226, 238, 0.78)' },
            axisLine: { lineStyle: { color: 'rgba(127, 208, 255, 0.22)' } },
            axisLabel: { color: 'rgba(217, 226, 238, 0.78)' },
            splitLine: { lineStyle: { color: 'rgba(127, 208, 255, 0.08)' } },
        }));
        const echartsSeries = series.map((s, i) => {
            const unitIdx = unitOrder.indexOf(s.unit || '');
            const color = HISTORY_COLOR_BY_CODE[s.code] || HISTORY_PALETTE[i % HISTORY_PALETTE.length];
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
            grid: {
                top: 36,
                left: 50,
                right: narrow ? 16 : (unitOrder.length > 1 ? 50 + (unitOrder.length - 1) * 50 : 16),
                bottom: 50,
            },
            dataZoom: [
                { type: 'inside', xAxisIndex: 0 },
                { type: 'slider', xAxisIndex: 0, height: 18, bottom: 6, borderColor: 'rgba(127, 208, 255, 0.2)' },
            ],
            xAxis: {
                type: 'time',
                axisLine: { lineStyle: { color: 'rgba(127, 208, 255, 0.4)' } },
                axisLabel: {
                    color: 'rgba(217, 226, 238, 0.78)',
                    hideOverlap: true,
                    formatter: buildAxisLabelFormatter(historyPeriod),
                },
                minorTick: { show: historyPeriod !== 'day', splitNumber: minorTicksFor(historyPeriod) },
                minorSplitLine: { show: false },
                splitLine: { show: false },
                splitNumber: majorSplitFor(historyPeriod),
            },
            yAxis: yAxes,
            series: echartsSeries,
        }, true);
        restoreHistoryZoom(savedZoom);
    };

    const loadHistory = async () => {
        if (!historyChartEl) return;
        historyAbortController?.abort();
        const controller = new AbortController();
        historyAbortController = controller;
        try {
            const query = buildHistoryQuery();
            const params = new URLSearchParams({ period: query.period });
            if (query.start) params.set('start', query.start);
            if (query.end) params.set('end', query.end);
            const url = `/api/devices/${encodeURIComponent(deviceId)}/sensor-history?${params.toString()}`;
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
            // Reset selected keys so we land on the current bucket of the new period
            selectedDayKey = null;
            selectedWeekKey = null;
            selectedMonthKey = null;
            selectedYearKey = null;
            updateHistoryNav();
            loadHistory();
            ensureHistoryRefreshTimer();
        });
    });

    historyNavPrev?.addEventListener('click', () => {
        shiftHistoryPeriod(-1);
        ensureHistoryRefreshTimer();
    });
    historyNavNext?.addEventListener('click', () => {
        shiftHistoryPeriod(1);
        ensureHistoryRefreshTimer();
    });
    updateHistoryNav();
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
        clearHistoryRefreshTimer();
        historyAbortController?.abort();
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
            clearHistoryRefreshTimer();
            requestAbortController?.abort();
            return;
        }
        loadSensor();
        ensurePollingTimers();
        // Refresh history once we come back so the user sees the latest
        // points immediately, then resume the timer.
        loadHistory();
        ensureHistoryRefreshTimer();
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
            ensureHistoryRefreshTimer();
        } else {
            setTimeout(startHistory, 100);
        }
    };
    startHistory();
}