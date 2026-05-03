ALTER TABLE devices ALTER COLUMN power_dps_key   DROP NOT NULL;

ALTER TABLE devices ALTER COLUMN power_scale     DROP NOT NULL;

ALTER TABLE devices ALTER COLUMN voltage_dps_keys DROP NOT NULL;
