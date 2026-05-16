const page = document.querySelector('[data-device-page]');

if (page) {
    const LIVE_REFRESH_INTERVAL_MS = 1000;
    const AGGREGATE_REFRESH_INTERVAL_MS = 5000;
    const HOUR_BUCKET_MS = 60 * 60 * 1000;
    const DAY_PERIOD = 'day';
    const MONTH_PERIOD = 'month';
    const YEAR_PERIOD = 'year';
    const deviceId = page.dataset.deviceId;
    const initialPayloadNode = document.querySelector('[data-initial-device-stats]');
    const periodInputs = [...page.querySelectorAll('input[data-period]')];
    const customRangeForm = page.querySelector('[data-custom-range]');
    const summary = page.querySelector('[data-device-summary]');
    const functionsContainer = page.querySelector('[data-device-functions]');
    const chart = page.querySelector('[data-chart]');
    const chartEmpty = page.querySelector('[data-chart-empty]');
    const chartMeta = page.querySelector('[data-chart-meta]');
    const dayNav = page.querySelector('[data-day-nav]');
    const dayNavLabel = page.querySelector('[data-day-nav-label]');
    const dayNavPrev = page.querySelector('[data-day-nav-prev]');
    const dayNavNext = page.querySelector('[data-day-nav-next]');
    const chargerSessions = page.querySelector('[data-charger-sessions]');
    const chargerSessionList = page.querySelector('[data-charger-session-list]');
    const chargerSessionTotal = page.querySelector('[data-charger-session-total]');
    const timerDialog = document.querySelector('[data-timer-dialog]');
    const timerForm = document.querySelector('[data-timer-form]');
    const timerCancel = document.querySelector('[data-timer-cancel]');
    const chartInstance = window.echarts ? window.echarts.init(chart) : null;
    const initialPayload = initialPayloadNode ? JSON.parse(initialPayloadNode.textContent) : null;
    let currentPeriod = 'day';
    let currentStart = null;
    let currentEnd = null;
    let selectedDayKey = null;
    let selectedMonthKey = null;
    let isAggregateLoading = false;
    let isLiveLoading = false;
    let pendingAggregateRequest = null;
    let latestAggregateRequestKey = null;
    let timerFunction = null;
    let refreshTimerId = null;
    let liveAbortController = null;
    let aggregateAbortController = null;
    let pagePollingStopped = false;
    let lastAggregateRefreshAt = 0;
    const summaryState = {
        metrics: [],
        sampleCount: '--',
        latestSample: '--',
        latestSampleStatus: 'error',
    };

    const dateFormatter = new Intl.DateTimeFormat('ru-RU', {
        weekday: 'long',
        day: '2-digit',
        month: 'long',
        year: 'numeric',
    });

    const formatValue = (value, suffix = '') => {
        if (value === null || value === undefined || Number.isNaN(value)) {
            return '--';
        }
        return `${value}${suffix}`;
    };

    const formatPower = (value) => {
        if (value === null || value === undefined || Number.isNaN(value)) {
            return '--';
        }
        if (Math.abs(value) >= 1000) {
            return `${formatNumber(value / 1000)} кВт`;
        }
        return `${formatNumber(value)} Вт`;
    };

    const formatCurrent = (value) => {
        if (value === null || value === undefined || Number.isNaN(value)) {
            return '--';
        }
        if (Math.abs(value) >= 1000) {
            return `${formatNumber(value / 1000)} А`;
        }
        return `${Math.round(value)} мА`;
    };

    const formatNumber = (value) => {
        if (!Number.isFinite(value)) {
            return '0';
        }
        if (value >= 10) {
            return value.toFixed(1).replace(/\.0$/, '');
        }
        if (value >= 1) {
            return value.toFixed(2).replace(/0$/, '').replace(/\.$/, '');
        }
        return value.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
    };

    const toDateKey = (date) => {
        const year = date.getFullYear();
        const month = String(date.getMonth() + 1).padStart(2, '0');
        const day = String(date.getDate()).padStart(2, '0');
        return `${year}-${month}-${day}`;
    };

    const toMonthKey = (date) => {
        const year = date.getFullYear();
        const month = String(date.getMonth() + 1).padStart(2, '0');
        return `${year}-${month}`;
    };

    const parseDateKey = (value) => {
        const [year, month, day] = String(value || '').split('-').map(Number);
        return new Date(year, (month || 1) - 1, day || 1, 12, 0, 0, 0);
    };

    const parseMonthKey = (value) => {
        const [year, month] = String(value || '').split('-').map(Number);
        return new Date(year, (month || 1) - 1, 1, 12, 0, 0, 0);
    };

    const getTodayKey = () => toDateKey(new Date());

    const getCurrentMonthKey = () => toMonthKey(new Date());

    const getEffectiveDayKey = () => selectedDayKey || getTodayKey();

    const getMonthRange = (monthKey) => {
        const monthDate = parseMonthKey(monthKey);
        const start = toDateKey(new Date(monthDate.getFullYear(), monthDate.getMonth(), 1, 12, 0, 0, 0));
        const end = toDateKey(new Date(monthDate.getFullYear(), monthDate.getMonth() + 1, 0, 12, 0, 0, 0));
        return { start, end };
    };

    const getAggregateRequest = () => {
        if (currentPeriod === 'custom') {
            return { period: 'custom', start: currentStart, end: currentEnd };
        }
        if (currentPeriod === DAY_PERIOD && selectedDayKey) {
            return { period: 'custom', start: selectedDayKey, end: selectedDayKey };
        }
        if (currentPeriod === MONTH_PERIOD && selectedMonthKey) {
            return { period: 'custom', ...getMonthRange(selectedMonthKey) };
        }
        return { period: currentPeriod, start: null, end: null };
    };

    const getAggregateRequestKey = (period, start, end) => `${period}|${start || ''}|${end || ''}`;

    const shouldRefreshAggregate = () => {
        const todayKey = getTodayKey();
        if (currentPeriod === DAY_PERIOD) {
            return !selectedDayKey || selectedDayKey >= todayKey;
        }
        if (currentPeriod === MONTH_PERIOD) {
            return !selectedMonthKey || selectedMonthKey >= getCurrentMonthKey();
        }
        if (currentPeriod === 'custom') {
            return Boolean(currentStart && currentEnd && currentStart <= todayKey && currentEnd >= todayKey);
        }
        return true;
    };

    const setSelectedPeriodInput = (period) => {
        periodInputs.forEach((input) => {
            input.checked = input.value === period;
        });
    };

    const updateDayNav = () => {
        const isDayView = currentPeriod === DAY_PERIOD;
        dayNav.hidden = !isDayView;
        if (!isDayView) {
            return;
        }

        const dayKey = getEffectiveDayKey();
        dayNavLabel.textContent = dateFormatter.format(parseDateKey(dayKey));
        dayNavPrev.disabled = isAggregateLoading;
        dayNavNext.disabled = isAggregateLoading || dayKey >= getTodayKey();
    };

    const drillToDay = (timestamp) => {
        selectedDayKey = toDateKey(new Date(timestamp));
        selectedMonthKey = null;
        currentPeriod = DAY_PERIOD;
        currentStart = null;
        currentEnd = null;
        customRangeForm.reset();
        setSelectedPeriodInput(DAY_PERIOD);
        updateDayNav();
        loadCurrentPeriod();
    };

    const drillToMonth = (timestamp) => {
        selectedMonthKey = toMonthKey(new Date(timestamp));
        selectedDayKey = null;
        currentPeriod = MONTH_PERIOD;
        currentStart = null;
        currentEnd = null;
        customRangeForm.reset();
        setSelectedPeriodInput(MONTH_PERIOD);
        updateDayNav();
        loadCurrentPeriod();
    };

    const applyReadingStatus = (node, status) => {
        node.classList.remove('is-ok', 'is-warning', 'is-error');
        node.classList.add('reading-status', `is-${status || 'error'}`);
    };

    const createPhasePacketMetric = (metric) => {
        const wrapper = document.createElement('span');
        wrapper.className = 'raw-phase-chip';
        wrapper.tabIndex = 0;
        if (metric.tooltip) {
            wrapper.dataset.tooltip = metric.tooltip;
            wrapper.setAttribute('aria-label', metric.tooltip);
        }

        const parts = Array.isArray(metric.parts) ? metric.parts : [];
        parts.forEach((part) => {
            const key = document.createElement('span');
            key.className = 'raw-phase-chip-key';
            key.textContent = part.short_label || '?';

            const value = document.createElement('span');
            value.className = 'raw-phase-chip-value';
            value.textContent = part.value || '--';

            wrapper.append(key, value);
        });

        return wrapper;
    };

    const createMetricValueNode = (metric) => {
        if (metric && metric.display_kind === 'phase_packet') {
            return createPhasePacketMetric(metric);
        }

        const text = document.createElement('span');
        text.textContent = metric?.value || '--';
        if (metric?.tooltip) {
            text.title = metric.tooltip;
        }
        return text;
    };

    const isMissingMetricValue = (value) => value === null || value === undefined || value === '' || value === '--' || value === 'Нет данных';

    const rebuildPhasePacketValue = (parts) => {
        const filledParts = (Array.isArray(parts) ? parts : []).filter((part) => !isMissingMetricValue(part?.value));
        if (!filledParts.length) {
            return 'Нет данных';
        }
        return filledParts.map((part) => `${part.short_label} ${part.value} ${part.unit}`).join(' ');
    };

    const mergePhasePacketMetric = (previousMetric, nextMetric) => {
        const previousParts = Array.isArray(previousMetric?.parts) ? previousMetric.parts : [];
        const nextParts = Array.isArray(nextMetric?.parts) ? nextMetric.parts : [];
        const mergedParts = nextParts.map((part, index) => {
            const previousPart = previousParts[index];
            if (!previousPart || !isMissingMetricValue(previousPart.value) && !isMissingMetricValue(part?.value)) {
                return part;
            }
            if (isMissingMetricValue(part?.value) && !isMissingMetricValue(previousPart?.value)) {
                return { ...part, value: previousPart.value };
            }
            return part;
        });

        return {
            ...nextMetric,
            parts: mergedParts,
            value: rebuildPhasePacketValue(mergedParts),
        };
    };

    const mergeMetricWithPrevious = (previousMetric, nextMetric) => {
        if (!previousMetric) {
            return nextMetric;
        }
        if (nextMetric?.display_kind === 'phase_packet') {
            return mergePhasePacketMetric(previousMetric, nextMetric);
        }
        if (isMissingMetricValue(nextMetric?.value) && !isMissingMetricValue(previousMetric?.value)) {
            return {
                ...nextMetric,
                value: previousMetric.value,
            };
        }
        return nextMetric;
    };

    const mergeLiveMetrics = (previousMetrics, nextMetrics) => {
        if (!Array.isArray(nextMetrics) || !nextMetrics.length) {
            return Array.isArray(previousMetrics) ? previousMetrics : [];
        }
        const previousByCode = new Map((Array.isArray(previousMetrics) ? previousMetrics : []).map((metric) => [metric.code, metric]));
        return nextMetrics.map((metric) => mergeMetricWithPrevious(previousByCode.get(metric.code), metric));
    };

    const createSummaryRow = (label, value, status = null, rowClass = '') => {
        const item = document.createElement('div');
        const term = document.createElement('dt');
        const description = document.createElement('dd');
        if (rowClass) {
            item.classList.add(rowClass);
        }
        term.textContent = label;
        if (value instanceof Node) {
            description.appendChild(value);
        } else {
            description.textContent = value || '--';
        }
        if (status) {
            applyReadingStatus(description, status);
        }
        item.append(term, description);
        return item;
    };

    const renderSummaryPanel = () => {
        summary.innerHTML = '';

        const metricRows = Array.isArray(summaryState.metrics) ? summaryState.metrics : [];
        if (metricRows.length) {
            metricRows.forEach((metric) => {
                const rowClass = metric?.display_kind === 'phase_packet' ? 'summary-row-phase' : '';
                summary.appendChild(createSummaryRow(metric.label || `DPS ${metric.code}`, createMetricValueNode(metric), null, rowClass));
            });
        } else {
            summary.appendChild(createSummaryRow('Поля визуализации', 'Не выбраны'));
        }

        summary.appendChild(createSummaryRow('Замеров', String(summaryState.sampleCount ?? '--')));
        summary.appendChild(createSummaryRow('Последний замер', summaryState.latestSample || '--', summaryState.latestSampleStatus));
    };

    const describeBucket = (bucket) => {
        if (bucket === 'hour') {
            return 'часам';
        }
        if (bucket === 'day') {
            return 'дням';
        }
        return 'месяцам';
    };

    const setFunctionButtonState = (disabled) => {
        functionsContainer.querySelectorAll('button, input[type="checkbox"]').forEach((node) => {
            node.disabled = disabled;
        });
    };

    const syncFunctionStates = (items) => {
        const cards = [...functionsContainer.querySelectorAll('[data-function-code]')];
        const cardsByCode = new Map(cards.map((node) => [node.dataset.functionCode, node]));
        if (cards.length !== items.length || items.some((item) => !cardsByCode.has(item.code))) {
            renderFunctions(items);
            return;
        }

        items.forEach((item) => {
            const card = cardsByCode.get(item.code);
            if (!card) {
                return;
            }

            const state = card.querySelector('.device-function-state');
            if (state) {
                state.textContent = item.current_label;
            }

            if (item.control_type === 'toggle') {
                const checkbox = card.querySelector('input[type="checkbox"]');
                const caption = card.querySelector('.switch-caption');
                if (checkbox && document.activeElement !== checkbox) {
                    checkbox.checked = Boolean(item.current_value);
                }
                if (caption && checkbox) {
                    caption.textContent = checkbox.checked ? 'Вкл' : 'Выкл';
                }
            }
        });
    };

    const runFunction = async (code, value) => {
        setFunctionButtonState(true);
        try {
            const response = await fetch(`/api/devices/${deviceId}/functions/${code}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ value }),
            });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(payload.detail || 'Не удалось выполнить действие.');
            }
            await Promise.all([
                loadCurrentPeriod(),
                loadLive(),
            ]);
        } catch (error) {
            window.alert(error.message);
        } finally {
            setFunctionButtonState(false);
        }
    };

    const renderFunctions = (items) => {
        functionsContainer.innerHTML = '';
        if (!items || !items.length) {
            functionsContainer.innerHTML = '<p class="device-functions-empty">Для этого устройства управляемые функции пока не определены.</p>';
            return;
        }

        items.forEach((item) => {
            const card = document.createElement('section');
            card.className = 'device-function-item';
            card.dataset.functionCode = item.code;

            const heading = document.createElement('div');
            heading.className = 'device-function-head';

            const titleWrap = document.createElement('div');
            const title = document.createElement('h3');
            title.textContent = item.label;
            const description = document.createElement('p');
            description.textContent = item.description;
            titleWrap.append(title, description);

            const state = document.createElement('strong');
            state.className = 'device-function-state';
            state.textContent = item.current_label;

            heading.append(titleWrap, state);
            card.appendChild(heading);

            const controls = document.createElement('div');
            controls.className = 'device-function-controls';

            if (item.control_type === 'toggle') {
                const label = document.createElement('label');
                label.className = 'switch-control';
                const checkbox = document.createElement('input');
                checkbox.type = 'checkbox';
                checkbox.checked = Boolean(item.current_value);
                checkbox.addEventListener('change', () => {
                    runFunction(item.code, checkbox.checked);
                });
                const slider = document.createElement('span');
                slider.className = 'switch-slider';
                const caption = document.createElement('span');
                caption.className = 'switch-caption';
                caption.textContent = checkbox.checked ? 'Вкл' : 'Выкл';
                checkbox.addEventListener('change', () => {
                    caption.textContent = checkbox.checked ? 'Вкл' : 'Выкл';
                });
                label.append(checkbox, slider, caption);
                controls.appendChild(label);
            }

            if (item.control_type === 'timer') {
                const button = document.createElement('button');
                button.type = 'button';
                button.textContent = 'Задать таймер';
                button.addEventListener('click', () => {
                    timerFunction = item;
                    timerForm.elements.minutes.value = String(Math.round((Number(item.current_value) || 0) / 60));
                    if (typeof timerDialog.showModal === 'function') {
                        timerDialog.showModal();
                    }
                });
                controls.appendChild(button);
            }

            card.appendChild(controls);
            functionsContainer.appendChild(card);
        });
    };

    const renderAggregateSummary = (payload) => {
        const fields = payload.summary;
        summaryState.sampleCount = String(fields.sample_count ?? '--');
        renderSummaryPanel();
    };

    const renderLiveSummary = (payload) => {
        const fields = payload.summary;
        summaryState.metrics = mergeLiveMetrics(summaryState.metrics, payload.live_metrics);
        summaryState.latestSample = fields.latest_sample || '--';
        summaryState.latestSampleStatus = fields.latest_sample_status || 'error';
        renderSummaryPanel();
        syncFunctionStates(payload.device_functions || []);
    };

    const formatClock = (date) => {
        const h = String(date.getHours()).padStart(2, '0');
        const m = String(date.getMinutes()).padStart(2, '0');
        return `${h}:${m}`;
    };

    const formatDuration = (seconds) => {
        const total = Math.max(0, Math.round(seconds));
        const h = Math.floor(total / 3600);
        const m = Math.floor((total % 3600) / 60);
        if (h > 0) {
            return `${h}ч ${String(m).padStart(2, '0')}м`;
        }
        return `${m}м`;
    };

    const renderChargerSessions = (sessions) => {
        if (!chargerSessions || !chargerSessionList || !chargerSessionTotal) {
            return;
        }
        if (!sessions || !sessions.length) {
            chargerSessions.hidden = true;
            chargerSessionList.innerHTML = '';
            chargerSessionTotal.textContent = '';
            return;
        }
        chargerSessions.hidden = false;
        chargerSessionList.innerHTML = sessions.map((s, i) => {
            const start = new Date(s.start);
            const end = new Date(s.end);
            const range = `${formatClock(start)} – ${formatClock(end)}`;
            const duration = formatDuration(s.duration_seconds);
            const energy = `${formatNumber(s.energy_kwh)} кВт·ч`;
            const avg = s.avg_power_kw ? `avg ${formatNumber(s.avg_power_kw)} кВт` : '';
            return `<li class="charger-session-row">
                <span class="charger-session-index">${i + 1}</span>
                <span class="charger-session-time">${range}</span>
                <span class="charger-session-duration">${duration}</span>
                <span class="charger-session-energy">${energy}</span>
                <span class="charger-session-avg">${avg}</span>
            </li>`;
        }).join('');
        const totalEnergy = sessions.reduce((a, s) => a + Number(s.energy_kwh || 0), 0);
        chargerSessionTotal.textContent = `Итого: ${formatNumber(totalEnergy)} кВт·ч за ${sessions.length} ${sessions.length === 1 ? 'сессию' : 'сессии'}`;
    };

    const renderLineChart = (series, chartConfig, sessions) => {
        const points = series
            .map((item) => [Date.parse(item.timestamp), Number(item.power_kw ?? 0)])
            .filter((p) => Number.isFinite(p[0]) && Number.isFinite(p[1]));
        if (!points.length) {
            chartEmpty.hidden = false;
            chartMeta.textContent = chartConfig.label || 'Мгновенная мощность';
            chartInstance?.clear();
            renderChargerSessions(sessions);
            return;
        }
        chartEmpty.hidden = true;
        chartMeta.textContent = `${chartConfig.label || 'Мгновенная мощность'}, ${chartConfig.unit || 'кВт'}`;
        const dayStart = new Date(points[0][0]);
        dayStart.setHours(0, 0, 0, 0);
        const dayEnd = dayStart.getTime() + 24 * 60 * 60 * 1000;
        chartInstance?.setOption({
            animation: false,
            grid: { left: 60, right: 20, top: 18, bottom: 56 },
            tooltip: {
                trigger: 'axis',
                backgroundColor: 'rgba(31, 32, 34, 0.92)',
                borderWidth: 0,
                textStyle: { color: '#d7ecff', fontFamily: 'Bahnschrift, Segoe UI, sans-serif' },
                formatter: (params) => {
                    const p = Array.isArray(params) ? params[0] : params;
                    const ts = new Date(p.value[0]);
                    return `<strong>${formatClock(ts)}:${String(ts.getSeconds()).padStart(2,'0')}</strong><br/>${formatNumber(p.value[1])} ${chartConfig.unit || 'кВт'}`;
                },
            },
            toolbox: {
                right: 8,
                top: 0,
                itemSize: 14,
                iconStyle: { borderColor: '#7cbcff' },
                emphasis: { iconStyle: { borderColor: '#ffffff' } },
                feature: {
                    dataZoom: {
                        yAxisIndex: 'none',
                        title: { zoom: 'Зум по выделению', back: 'Сброс зума' },
                    },
                    restore: { title: 'Сбросить' },
                },
            },
            dataZoom: [
                { type: 'inside', xAxisIndex: 0, filterMode: 'none' },
                { type: 'slider', xAxisIndex: 0, height: 18, bottom: 6, filterMode: 'none', borderColor: 'rgba(112, 183, 255, 0.2)', textStyle: { color: '#7cbcff' } },
            ],
            xAxis: {
                type: 'time',
                min: dayStart.getTime(),
                max: dayEnd,
                axisLine: { lineStyle: { color: 'rgba(112, 183, 255, 0.4)' } },
                axisLabel: {
                    color: '#94cfff',
                    fontFamily: 'Bahnschrift, Segoe UI, sans-serif',
                    hideOverlap: true,
                    formatter: (value) => {
                        const d = new Date(Number(value));
                        return d.getMinutes() === 0 ? String(d.getHours()) : `${d.getHours()}:${String(d.getMinutes()).padStart(2,'0')}`;
                    },
                },
                splitLine: { show: false },
            },
            yAxis: {
                type: 'value',
                min: 0,
                splitNumber: 4,
                axisLabel: {
                    color: '#7cbcff',
                    fontFamily: 'Bahnschrift, Segoe UI, sans-serif',
                    formatter: (value) => `${formatNumber(Number(value))} ${chartConfig.unit || 'кВт'}`,
                },
                splitLine: { lineStyle: { color: 'rgba(112, 183, 255, 0.12)' } },
            },
            series: [{
                type: 'line',
                data: points,
                showSymbol: false,
                smooth: false,
                step: 'end',
                lineStyle: { width: 1.5, color: '#7fd0ff' },
                areaStyle: {
                    color: new window.echarts.graphic.LinearGradient(0, 0, 0, 1, [
                        { offset: 0, color: 'rgba(127, 208, 255, 0.45)' },
                        { offset: 1, color: 'rgba(45, 120, 181, 0.05)' },
                    ]),
                },
            }],
        }, true);
        renderChargerSessions(sessions);
    };

    const renderChart = (series, chartConfig, extra) => {
        if (chartConfig?.kind === 'line') {
            renderLineChart(series, chartConfig, extra?.sessions || []);
            return;
        }
        if (chargerSessions) {
            chargerSessions.hidden = true;
        }
        const values = series.map((item) => Number(item.chart_value ?? 0));
        const maxValue = Math.max(...values, 0);
        const useIntervalHourBars = (
            chartConfig?.bucket === 'hour'
            && chartConfig?.period === DAY_PERIOD
            && series.every((item) => Number.isFinite(Date.parse(item.timestamp)))
        );
        if (!series.length || maxValue <= 0) {
            chartEmpty.hidden = false;
            chartMeta.textContent = 'Потребление по интервалам';
            chartInstance?.clear();
            return;
        }

        chartEmpty.hidden = true;
        chartMeta.textContent = `${chartConfig.label} по ${describeBucket(chartConfig.bucket)}`;
        const intervalBarData = useIntervalHourBars
            ? series.map((item) => {
                const startMs = Date.parse(item.timestamp);
                return {
                    value: [startMs, startMs + HOUR_BUCKET_MS, Number(item.chart_value ?? 0)],
                    timestamp: item.timestamp,
                    tooltipLabel: item.tooltip_label,
                };
            })
            : [];
        const chartWidth = Math.max(chart?.clientWidth || 0, 1);
        const intervalChartStartMs = intervalBarData.length ? Number(intervalBarData[0].value[0]) : null;
        const intervalChartEndMs = intervalBarData.length ? Number(intervalBarData[intervalBarData.length - 1].value[1]) : null;
        const intervalLabelStep = useIntervalHourBars
            ? Math.max(1, Math.ceil(((intervalBarData.length + 1) * 36) / chartWidth))
            : 1;
        const barGradient = new window.echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: '#7fd0ff' },
            { offset: 1, color: '#2d78b5' },
        ]);
        chartInstance?.setOption({
            animation: false,
            grid: {
                left: 72,
                right: 20,
                top: 18,
                bottom: 48,
            },
            tooltip: {
                trigger: useIntervalHourBars ? 'item' : 'axis',
                axisPointer: useIntervalHourBars
                    ? undefined
                    : {
                        type: 'shadow',
                        shadowStyle: {
                            color: 'rgba(186, 91, 46, 0.08)',
                        },
                    },
                backgroundColor: 'rgba(31, 32, 34, 0.92)',
                borderWidth: 0,
                textStyle: {
                    color: '#d7ecff',
                    fontFamily: 'Bahnschrift, Segoe UI, sans-serif',
                },
                formatter: (params) => {
                    const payload = Array.isArray(params) ? params[0] : params;
                    const item = payload?.data || {};
                    const metricValue = Array.isArray(item.value) ? Number(item.value[2] || 0) : Number(item.value || 0);
                    return `<strong>${item.tooltipLabel || ''}</strong><br/>${chartConfig.label}: ${formatNumber(metricValue)} ${chartConfig.unit}`;
                },
            },
            xAxis: useIntervalHourBars ? {
                type: 'time',
                min: intervalBarData[0]?.value?.[0],
                max: intervalChartEndMs,
                boundaryGap: false,
                interval: HOUR_BUCKET_MS,
                minInterval: HOUR_BUCKET_MS,
                maxInterval: HOUR_BUCKET_MS,
                axisTick: { show: true },
                axisLine: { lineStyle: { color: 'rgba(112, 183, 255, 0.4)' } },
                axisLabel: {
                    hideOverlap: true,
                    color: '#94cfff',
                    fontSize: 12,
                    fontFamily: 'Bahnschrift, Segoe UI, sans-serif',
                    formatter: (value) => {
                        if (intervalChartEndMs !== null && Number(value) >= intervalChartEndMs) {
                            return '';
                        }
                        if (intervalChartStartMs !== null) {
                            const offset = Math.round((Number(value) - intervalChartStartMs) / HOUR_BUCKET_MS);
                            if (offset > 0 && offset % intervalLabelStep !== 0) {
                                return '';
                            }
                        }
                        return String(new Date(Number(value)).getHours());
                    },
                },
                splitLine: { show: false },
            } : {
                type: 'category',
                data: series.map((item) => item.axis_label),
                boundaryGap: true,
                axisTick: { alignWithLabel: true },
                    axisLine: { lineStyle: { color: 'rgba(112, 183, 255, 0.4)' } },
                axisLabel: {
                    interval: 'auto',
                    hideOverlap: true,
                        color: '#94cfff',
                    fontSize: 12,
                        fontFamily: 'Bahnschrift, Segoe UI, sans-serif',
                },
            },
            yAxis: {
                type: 'value',
                min: 0,
                splitNumber: 4,
                axisLabel: {
                        color: '#7cbcff',
                        fontFamily: 'Bahnschrift, Segoe UI, sans-serif',
                    formatter: (value) => `${formatNumber(Number(value))} ${chartConfig.unit}`,
                },
                splitLine: {
                    lineStyle: {
                            color: 'rgba(112, 183, 255, 0.12)',
                    },
                },
            },
            series: useIntervalHourBars ? [
                {
                    type: 'custom',
                    renderItem: (params, api) => {
                        const xStart = api.coord([api.value(0), 0])[0];
                        const xEnd = api.coord([api.value(1), 0])[0];
                        const yValue = api.coord([api.value(0), api.value(2)])[1];
                        const yZero = api.coord([api.value(0), 0])[1];
                        const shape = window.echarts.graphic.clipRectByRect({
                            x: xStart,
                            y: yValue,
                            width: Math.max(xEnd - xStart, 1),
                            height: Math.max(yZero - yValue, 1),
                        }, {
                            x: params.coordSys.x,
                            y: params.coordSys.y,
                            width: params.coordSys.width,
                            height: params.coordSys.height,
                        });
                        if (!shape) {
                            return null;
                        }
                        return {
                            type: 'rect',
                            shape,
                            style: {
                                fill: barGradient,
                            },
                        };
                    },
                    data: intervalBarData,
                    encode: {
                        x: [0, 1],
                        y: 2,
                        tooltip: 2,
                    },
                    cursor: 'default',
                },
            ] : [
                {
                    type: 'bar',
                    barWidth: '42%',
                    barCategoryGap: '30%',
                    barGap: '0%',
                    itemStyle: {
                        color: barGradient,
                            borderRadius: [0, 0, 0, 0],
                    },
                    emphasis: {
                        itemStyle: {
                                color: '#a1deff',
                        },
                    },
                    data: series.map((item) => ({
                        value: Number(item.chart_value ?? 0),
                        tooltipLabel: item.tooltip_label,
                        timestamp: item.timestamp,
                    })),
                    cursor: currentPeriod === 'week' || currentPeriod === MONTH_PERIOD || currentPeriod === YEAR_PERIOD ? 'pointer' : 'default',
                },
            ],
        }, true);
    };

    const loadCurrentPeriod = () => {
        const request = getAggregateRequest();
        lastAggregateRefreshAt = Date.now();
        return loadPeriod(request.period, request.start, request.end);
    };

    const loadPeriod = async (period, start, end) => {
        const requestKey = getAggregateRequestKey(period, start, end);
        latestAggregateRequestKey = requestKey;
        if (isAggregateLoading) {
            pendingAggregateRequest = { period, start, end, key: requestKey };
            return;
        }
        isAggregateLoading = true;
        pendingAggregateRequest = null;
        updateDayNav();
        const query = new URLSearchParams({ period });
        if (start && end) {
            query.set('start', start);
            query.set('end', end);
        }
        try {
            const controller = new AbortController();
            aggregateAbortController = controller;
            const response = await fetch(`/api/devices/${deviceId}/stats?${query.toString()}`, {
                cache: 'no-store',
                signal: controller.signal,
            });
            if (!response.ok) {
                throw new Error(`Device stats request failed: ${response.status}`);
            }
            const payload = await response.json();
            if (requestKey !== latestAggregateRequestKey) {
                return;
            }
            renderAggregateSummary(payload);
            renderChart(payload.series, payload.chart || { label: 'Потребление', unit: 'кВт·ч', bucket: 'day' }, { sessions: payload.sessions });
        } catch (error) {
            if (error?.name !== 'AbortError') {
                console.error(error);
            }
        } finally {
            aggregateAbortController = null;
            isAggregateLoading = false;
            updateDayNav();
            if (pendingAggregateRequest && pendingAggregateRequest.key !== requestKey) {
                const nextRequest = pendingAggregateRequest;
                pendingAggregateRequest = null;
                loadPeriod(nextRequest.period, nextRequest.start, nextRequest.end);
            }
        }
    };

    const loadLive = async () => {
        if (isLiveLoading) {
            return;
        }
        isLiveLoading = true;
        try {
            const controller = new AbortController();
            liveAbortController = controller;
            const response = await fetch(`/api/devices/${deviceId}/live`, {
                cache: 'no-store',
                signal: controller.signal,
            });
            if (!response.ok) {
                throw new Error(`Device live request failed: ${response.status}`);
            }
            const payload = await response.json();
            renderLiveSummary(payload);
        } catch (error) {
            if (error?.name !== 'AbortError') {
                console.error(error);
            }
        } finally {
            liveAbortController = null;
            isLiveLoading = false;
        }
    };

    const clearRefreshTimer = () => {
        if (refreshTimerId) {
            clearTimeout(refreshTimerId);
            refreshTimerId = null;
        }
    };

    const stopPagePolling = () => {
        pagePollingStopped = true;
        clearRefreshTimer();
        liveAbortController?.abort();
        aggregateAbortController?.abort();
    };

    const scheduleNextRefresh = (delay = LIVE_REFRESH_INTERVAL_MS) => {
        if (pagePollingStopped) {
            return;
        }
        clearRefreshTimer();
        refreshTimerId = setTimeout(() => {
            refreshTimerId = null;
            if (document.hidden) {
                scheduleNextRefresh(delay);
                return;
            }
            refreshPage();
        }, delay);
    };

    const refreshPage = () => {
        if (pagePollingStopped) {
            return;
        }
        loadLive();
        if (shouldRefreshAggregate() && Date.now() - lastAggregateRefreshAt >= AGGREGATE_REFRESH_INTERVAL_MS) {
            loadCurrentPeriod();
        }
        scheduleNextRefresh();
    };

    periodInputs.forEach((input) => {
        input.addEventListener('change', () => {
            if (!input.checked) {
                return;
            }
            currentPeriod = input.value;
            currentStart = null;
            currentEnd = null;
            selectedDayKey = null;
            selectedMonthKey = null;
            customRangeForm.reset();
            updateDayNav();
            loadCurrentPeriod();
        });
    });

    [...customRangeForm.querySelectorAll('input[type="date"]')].forEach((input) => {
        input.addEventListener('change', () => {
            const formData = new FormData(customRangeForm);
            const start = String(formData.get('start') || '');
            const end = String(formData.get('end') || '');
            if (!start || !end) {
                return;
            }
            periodInputs.forEach((item) => {
                item.checked = false;
            });
            currentPeriod = 'custom';
            currentStart = start;
            currentEnd = end;
            selectedDayKey = null;
            selectedMonthKey = null;
            updateDayNav();
            loadPeriod(currentPeriod, currentStart, currentEnd);
        });
    });

    dayNavPrev?.addEventListener('click', () => {
        const currentDate = parseDateKey(getEffectiveDayKey());
        currentDate.setDate(currentDate.getDate() - 1);
        selectedDayKey = toDateKey(currentDate);
        updateDayNav();
        loadCurrentPeriod();
    });

    dayNavNext?.addEventListener('click', () => {
        const currentDate = parseDateKey(getEffectiveDayKey());
        currentDate.setDate(currentDate.getDate() + 1);
        const nextKey = toDateKey(currentDate);
        if (nextKey > getTodayKey()) {
            return;
        }
        selectedDayKey = nextKey === getTodayKey() ? null : nextKey;
        updateDayNav();
        loadCurrentPeriod();
    });

    if (timerCancel) {
        timerCancel.addEventListener('click', () => {
            timerDialog.close();
        });
    }

    if (timerForm) {
        timerForm.addEventListener('submit', async (event) => {
            event.preventDefault();
            if (!timerFunction) {
                return;
            }
            const minutes = Number(timerForm.elements.minutes.value || 0);
            timerDialog.close();
            await runFunction(timerFunction.code, Math.max(0, Math.round(minutes * 60)));
        });
    }

    if (initialPayload) {
        renderAggregateSummary(initialPayload);
        renderLiveSummary(initialPayload);
        renderChart(initialPayload.series, initialPayload.chart || { label: 'Потребление', unit: 'кВт·ч', bucket: 'hour' }, { sessions: initialPayload.sessions });
        updateDayNav();
        lastAggregateRefreshAt = Date.now();
    } else {
        loadCurrentPeriod();
    }
    chartInstance?.on('click', (params) => {
        const timestamp = params?.data?.timestamp;
        if (!timestamp) {
            return;
        }
        if (currentPeriod === 'week' || currentPeriod === MONTH_PERIOD) {
            drillToDay(timestamp);
            return;
        }
        if (currentPeriod === YEAR_PERIOD) {
            drillToMonth(timestamp);
        }
    });
    window.addEventListener('resize', () => {
        chartInstance?.resize();
    });

    document.addEventListener('visibilitychange', () => {
        if (pagePollingStopped) {
            return;
        }
        if (document.hidden) {
            clearRefreshTimer();
            liveAbortController?.abort();
            aggregateAbortController?.abort();
            return;
        }
        refreshPage();
    });

    window.addEventListener('pagehide', stopPagePolling);

    loadLive();
    scheduleNextRefresh();
}