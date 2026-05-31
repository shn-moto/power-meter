-- Mark devices that habitually consume balcony-solar feed-in energy.
-- Used to overlay their combined draw against the generator's curve so
-- you can see when generation actually covers them (and when surplus
-- runs back to the grid).
ALTER TABLE devices ADD COLUMN IF NOT EXISTS is_solar_consumer BOOLEAN NOT NULL DEFAULT FALSE;
