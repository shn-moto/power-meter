-- Zigbee gateway support. Sub-devices reach the LAN through their
-- gateway, so they don't carry their own local_key / IP; instead they
-- point at the gateway by id.
ALTER TABLE devices ADD COLUMN IF NOT EXISTS is_gateway BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE device_connections ADD COLUMN IF NOT EXISTS gateway_device_id TEXT NULL
    REFERENCES devices(device_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_device_connections_gateway
    ON device_connections(gateway_device_id);
