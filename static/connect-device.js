const connectForm = document.querySelector('[data-connect-form]');

if (connectForm) {
    const feedback = document.querySelector('[data-connect-feedback]');
    const configPanel = document.querySelector('[data-connect-config]');
    const configForm = document.querySelector('[data-connect-config-form]');
    const totalPowerSelect = document.querySelector('[data-total-power-select]');
    const visualizedCodesContainer = document.querySelector('[data-visualized-codes]');
    let pendingDeviceId = null;

    const setFeedback = (message, tone) => {
        feedback.hidden = false;
        feedback.className = `connect-feedback is-${tone}`;
        feedback.innerHTML = message;
    };

    const renderConfigOptions = (summaryConfig) => {
        if (!configPanel || !configForm || !totalPowerSelect || !visualizedCodesContainer) {
            return;
        }

        const powerOptions = Array.isArray(summaryConfig?.power_options) ? summaryConfig.power_options : [];
        const visualizationOptions = Array.isArray(summaryConfig?.visualization_options) ? summaryConfig.visualization_options : [];
        const selectedVisualizedCodes = new Set(summaryConfig?.visualized_codes || []);

        totalPowerSelect.innerHTML = '<option value="">Не выбран</option>';
        powerOptions.forEach((option) => {
            const node = document.createElement('option');
            node.value = option.dp_id;
            node.textContent = `${option.dp_id} · ${option.name}${option.code ? ` (${option.code})` : ''}`;
            if (String(summaryConfig?.total_power_dps_key || '') === String(option.dp_id)) {
                node.selected = true;
            }
            totalPowerSelect.appendChild(node);
        });

        visualizedCodesContainer.innerHTML = '<p class="hero-copy">Выберите коды для live-сводки устройства.</p>';
        const optionsList = document.createElement('div');
        optionsList.className = 'config-option-list';
        visualizationOptions.forEach((option) => {
            const label = document.createElement('label');
            label.className = 'config-option';
            const checkbox = document.createElement('input');
            const text = document.createElement('span');
            checkbox.type = 'checkbox';
            checkbox.name = 'visualized_codes';
            checkbox.value = option.dp_id;
            checkbox.checked = selectedVisualizedCodes.has(String(option.dp_id));
            text.textContent = `${option.dp_id} · ${option.name}${option.code ? ` (${option.code})` : ''}`;
            label.append(checkbox, text);
            optionsList.appendChild(label);
        });
        visualizedCodesContainer.appendChild(optionsList);

        configPanel.hidden = false;
    };

    configForm?.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (!pendingDeviceId) {
            return;
        }

        const submitButton = configForm.querySelector('button[type="submit"]');
        const formData = new FormData(configForm);
        const visualizedCodes = formData.getAll('visualized_codes').map((value) => String(value));

        submitButton.disabled = true;
        setFeedback('Сохраняю конфигурацию сводки устройства.', 'pending');

        try {
            const response = await fetch(`/api/devices/${pendingDeviceId}/summary-config`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    total_power_dps_key: String(formData.get('total_power_dps_key') || '').trim() || null,
                    visualized_codes: visualizedCodes,
                }),
            });
            const payload = await response.json();
            if (!response.ok) {
                throw new Error(payload.detail || 'Не удалось сохранить конфигурацию сводки.');
            }

            setFeedback('Конфигурация сводки сохранена.', 'success');
            setTimeout(() => window.location.reload(), 800);
        } catch (error) {
            setFeedback(error.message, 'error');
        } finally {
            submitButton.disabled = false;
        }
    });

    connectForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        const submitButton = connectForm.querySelector('button[type="submit"]');
        const deviceId = String(new FormData(connectForm).get('device_id') || '').trim();
        if (!deviceId) {
            setFeedback('Укажите device ID.', 'error');
            return;
        }

        submitButton.disabled = true;
        setFeedback('Подключение устройства запущено. Это может занять до минуты, пока приложение найдет его в локальной сети.', 'pending');

        try {
            const response = await fetch('/api/devices/connect', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ device_id: deviceId }),
            });
            const payload = await response.json();
            if (!response.ok) {
                throw new Error(payload.detail || 'Не удалось подключить устройство.');
            }

            const message = payload.connection_ready
                ? `
                <strong>${payload.name}</strong> подключено.<br>
                Тип: ${payload.device_kind_label}.<br>
                Локальный IP: ${payload.ip_address}.<br>
                Версия протокола: ${payload.version}.<br>
                Определено возможностей: ${payload.capability_count}.
                `
                : `
                <strong>${payload.name}</strong> добавлено в систему.<br>
                Тип: ${payload.device_kind_label}.<br>
                Определено возможностей: ${payload.capability_count}.<br>
                ${payload.connection_message || 'Локальное обнаружение пока не завершено.'}
                `;

            setFeedback(message, payload.connection_ready ? 'success' : 'pending');
            pendingDeviceId = payload.device_id;
            if (payload.summary_config) {
                renderConfigOptions(payload.summary_config);
            } else {
                setTimeout(() => window.location.reload(), 1200);
            }
        } catch (error) {
            setFeedback(error.message, 'error');
        } finally {
            submitButton.disabled = false;
        }
    });

    document.addEventListener('click', async (event) => {
        const actionButton = event.target.closest('[data-device-action]');
        if (!actionButton) {
            return;
        }

        const action = actionButton.dataset.deviceAction;
        const deviceId = String(actionButton.dataset.deviceId || '').trim();
        const deviceName = String(actionButton.dataset.deviceName || deviceId || 'устройство').trim();
        if (!deviceId) {
            return;
        }

        if (action === 'delete-device') {
            if (!window.confirm(`Удалить устройство ${deviceName}? Все сохраненные данные по нему будут удалены.`)) {
                return;
            }

            actionButton.disabled = true;
            setFeedback(`Удаляю устройство ${deviceName}.`, 'pending');
            try {
                const response = await fetch(`/api/devices/${deviceId}`, {
                    method: 'DELETE',
                });
                const payload = await response.json();
                if (!response.ok) {
                    throw new Error(payload.detail || 'Не удалось удалить устройство.');
                }
                setFeedback(`Устройство ${deviceName} удалено.`, 'success');
                setTimeout(() => window.location.reload(), 500);
            } catch (error) {
                setFeedback(error.message, 'error');
                actionButton.disabled = false;
            }
            return;
        }

        if (action === 'retry-discovery') {
            actionButton.disabled = true;
            setFeedback(`Повторно ищу устройство ${deviceName} в локальной сети.`, 'pending');
            try {
                const response = await fetch(`/api/devices/${deviceId}/retry-discovery`, {
                    method: 'POST',
                });
                const payload = await response.json();
                if (!response.ok) {
                    throw new Error(payload.detail || 'Не удалось повторить локальное обнаружение.');
                }

                const message = payload.connection_ready
                    ? `
                    <strong>${payload.name}</strong> обнаружено в локальной сети.<br>
                    Локальный IP: ${payload.ip_address}.<br>
                    Версия протокола: ${payload.version}.<br>
                    `
                    : `
                    <strong>${payload.name}</strong> пока не найдено в локальной сети.<br>
                    ${payload.connection_message || 'Локальное обнаружение пока не завершено.'}
                    `;

                setFeedback(message, payload.connection_ready ? 'success' : 'pending');
                setTimeout(() => window.location.reload(), payload.connection_ready ? 600 : 1200);
            } catch (error) {
                setFeedback(error.message, 'error');
                actionButton.disabled = false;
            }
        }
    });
}