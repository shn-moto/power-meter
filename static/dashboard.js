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

    const applyReadingStatus = (node, status) => {
        node.classList.remove('is-ok', 'is-warning', 'is-error');
        node.classList.add('reading-status', `is-${status || 'error'}`);
    };

    const renderDeviceMedia = (device) => {
        if (device.image_url) {
            return `<img class="device-photo" src="${escapeHtml(device.image_url)}" alt="${escapeHtml(device.name)}">`;
        }
        return `<div class="device-placeholder device-placeholder-${escapeHtml(device.device_kind || 'switch')}" aria-hidden="true"></div>`;
    };

    const renderCardMarkup = (device) => `
        <a class="device-image" href="/devices/${encodeURIComponent(device.device_id)}" aria-label="Открыть детали устройства ${escapeHtml(device.name)}">
            ${renderDeviceMedia(device)}
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
                    <dd class="reading-status is-${escapeHtml(device.last_seen_status || 'error')}" data-device-last-seen>${escapeHtml(device.last_seen || 'Пока нет данных')}</dd>
                </div>
            </dl>
        </div>
    `;

    const updateCard = (card, device) => {
        card.innerHTML = renderCardMarkup(device);
    };

    const syncDevices = (devices) => {
        const existingCards = new Map(
            [...deviceGrid.querySelectorAll('[data-device-card]')].map((card) => [card.dataset.deviceId, card])
        );

        if (existingCards.size !== devices.length || devices.some((device) => !existingCards.has(device.device_id))) {
            deviceGrid.innerHTML = '';
            devices.forEach((device) => {
                const card = document.createElement('article');
                card.className = 'device-card';
                card.dataset.deviceCard = '';
                card.dataset.deviceId = device.device_id;
                card.innerHTML = renderCardMarkup(device);
                deviceGrid.appendChild(card);
            });
            return;
        }

        devices.forEach((device) => updateCard(existingCards.get(device.device_id), device));
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