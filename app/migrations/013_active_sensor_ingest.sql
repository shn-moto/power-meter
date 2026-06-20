-- Active-pusher devices: gear that POSTs its readings to us instead of being
-- LAN-polled. local_key / ip_address stay empty (so the poll loop skips them),
-- and authentication for incoming samples uses this per-device token.
ALTER TABLE device_connections
    ADD COLUMN IF NOT EXISTS ingest_token TEXT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_device_connections_ingest_token
    ON device_connections(ingest_token)
    WHERE ingest_token IS NOT NULL;
