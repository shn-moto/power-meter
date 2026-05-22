-- Zigbee sub-devices addressable by their node_id (CID), not the Tuya UUID.
-- The Tuya gateway's LAN session requires the real 16-char CID; the device_id
-- is only meaningful in the cloud catalogue.
ALTER TABLE device_connections
    ADD COLUMN IF NOT EXISTS cid TEXT NULL;
