const reportPage = document.querySelector('[data-report-page]');

if (reportPage) {
    const payloadNode = document.querySelector('[data-report-payload]');
    const yearSelect = document.querySelector('[data-report-year]');
    const monthSelect = document.querySelector('[data-report-month]');

    const navigateToPeriod = (year, month) => {
        const url = new URL(window.location.href);
        url.searchParams.set('year', String(year));
        url.searchParams.set('month', String(month));
        window.location.href = url.toString();
    };

    if (yearSelect) {
        yearSelect.addEventListener('change', () => {
            navigateToPeriod(Number(yearSelect.value), Number(monthSelect?.value || 1));
        });
    }
    if (monthSelect) {
        monthSelect.addEventListener('change', () => {
            navigateToPeriod(Number(yearSelect?.value || new Date().getFullYear()), Number(monthSelect.value));
        });
    }

    if (!payloadNode) {
        // nothing to render
    } else {
        const payload = JSON.parse(payloadNode.textContent || '{}');

        // Same palette as the inverter sensor-history page's classic
        // energy-balance bars so the mental model translates between
        // pages: red = load covered by solar (up), amber = grid buy (up),
        // green = solar generation (down).
        const palette = {
            self_covered: '#ff7a7a',    // red — load offset by solar
            grid: '#e8a838',            // amber — load supplied by grid
            solar: '#67b86b',           // green — generation
            gridline: 'rgba(127, 208, 255, 0.12)',
            text: 'rgba(232, 240, 250, 0.78)',
        };

        const formatMonth = (iso) => {
            try {
                const d = new Date(iso);
                return d.toLocaleString('ru-RU', { month: 'short', year: 'numeric' });
            } catch {
                return iso;
            }
        };

        const formatDay = (iso) => {
            try {
                const d = new Date(iso);
                return String(d.getDate()).padStart(2, '0');
            } catch {
                return iso;
            }
        };

        const buildBarChart = (container, series, formatter, emptyNode, onBarClick) => {
            if (!container) {
                return;
            }
            if (!series || !series.length) {
                if (emptyNode) emptyNode.hidden = false;
                return;
            }
            if (emptyNode) emptyNode.hidden = true;
            const chart = window.echarts.init(container);
            const labels = series.map((p) => formatter(p.bucket));
            // Classic energy-balance layout, one column per bucket:
            //   up bar = load (grid-delta on the bottom + solar-covered
            //            remainder on top, sum = consumed_kwh)
            //   down bar = solar generation
            // Shared stack name so positives stack up from zero and the
            // negative solar bar extends down from the same x.
            const gridData = series.map((p) => Number(p.delta_kwh || 0));
            const selfData = series.map((p) => Math.max(
                0, Number(p.consumed_kwh || 0) - Number(p.delta_kwh || 0)
            ));
            const solarData = series.map((p) => -Math.abs(Number(p.solar_kwh || 0)));
            chart.setOption({
                tooltip: {
                    trigger: 'axis',
                    axisPointer: { type: 'shadow' },
                    backgroundColor: 'rgba(11, 18, 26, 0.92)',
                    borderColor: 'rgba(127, 208, 255, 0.22)',
                    textStyle: { color: '#e8f0fa' },
                    formatter: (params) => {
                        if (!params || !params.length) return '';
                        const lines = [`<strong>${params[0].axisValue}</strong>`];
                        params.forEach((p) => {
                            const raw = Array.isArray(p.value) ? p.value[1] : p.value;
                            const abs = Math.abs(Number(raw) || 0);
                            lines.push(`${p.marker} ${p.seriesName}: ${abs.toFixed(2)} кВт·ч`);
                        });
                        return lines.join('<br/>');
                    },
                },
                legend: {
                    data: ['Из сети', 'Покрыто солнцем', 'Солнце'],
                    textStyle: { color: palette.text },
                    top: 4,
                },
                grid: { left: 56, right: 24, top: 36, bottom: 28 },
                xAxis: {
                    type: 'category',
                    data: labels,
                    axisLine: { lineStyle: { color: palette.gridline } },
                    axisLabel: { color: palette.text, hideOverlap: true },
                },
                yAxis: {
                    type: 'value',
                    name: 'кВт·ч',
                    nameTextStyle: { color: palette.text },
                    axisLine: { lineStyle: { color: palette.gridline } },
                    splitLine: { lineStyle: { color: palette.gridline } },
                    axisLabel: {
                        color: palette.text,
                        formatter: (v) => Math.abs(v).toFixed(1),
                    },
                },
                series: [
                    {
                        name: 'Из сети',
                        type: 'bar',
                        stack: 'balance',
                        data: gridData,
                        itemStyle: { color: palette.grid },
                    },
                    {
                        name: 'Покрыто солнцем',
                        type: 'bar',
                        stack: 'balance',
                        data: selfData,
                        itemStyle: { color: palette.self_covered },
                    },
                    {
                        name: 'Солнце',
                        type: 'bar',
                        stack: 'balance',
                        data: solarData,
                        itemStyle: { color: palette.solar },
                    },
                ],
            });
            if (typeof onBarClick === 'function') {
                chart.getZr().setCursorStyle('pointer');
                chart.on('click', (params) => {
                    if (params && typeof params.dataIndex === 'number') {
                        const item = series[params.dataIndex];
                        if (item) onBarClick(item);
                    }
                });
            }
            window.addEventListener('resize', () => chart.resize());
        };

        const handleMonthlyBarClick = (item) => {
            const d = new Date(item.bucket);
            if (Number.isFinite(d.getTime())) {
                navigateToPeriod(d.getFullYear(), d.getMonth() + 1);
            }
        };

        const render = () => {
            buildBarChart(
                document.querySelector('[data-report-daily-chart]'),
                payload.daily_series,
                formatDay,
                document.querySelector('[data-report-daily-empty]'),
            );
            buildBarChart(
                document.querySelector('[data-report-monthly-chart]'),
                payload.monthly_series,
                formatMonth,
                document.querySelector('[data-report-monthly-empty]'),
                handleMonthlyBarClick,
            );
        };

        if (window.echarts) {
            render();
        } else {
            // Wait for ECharts CDN script to load
            const interval = setInterval(() => {
                if (window.echarts) {
                    clearInterval(interval);
                    render();
                }
            }, 50);
        }
    }
}
