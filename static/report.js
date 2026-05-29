const reportPage = document.querySelector('[data-report-page]');

if (reportPage) {
    const payloadNode = document.querySelector('[data-report-payload]');
    if (!payloadNode) {
        // nothing to render
    } else {
        const payload = JSON.parse(payloadNode.textContent || '{}');

        const palette = {
            consumed: '#e8a838',
            generated: '#67b86b',
            net: '#7fd0ff',
            grid: 'rgba(127, 208, 255, 0.12)',
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

        const buildBarChart = (container, series, formatter, emptyNode) => {
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
            chart.setOption({
                tooltip: {
                    trigger: 'axis',
                    backgroundColor: 'rgba(11, 18, 26, 0.92)',
                    borderColor: 'rgba(127, 208, 255, 0.22)',
                    textStyle: { color: '#e8f0fa' },
                },
                legend: {
                    data: ['Потребление', 'Генерация', 'Нетто'],
                    textStyle: { color: palette.text },
                    top: 4,
                },
                grid: { left: 56, right: 24, top: 36, bottom: 28 },
                xAxis: {
                    type: 'category',
                    data: labels,
                    axisLine: { lineStyle: { color: palette.grid } },
                    axisLabel: { color: palette.text },
                },
                yAxis: {
                    type: 'value',
                    name: 'кВт·ч',
                    nameTextStyle: { color: palette.text },
                    axisLine: { lineStyle: { color: palette.grid } },
                    splitLine: { lineStyle: { color: palette.grid } },
                    axisLabel: { color: palette.text },
                },
                series: [
                    {
                        name: 'Потребление',
                        type: 'bar',
                        stack: 'energy',
                        data: series.map((p) => Number(p.consumed_kwh || 0).toFixed(2)),
                        itemStyle: { color: palette.consumed },
                    },
                    {
                        name: 'Генерация',
                        type: 'bar',
                        stack: 'energy',
                        data: series.map((p) => -Number(p.generated_kwh || 0).toFixed(2)),
                        itemStyle: { color: palette.generated },
                    },
                    {
                        name: 'Нетто',
                        type: 'line',
                        data: series.map((p) => Number(p.net_kwh || 0).toFixed(2)),
                        smooth: true,
                        lineStyle: { color: palette.net, width: 2 },
                        itemStyle: { color: palette.net },
                        symbol: 'circle',
                        symbolSize: 6,
                    },
                ],
            });
            window.addEventListener('resize', () => chart.resize());
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
