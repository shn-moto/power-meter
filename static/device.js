const page = document.querySelector('[data-device-page]');

if (page) {
    const LIVE_REFRESH_INTERVAL_MS = 1000;
    const AGGREGATE_REFRESH_INTERVAL_MS = 5000;
    const deviceId = page.dataset.deviceId;
    const initialPayloadNode = document.querySelector('[data-initial-device-stats]');
    const periodInputs = [...page.querySelectorAll('input[data-period]')];
    const customRangeForm = page.querySelector('[data-custom-range]');
    const summary = page.querySelector('[data-device-summary]');
    const functionsContainer = page.querySelector('[data-device-functions]');
    const chart = page.querySelector('[data-chart]');
    const chartEmpty = page.querySelector('[data-chart-empty]');
    const chartMeta = page.querySelector('[data-chart-meta]');
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
    let isAggregateLoading = false;
    let isLiveLoading = false;
    let timerFunction = null;

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
            await loadPeriod(currentPeriod, currentStart, currentEnd);
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
        renderLiveSummary(payload);
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
                    interval: 0,
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
                    })),
                },
            ],
        }, true);
    };

    const loadPeriod = async (period, start, end) => {
        if (isAggregateLoading) {
            return;
        }
        isAggregateLoading = true;
        const query = new URLSearchParams({ period });
        if (start && end) {
            query.set('start', start);
            query.set('end', end);
        }
        try {
            const response = await fetch(`/api/devices/${deviceId}/stats?${query.toString()}`, { cache: 'no-store' });
            const payload = await response.json();
            renderAggregateSummary(payload);
            renderChart(payload.series, payload.chart || { label: 'Потребление', unit: 'кВт·ч', bucket: 'day' });
        } finally {
            isAggregateLoading = false;
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
            customRangeForm.reset();
            loadPeriod(currentPeriod, currentStart, currentEnd);
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
            loadPeriod(currentPeriod, currentStart, currentEnd);
        });
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
        renderChart(initialPayload.series, initialPayload.chart || { label: 'Потребление', unit: 'кВт·ч', bucket: 'hour' });
    } else {
        loadPeriod(currentPeriod, currentStart, currentEnd);
    }
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
        loadPeriod(currentPeriod, currentStart, currentEnd);
    }, AGGREGATE_REFRESH_INTERVAL_MS);
}