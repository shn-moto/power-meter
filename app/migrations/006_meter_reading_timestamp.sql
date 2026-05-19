-- Replace date-only meter readings with full timestamps so the
-- discrepancy table can match meter intervals to device data precisely.
ALTER TABLE meter_readings ADD COLUMN IF NOT EXISTS reading_at TIMESTAMPTZ;

-- Legacy rows: assume the reading happened around 12:00 in the app's local
-- time. User can edit individual entries afterwards.
UPDATE meter_readings
SET reading_at = ((reading_date::timestamp + INTERVAL '12 hours') AT TIME ZONE 'Europe/Warsaw')
WHERE reading_at IS NULL;

ALTER TABLE meter_readings ALTER COLUMN reading_at SET NOT NULL;

-- Drop the old date-based uniqueness; allow several readings on the same
-- day if they happened at different times.
ALTER TABLE meter_readings DROP CONSTRAINT IF EXISTS meter_readings_apartment_reading_date_key;
DROP INDEX IF EXISTS idx_meter_readings_apt_date;
DROP INDEX IF EXISTS idx_meter_readings_settlement;

ALTER TABLE meter_readings ADD CONSTRAINT meter_readings_apartment_reading_at_key
    UNIQUE (apartment, reading_at);

CREATE INDEX IF NOT EXISTS idx_meter_readings_apt_at
    ON meter_readings (apartment, reading_at DESC);

CREATE INDEX IF NOT EXISTS idx_meter_readings_settlement_at
    ON meter_readings (apartment, reading_at DESC) WHERE is_settlement;

-- reading_date is now derived; keep it for now if any consumer reads it,
-- but it's no longer the authoritative timestamp. To drop later:
-- ALTER TABLE meter_readings DROP COLUMN reading_date;
