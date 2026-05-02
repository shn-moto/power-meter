const connectForm = document.querySelector('[data-connect-form]');

if (connectForm) {
    const feedback = document.querySelector('[data-connect-feedback]');

    const setFeedback = (message, tone) => {
        feedback.hidden = false;
        feedback.className = `connect-feedback is-${tone}`;
        feedback.innerHTML = message;
    };

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
            setTimeout(() => window.location.reload(), 1200);
        } catch (error) {
            setFeedback(error.message, 'error');
        } finally {
            submitButton.disabled = false;
        }
    });
}