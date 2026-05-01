const page = document.querySelector('[data-device-page]');

if (page) {
    const slug = page.dataset.deviceSlug;
    const buttons = [...page.querySelectorAll('[data-period]')];
    const customRangeForm = page.querySelector('[data-custom-range]');
    const summary = page.querySelector('[data-device-summary]');
    const rawDps = page.querySelector('[data-raw-dps]');
    const chart = page.querySelector('[data-chart]');
    const chartGrid = page.querySelector('[data-chart-grid]');
    const chartLine = page.querySelector('[data-chart-line]');
    const chartEmpty = page.querySelector('[data-chart-empty]');

    const formatValue = (value, suffix = '') => {
        if (value === null || value === undefined || Number.isNaN(value)) {
            return '--';
        }
        return `${value}${suffix}`;
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

        rawDps.textContent = JSON.stringify(payload.summary.latest_raw_dps, null, 2);
    };

    const renderChart = (series) => {
        chartGrid.innerHTML = '';
        if (!series.length) {
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
        const maxValue = Math.max(...series.map((item) => item.energy_kwh), 0.1);

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
            const y = padding.top + plotHeight - (item.energy_kwh / maxValue) * plotHeight;
            return `${x},${y}`;
        });
        chartLine.setAttribute('points', points.join(' '));
    };

    const loadPeriod = async (period, start, end) => {
        const query = new URLSearchParams({ period });
        if (start && end) {
            query.set('start', start);
            query.set('end', end);
        }
        const response = await fetch(`/api/devices/${slug}/stats?${query.toString()}`);
        const payload = await response.json();
        renderSummary(payload);
        renderChart(payload.series);
    };

    buttons.forEach((button) => {
        button.addEventListener('click', () => {
            buttons.forEach((item) => item.classList.remove('is-active'));
            button.classList.add('is-active');
            loadPeriod(button.dataset.period);
        });
    });

    customRangeForm.addEventListener('submit', (event) => {
        event.preventDefault();
        const formData = new FormData(customRangeForm);
        buttons.forEach((item) => item.classList.remove('is-active'));
        loadPeriod('custom', formData.get('start'), formData.get('end'));
    });

    loadPeriod('month');
}