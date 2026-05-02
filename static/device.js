const page = document.querySelector('[data-device-page]');

if (page) {
    const slug = page.dataset.deviceSlug;
    const buttons = [...page.querySelectorAll('[data-period]')];
    const customRangeForm = page.querySelector('[data-custom-range]');
    const summary = page.querySelector('[data-device-summary]');
    const functionsContainer = page.querySelector('[data-device-functions]');
    const chart = page.querySelector('[data-chart]');
    const chartGrid = page.querySelector('[data-chart-grid]');
    const chartLine = page.querySelector('[data-chart-line]');
    const chartEmpty = page.querySelector('[data-chart-empty]');
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

    const renderChart = (series) => {
        chartGrid.innerHTML = '';
        const values = series.map((item) => Number(item.chart_value ?? 0));
        const maxValue = Math.max(...values, 0);
        if (!series.length || maxValue <= 0) {
            chartEmpty.hidden = false;
            chartLine.setAttribute('points', '');
            return;
        }

        chartEmpty.hidden = true;
        const width = 960;
        const height = 320;
        const padding = { top: 24, right: 24, bottom: 30, left: 32 };
        const plotWidth = width - padding.left - padding.right;
        const plotHeight = height - padding.top - padding.bottom;
        const yMax = maxValue * 1.1;

        for (let step = 0; step <= 4; step += 1) {
            const y = padding.top + (plotHeight / 4) * step;
            const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            line.setAttribute('x1', String(padding.left));
            line.setAttribute('x2', String(width - padding.right));
            line.setAttribute('y1', String(y));
            line.setAttribute('y2', String(y));
            chartGrid.appendChild(line);
        }

        const points = series.map((item, index) => {
            const x = padding.left + (plotWidth * index) / Math.max(series.length - 1, 1);
            const value = Number(item.chart_value ?? 0);
            const y = padding.top + plotHeight - (value / yMax) * plotHeight;
            return `${x},${y}`;
        });
        chartLine.setAttribute('points', points.join(' '));
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
            renderChart(payload.series);
        } finally {
            isLoading = false;
        }
    };

    buttons.forEach((button) => {
        button.addEventListener('click', () => {
            buttons.forEach((item) => item.classList.remove('is-active'));
            button.classList.add('is-active');
            currentPeriod = button.dataset.period;
            currentStart = null;
            currentEnd = null;
            loadPeriod(currentPeriod, currentStart, currentEnd);
        });
    });

    customRangeForm.addEventListener('submit', (event) => {
        event.preventDefault();
        const formData = new FormData(customRangeForm);
        buttons.forEach((item) => item.classList.remove('is-active'));
        currentPeriod = 'custom';
        currentStart = formData.get('start');
        currentEnd = formData.get('end');
        loadPeriod(currentPeriod, currentStart, currentEnd);
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