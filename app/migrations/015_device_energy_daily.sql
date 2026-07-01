-- Daily energy verified against Tuya Cloud add_ele event stream.
-- The cloud logs each ~10-minute add_ele report from the plug even while
-- the plug is offline from our LAN — its internal counter buffer syncs
-- to Tuya on the next reconnect. Summing those event values per local
-- day = the plug's own view of daily kWh, which is our ground truth.

CREATE TABLE IF NOT EXISTS device_energy_daily (
    device_id      TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    day_local      DATE NOT NULL,
    kwh_cloud      DOUBLE PRECISION,
    kwh_local      DOUBLE PRECISION,
    source         TEXT NOT NULL DEFAULT 'local'
                     CHECK (source IN ('cloud', 'local', 'error')),
    verified_at    TIMESTAMPTZ,
    error_message  TEXT,
    PRIMARY KEY (device_id, day_local)
);

CREATE INDEX IF NOT EXISTS device_energy_daily_day_idx
    ON device_energy_daily (day_local DESC);
