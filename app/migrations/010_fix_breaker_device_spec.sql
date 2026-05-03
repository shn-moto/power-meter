UPDATE devices
SET power_dps_key = '102',
    power_scale = 100,
    voltage_dps_keys = '["107", "108", "109"]'::jsonb,
    updated_at = NOW()
WHERE device_id = 'bf47402ca7399b6eef6bw7';

UPDATE device_connections
SET power_dps_key = '102',
    power_scale = 100,
    voltage_dps_keys = '["107", "108", "109"]'::jsonb,
    updated_at = NOW()
WHERE device_id = 'bf47402ca7399b6eef6bw7';