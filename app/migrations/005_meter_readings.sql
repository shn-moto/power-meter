CREATE TABLE IF NOT EXISTS meter_readings (
    id BIGSERIAL PRIMARY KEY,
    apartment TEXT NOT NULL,
    reading_date DATE NOT NULL,
    reading_kwh NUMERIC(12, 3) NOT NULL,
    is_settlement BOOLEAN NOT NULL DEFAULT FALSE,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (apartment, reading_date)
);

CREATE INDEX IF NOT EXISTS idx_meter_readings_apt_date
    ON meter_readings (apartment, reading_date DESC);

CREATE INDEX IF NOT EXISTS idx_meter_readings_settlement
    ON meter_readings (apartment, reading_date DESC) WHERE is_settlement;
