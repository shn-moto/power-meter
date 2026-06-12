const page = document.querySelector('[data-automations-page]');

if (page) {
    const post = async (url, body) => {
        const r = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        });
        if (!r.ok) {
            const detail = (await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`;
            throw new Error(detail);
        }
        return r.json();
    };

    page.addEventListener('change', async (event) => {
        const target = event.target;
        const row = target.closest('[data-automation-row]');
        if (!row) return;
        const slug = row.dataset.slug;

        if (target.matches('[data-automation-bind]')) {
            try {
                await post(`/api/automations/${encodeURIComponent(slug)}/bind`, { device_id: target.value || null });
            } catch (err) {
                window.alert(err.message);
            }
        } else if (target.matches('[data-automation-enabled]')) {
            const next = target.checked;
            try {
                await post(`/api/automations/${encodeURIComponent(slug)}/enable`, { enabled: next });
                row.querySelector('.switch-caption').textContent = next ? 'Вкл' : 'Выкл';
            } catch (err) {
                target.checked = !next;
                window.alert(err.message);
            }
        }
    });

    page.addEventListener('click', async (event) => {
        const target = event.target;
        const row = target.closest('[data-automation-row]');
        if (!row) return;
        const slug = row.dataset.slug;

        if (target.matches('[data-automation-run]')) {
            target.disabled = true;
            try {
                const result = await post(`/api/automations/${encodeURIComponent(slug)}/run`, {});
                if (result.status === 'started') {
                    target.textContent = 'Выполняется…';
                    // Reload after a short delay so the user sees the
                    // last_run_at/status update once the script finishes.
                    // For multi-hour runs you'd need to refresh manually.
                    setTimeout(() => window.location.reload(), 4000);
                } else {
                    window.alert(`${result.status}: ${result.log || '(нет лога)'}`);
                    window.location.reload();
                }
            } catch (err) {
                window.alert(err.message);
                target.disabled = false;
            }
        }
    });

    // Commit cron on blur so we don't fire on every keypress.
    page.querySelectorAll('[data-automation-cron]').forEach((input) => {
        const original = input.value;
        input.addEventListener('blur', async () => {
            if (input.value === original) return;
            const row = input.closest('[data-automation-row]');
            const slug = row.dataset.slug;
            try {
                await post(`/api/automations/${encodeURIComponent(slug)}/cron`, { cron_schedule: input.value });
            } catch (err) {
                input.value = original;
                window.alert(err.message);
            }
        });
    });
}
