-- Per-device multiplier applied at poll-time to the device's reported power
-- reading before we trust it as `samples.power_w`. Default 1.0 = no change.
-- Used to compensate for sockets whose chip miscalibrates real power on
-- switch-mode loads (cheap Tuya Wi-Fi plug under-reads by ~2× on the 72V
-- battery charger — proven against the Atorch readout with no inverter load).
ALTER TABLE device_connections
    ADD COLUMN IF NOT EXISTS power_correction_factor DOUBLE PRECISION NOT NULL DEFAULT 1.0;
