window.DeviceControls = (() => {
    const tuyaColourToHexRgb = (raw) => {
        if (!raw || raw.length < 12) {
            return '#ffffff';
        }
        const hue = parseInt(raw.slice(0, 4), 16);
        const sat = parseInt(raw.slice(4, 8), 16) / 1000;
        const val = parseInt(raw.slice(8, 12), 16) / 1000;
        if (Number.isNaN(hue) || Number.isNaN(sat) || Number.isNaN(val)) {
            return '#ffffff';
        }
        const c = val * sat;
        const x = c * (1 - Math.abs(((hue / 60) % 2) - 1));
        const m = val - c;
        let r = 0;
        let g = 0;
        let b = 0;
        if (hue < 60) { r = c; g = x; }
        else if (hue < 120) { r = x; g = c; }
        else if (hue < 180) { g = c; b = x; }
        else if (hue < 240) { g = x; b = c; }
        else if (hue < 300) { r = x; b = c; }
        else { r = c; b = x; }
        const toHex = (v) => Math.round((v + m) * 255).toString(16).padStart(2, '0');
        return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
    };

    const create = ({ deviceId, container, timerDialog, timerForm, timerCancel, onAfterCommand }) => {
        if (!container) {
            return { render: () => {}, sync: () => {} };
        }

        let busy = false;
        let timerFunction = null;

        const setBusy = (value) => {
            busy = value;
            container.querySelectorAll('input, select, button').forEach((node) => {
                node.disabled = value;
            });
        };

        const runFunction = async (code, value) => {
            setBusy(true);
            try {
                const response = await fetch(`/api/devices/${encodeURIComponent(deviceId)}/functions/${encodeURIComponent(code)}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ value }),
                });
                const payload = await response.json().catch(() => ({}));
                if (!response.ok) {
                    throw new Error(payload.detail || 'Не удалось выполнить действие.');
                }
            } catch (error) {
                window.alert(error.message);
            } finally {
                setBusy(false);
            }
            // Fire the post-command refresh in the background so it doesn't
            // gate re-enabling the controls.
            if (typeof onAfterCommand === 'function') {
                Promise.resolve(onAfterCommand()).catch(() => {});
            }
        };

        const render = (items) => {
            container.innerHTML = '';
            if (!items || !items.length) {
                container.innerHTML = '<p class="device-functions-empty">Для этого устройства управляемые функции пока не определены.</p>';
                return;
            }

            items.forEach((item) => {
                const card = document.createElement('section');
                card.className = 'device-function-item';
                card.dataset.functionCode = item.code;

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
                    const slider = document.createElement('span');
                    slider.className = 'switch-slider';
                    const caption = document.createElement('span');
                    caption.className = 'switch-caption';
                    caption.textContent = checkbox.checked ? 'Вкл' : 'Выкл';
                    checkbox.addEventListener('change', () => {
                        caption.textContent = checkbox.checked ? 'Вкл' : 'Выкл';
                        runFunction(item.code, checkbox.checked);
                    });
                    label.append(checkbox, slider, caption);
                    controls.appendChild(label);
                }

                if (item.control_type === 'timer' && timerDialog && timerForm) {
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

                if (item.control_type === 'slider') {
                    const range = document.createElement('input');
                    range.type = 'range';
                    range.className = 'device-function-slider';
                    range.min = String(item.min || 0);
                    range.max = String(item.max || 100);
                    range.step = String(item.step || 1);
                    range.value = String(Number(item.current_value) || 0);
                    const valueOut = document.createElement('span');
                    valueOut.className = 'device-function-slider-value';
                    valueOut.textContent = range.value;
                    range.addEventListener('input', () => { valueOut.textContent = range.value; });
                    range.addEventListener('change', () => {
                        runFunction(item.code, Number(range.value));
                    });
                    controls.appendChild(range);
                    controls.appendChild(valueOut);
                }

                if (item.control_type === 'enum') {
                    const select = document.createElement('select');
                    select.className = 'device-function-select';
                    (item.options || []).forEach((option) => {
                        const opt = document.createElement('option');
                        opt.value = option.value;
                        opt.textContent = option.label;
                        select.appendChild(opt);
                    });
                    select.value = String(item.current_value || '');
                    select.addEventListener('change', () => {
                        runFunction(item.code, select.value);
                    });
                    controls.appendChild(select);
                }

                if (item.control_type === 'color') {
                    const picker = document.createElement('input');
                    picker.type = 'color';
                    picker.className = 'device-function-color';
                    picker.value = tuyaColourToHexRgb(String(item.current_value || ''));
                    picker.addEventListener('change', () => {
                        runFunction(item.code, picker.value);
                    });
                    controls.appendChild(picker);
                }

                card.appendChild(controls);
                container.appendChild(card);
            });
        };

        const sync = (items) => {
            const cards = [...container.querySelectorAll('[data-function-code]')];
            const byCode = new Map(cards.map((node) => [node.dataset.functionCode, node]));
            if (cards.length !== items.length || items.some((item) => !byCode.has(item.code))) {
                render(items);
                return;
            }

            items.forEach((item) => {
                const card = byCode.get(item.code);
                if (!card) {
                    return;
                }
                const state = card.querySelector('.device-function-state');
                if (state) {
                    state.textContent = item.current_label;
                }
                if (item.control_type === 'toggle') {
                    const checkbox = card.querySelector('input[type="checkbox"]');
                    const caption = card.querySelector('.switch-caption');
                    if (checkbox && document.activeElement !== checkbox) {
                        checkbox.checked = Boolean(item.current_value);
                    }
                    if (caption && checkbox) {
                        caption.textContent = checkbox.checked ? 'Вкл' : 'Выкл';
                    }
                }
                if (item.control_type === 'slider') {
                    const range = card.querySelector('input[type="range"]');
                    if (range && document.activeElement !== range) {
                        range.value = String(Number(item.current_value) || 0);
                    }
                    const valueOut = card.querySelector('.device-function-slider-value');
                    if (valueOut && range) {
                        valueOut.textContent = range.value;
                    }
                }
                if (item.control_type === 'enum') {
                    const select = card.querySelector('select');
                    if (select && document.activeElement !== select) {
                        select.value = String(item.current_value || '');
                    }
                }
                if (item.control_type === 'color') {
                    const picker = card.querySelector('input[type="color"]');
                    if (picker && document.activeElement !== picker) {
                        picker.value = tuyaColourToHexRgb(String(item.current_value || ''));
                    }
                }
            });
        };

        if (timerCancel && timerDialog) {
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

        return { render, sync };
    };

    return { create, tuyaColourToHexRgb };
})();
