const page = document.querySelector('[data-device-page]');

if (page) {
    const slug = page.dataset.deviceSlug;
    const periodInputs = [...page.querySelectorAll('input[data-period]')];
    const customRangeForm = page.querySelector('[data-custom-range]');
    const summary = page.querySelector('[data-device-summary]');
    const functionsContainer = page.querySelector('[data-device-functions]');
    const chart = page.querySelector('[data-chart]');
    const chartGrid = page.querySelector('[data-chart-grid]');
    const chartBars = page.querySelector('[data-chart-bars]');
    const chartXAxis = page.querySelector('[data-chart-x-axis]');
    const chartYAxis = page.querySelector('[data-chart-y-axis]');
    const chartEmpty = page.querySelector('[data-chart-empty]');
    const chartMeta = page.querySelector('[data-chart-meta]');
    const chartTooltip = page.querySelector('[data-chart-tooltip]');
    const timerDialog = document.querySelector('[data-timer-dialog]');
    const timerForm = document.querySelector('[data-timer-form]');
    const timerCancel = document.querySelector('[data-timer-cancel]');
    let currentPeriod = 'month';
    let currentStart = null;
    let currentEnd = null;
    let isLoading = false;
    let timerFunction = null;

    const formatValue = (value, suffix = '') => {
        if (value === null || value === undefined || Number.isNaN(value)) {
            return '--';
        }
        return `${value}${suffix}`;
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

    const formatTimestamp = (timestamp, bucket) => {
        const value = new Date(timestamp);
        if (bucket === 'hour') {
            return new Intl.DateTimeFormat('ru-RU', { hour: '2-digit' }).format(value);
        }
        if (bucket === 'day') {
            return new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short' }).format(value);
        }
        return new Intl.DateTimeFormat('ru-RU', { month: 'short' }).format(value);
    };

    const formatTooltipTimestamp = (timestamp, bucket) => {
        const value = new Date(timestamp);
        if (bucket === 'hour') {
            return new Intl.DateTimeFormat('ru-RU', {
                day: 'numeric',
                month: 'long',
                hour: '2-digit',
            }).format(value);
        }
        if (bucket === 'day') {
            return new Intl.DateTimeFormat('ru-RU', {
                day: 'numeric',
                month: 'long',
                year: 'numeric',
            }).format(value);
        }
        return new Intl.DateTimeFormat('ru-RU', {
            month: 'long',
            year: 'numeric',
        }).format(value);
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

    const hideTooltip = () => {
        chartTooltip.hidden = true;
    };

    const showTooltip = (event, item, chartConfig) => {
        const chartBounds = chart.getBoundingClientRect();
        const left = Math.min(
            Math.max(event.clientX - chartBounds.left + 14, 16),
            chartBounds.width - 180,
        );
        const top = Math.max(event.clientY - chartBounds.top - 54, 12);
        chartTooltip.innerHTML = `
            <strong>${formatTooltipTimestamp(item.timestamp, chartConfig.bucket)}</strong>
            <span>${chartConfig.label}: ${formatNumber(Number(item.chart_value || 0))} ${chartConfig.unit}</span>
        `;
        chartTooltip.style.left = `${left}px`;
        chartTooltip.style.top = `${top}px`;
        chartTooltip.hidden = false;
    };

    const setFunctionButtonState = (disabled) => {
        functionsContainer.querySelectorAll('button, input[type="checkbox"]').forEach((node) => {
            node.disabled = disabled;
        });
    };

    const runFunction = async (code, value) => {
        setFunctionButtonState(true);
        try {
            const response = await fetch(`/api/devices/${slug}/functions/${code}`, {
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

    const renderSummary = (payload) => {
        const fields = payload.summary;
        const values = [
            `${fields.energy_kwh} кВт·ч`,
            `${fields.average_power_kw} кВт`,
            `${fields.peak_power_kw} кВт`,
            formatValue(fields.average_voltage_v, ' В'),
            String(fields.sample_count),
            fields.latest_sample || '--',
        ];

        [...summary.querySelectorAll('dd')].forEach((node, index) => {
            node.textContent = values[index];
        });

        renderFunctions(payload.device_functions || []);
    };

    const renderChart = (series, chartConfig) => {
        chartGrid.innerHTML = '';
        chartBars.innerHTML = '';
        chartXAxis.innerHTML = '';
        chartYAxis.innerHTML = '';
        const values = series.map((item) => Number(item.chart_value ?? 0));
        const maxValue = Math.max(...values, 0);
        if (!series.length || maxValue <= 0) {
            chartEmpty.hidden = false;
            hideTooltip();
            chartMeta.textContent = 'Потребление по интервалам';
            return;
        }

        chartEmpty.hidden = true;
        const width = 960;
        const height = 320;
        chartMeta.textContent = `${chartConfig.label} по ${describeBucket(chartConfig.bucket)}`;
        const padding = { top: 24, right: 24, bottom: 54, left: 74 };
        const plotWidth = width - padding.left - padding.right;
        const plotHeight = height - padding.top - padding.bottom;
        const yMax = maxValue * 1.1;
        const stepCount = 4;

        for (let step = 0; step <= stepCount; step += 1) {
            const y = padding.top + (plotHeight / stepCount) * step;
            const tickValue = yMax - (yMax / stepCount) * step;
            const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            line.setAttribute('x1', String(padding.left));
            line.setAttribute('x2', String(width - padding.right));
            line.setAttribute('y1', String(y));
            line.setAttribute('y2', String(y));
            chartGrid.appendChild(line);

            const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            label.setAttribute('x', String(padding.left - 14));
            label.setAttribute('y', String(y + 4));
            label.setAttribute('text-anchor', 'end');
            label.textContent = `${formatNumber(tickValue)} ${chartConfig.unit}`;
            chartYAxis.appendChild(label);
        }

        const slotWidth = plotWidth / series.length;
        const barWidth = Math.max(Math.min(slotWidth * 0.72, 42), 10);
        const labelStep = Math.max(1, Math.ceil(series.length / 8));

        series.forEach((item, index) => {
            const value = Number(item.chart_value ?? 0);
            const barHeight = value > 0 ? Math.max((value / yMax) * plotHeight, 3) : 0;
            const x = padding.left + slotWidth * index + (slotWidth - barWidth) / 2;
            const y = padding.top + plotHeight - barHeight;

            const bar = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            bar.setAttribute('x', String(x));
            bar.setAttribute('y', String(y));
            bar.setAttribute('width', String(barWidth));
            bar.setAttribute('height', String(barHeight));
            bar.setAttribute('rx', '10');
            bar.setAttribute('class', 'chart-bar');
            bar.setAttribute('tabindex', '0');
            bar.addEventListener('mouseenter', (event) => showTooltip(event, item, chartConfig));
            bar.addEventListener('mousemove', (event) => showTooltip(event, item, chartConfig));
            bar.addEventListener('mouseleave', hideTooltip);
            bar.addEventListener('focus', () => {
                const rect = bar.getBoundingClientRect();
                showTooltip({ clientX: rect.left + rect.width / 2, clientY: rect.top }, item, chartConfig);
            });
            bar.addEventListener('blur', hideTooltip);
            chartBars.appendChild(bar);

            if (index % labelStep === 0 || index === series.length - 1) {
                const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                label.setAttribute('x', String(x + barWidth / 2));
                label.setAttribute('y', String(height - 18));
                label.setAttribute('text-anchor', 'middle');
                label.textContent = formatTimestamp(item.timestamp, chartConfig.bucket);
                chartXAxis.appendChild(label);
            }
        });
    };

    const loadPeriod = async (period, start, end) => {
        if (isLoading) {
            return;
        }
        isLoading = true;
        const query = new URLSearchParams({ period });
        if (start && end) {
            query.set('start', start);
            query.set('end', end);
        }
        try {
            const response = await fetch(`/api/devices/${slug}/stats?${query.toString()}`, { cache: 'no-store' });
            const payload = await response.json();
            renderSummary(payload);
            renderChart(payload.series, payload.chart || { label: 'Потребление', unit: 'кВт·ч', bucket: 'day' });
        } finally {
            isLoading = false;
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

    loadPeriod(currentPeriod, currentStart, currentEnd);
    setInterval(() => {
        loadPeriod(currentPeriod, currentStart, currentEnd);
    }, 1000);
}