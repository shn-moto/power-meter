-- Energy-generating devices (solar inverter feed-in plugs) — their accumulated
-- kWh counter is treated as production, subtracted from household totals and
-- shown in its own dashboard section.
ALTER TABLE devices ADD COLUMN IF NOT EXISTS is_generator BOOLEAN NOT NULL DEFAULT FALSE;
