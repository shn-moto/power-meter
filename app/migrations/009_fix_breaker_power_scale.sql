UPDATE devices
SET power_scale = 100
WHERE category_code = 'dlq'
  AND power_dps_key = '102'
  AND COALESCE(power_scale, 1) = 1;

UPDATE device_connections
SET power_scale = 100,
    updated_at = NOW()
WHERE device_id IN (
    SELECT device_id
    FROM devices
    WHERE category_code = 'dlq'
      AND power_dps_key = '102'
)
  AND COALESCE(power_scale, 1) = 1;