const dashboardPage = document.querySelector('[data-dashboard]');

if (dashboardPage) {
    const currentPower = dashboardPage.querySelector('[data-summary-current-power]');
    const monthEnergy = dashboardPage.querySelector('[data-summary-month-energy]');
    const estimatedCost = dashboardPage.querySelector('[data-summary-estimated-cost]');
    const deviceCount = dashboardPage.querySelector('[data-summary-device-count]');
    const deviceGrid = document.querySelector('[data-device-grid]');
    let isLoading = false;

    const escapeHtml = (value) => String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');

    const renderCardMarkup = (device) => `
        <a class="device-image" href="/devices/${encodeURIComponent(device.slug)}" aria-label="Открыть детали устройства ${escapeHtml(device.name)}">
            <span>${escapeHtml(device.image_label)}</span>
        </a>
        <div class="device-card-body">
            <div>
                <p class="device-room" data-device-room>${escapeHtml(device.room)}</p>
                <h3 data-device-name>${escapeHtml(device.name)}</h3>
            </div>
            <dl class="device-metrics">
                <div>
                    <dt>Текущая мощность</dt>
                    <dd data-device-current-power>${escapeHtml(device.current_power_kw)} кВт</dd>
                </div>
                <div>
                    <dt>Энергия за месяц</dt>
                    <dd data-device-month-energy>${escapeHtml(device.month_energy_kwh)} кВт·ч</dd>
                </div>
                <div>
                    <dt>Последний замер</dt>
                    <dd data-device-last-seen>${escapeHtml(device.last_seen || 'Пока нет данных')}</dd>
                </div>
            </dl>
        </div>
    `;

    const updateCard = (card, device) => {
        card.querySelector('.device-image').setAttribute('href', `/devices/${encodeURIComponent(device.slug)}`);
        card.querySelector('.device-image').setAttribute('aria-label', `Открыть детали устройства ${device.name}`);
        card.querySelector('.device-image span').textContent = device.image_label;
        card.querySelector('[data-device-room]').textContent = device.room;
        card.querySelector('[data-device-name]').textContent = device.name;
        card.querySelector('[data-device-current-power]').textContent = `${device.current_power_kw} кВт`;
        card.querySelector('[data-device-month-energy]').textContent = `${device.month_energy_kwh} кВт·ч`;
        card.querySelector('[data-device-last-seen]').textContent = device.last_seen || 'Пока нет данных';
    };

    const syncDevices = (devices) => {
        const existingCards = new Map(
            [...deviceGrid.querySelectorAll('[data-device-card]')].map((card) => [card.dataset.deviceSlug, card])
        );

        if (existingCards.size !== devices.length || devices.some((device) => !existingCards.has(device.slug))) {
            deviceGrid.innerHTML = '';
            devices.forEach((device) => {
                const card = document.createElement('article');
                card.className = 'device-card';
                card.dataset.deviceCard = '';
                card.dataset.deviceSlug = device.slug;
                card.innerHTML = renderCardMarkup(device);
                deviceGrid.appendChild(card);
            });
            return;
        }

        devices.forEach((device) => updateCard(existingCards.get(device.slug), device));
    };

    const loadSummary = async () => {
        if (isLoading) {
            return;
        }
        isLoading = true;
        try {
            const response = await fetch('/api/summary', { cache: 'no-store' });
            const payload = await response.json();
            currentPower.textContent = `${payload.current_power_kw} кВт`;
            monthEnergy.textContent = `${payload.month_energy_kwh} кВт·ч`;
            estimatedCost.textContent = `${payload.estimated_cost}`;
            deviceCount.textContent = `${payload.device_count}`;
            syncDevices(payload.devices || []);
        } finally {
            isLoading = false;
        }
    };

    loadSummary();
    setInterval(loadSummary, 1000);
}