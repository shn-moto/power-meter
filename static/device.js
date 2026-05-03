const page = document.querySelector('[data-device-page]');

if (page) {
    const LIVE_REFRESH_INTERVAL_MS = 1000;
    const AGGREGATE_REFRESH_INTERVAL_MS = 5000;
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
    const timerDialog = document.querySelector('[data-timer-dialog]');
    const timerForm = document.querySelector('[data-timer-form]');
    const timerCancel = document.querySelector('[data-timer-cancel]');
    const chartInstance = window.echarts ? window.echarts.init(chart) : null;
    const initialPayload = initialPayloadNode ? JSON.parse(initialPayloadNode.textContent) : null;
    const summaryCurrentPower = summary.querySelector('[data-summary-current-power]');
    const summaryCurrentCurrent = summary.querySelector('[data-summary-current-current]');
    const summaryCurrentVoltage = summary.querySelector('[data-summary-current-voltage]');
    const summaryEnergy = summary.querySelector('[data-summary-energy]');
    const summaryAveragePower = summary.querySelector('[data-summary-average-power]');
    const summaryPeakPower = summary.querySelector('[data-summary-peak-power]');
    const summaryAverageVoltage = summary.querySelector('[data-summary-average-voltage]');
    const summarySampleCount = summary.querySelector('[data-summary-sample-count]');
    const summaryLatestSample = summary.querySelector('[data-device-latest-sample]');
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
        summaryEnergy.textContent = `${fields.energy_kwh} кВт·ч`;
        summaryAveragePower.textContent = `${fields.average_power_kw} кВт`;
        summaryPeakPower.textContent = `${fields.peak_power_kw} кВт`;
        summaryAverageVoltage.textContent = formatValue(fields.average_voltage_v, ' В');
        summarySampleCount.textContent = String(fields.sample_count);
    };

    const renderLiveSummary = (payload) => {
        const fields = payload.summary;
        summaryCurrentPower.textContent = formatPower(fields.current_power_w);
        summaryCurrentCurrent.textContent = formatCurrent(fields.current_current_ma);
        summaryCurrentVoltage.textContent = formatValue(fields.current_voltage_v, ' В');
        summaryLatestSample.textContent = fields.latest_sample || '--';
        applyReadingStatus(summaryLatestSample, fields.latest_sample_status);
        syncFunctionStates(payload.device_functions || []);
    };

    const renderChart = (series, chartConfig) => {
        const values = series.map((item) => Number(item.chart_value ?? 0));
        const maxValue = Math.max(...values, 0);
        if (!series.length || maxValue <= 0) {
            chartEmpty.hidden = false;
            chartMeta.textContent = 'Потребление по интервалам';
            chartInstance?.clear();
            return;
        }

        chartEmpty.hidden = true;
        chartMeta.textContent = `${chartConfig.label} по ${describeBucket(chartConfig.bucket)}`;
        chartInstance?.setOption({
            animation: false,
            grid: {
                left: 72,
                right: 20,
                top: 18,
                bottom: 48,
            },
            tooltip: {
                trigger: 'axis',
                axisPointer: {
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
                    return `<strong>${item.tooltipLabel || ''}</strong><br/>${chartConfig.label}: ${formatNumber(Number(item.value || 0))} ${chartConfig.unit}`;
                },
            },
            xAxis: {
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
            series: [
                {
                    type: 'bar',
                        barWidth: '42%',
                    itemStyle: {
                        color: new window.echarts.graphic.LinearGradient(0, 0, 0, 1, [
                                { offset: 0, color: '#7fd0ff' },
                                { offset: 1, color: '#2d78b5' },
                        ]),
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
            const response = await fetch(`/api/devices/${deviceId}/stats?${query.toString()}`, { cache: 'no-store' });
            const payload = await response.json();
            if (requestKey !== latestAggregateRequestKey) {
                return;
            }
            renderAggregateSummary(payload);
            renderChart(payload.series, payload.chart || { label: 'Потребление', unit: 'кВт·ч', bucket: 'day' });
        } finally {
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
            const response = await fetch(`/api/devices/${deviceId}/live`, { cache: 'no-store' });
            const payload = await response.json();
            renderLiveSummary(payload);
        } finally {
            isLiveLoading = false;
        }
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
        renderChart(initialPayload.series, initialPayload.chart || { label: 'Потребление', unit: 'кВт·ч', bucket: 'hour' });
        updateDayNav();
    } else {
        loadCurrentPeriod();
        loadLive();
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
    setInterval(() => {
        if (document.hidden) {
            return;
        }
        loadLive();
    }, LIVE_REFRESH_INTERVAL_MS);
    setInterval(() => {
        if (document.hidden) {
            return;
        }
        if (!shouldRefreshAggregate()) {
            return;
        }
        loadCurrentPeriod();
    }, AGGREGATE_REFRESH_INTERVAL_MS);
}