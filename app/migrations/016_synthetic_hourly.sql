-- Synthetic per-hour energy entries used to visualise cloud-verified
-- restored consumption alongside real LAN samples.
--
-- For each device × day where the Tuya-Cloud add_ele sum exceeded what
-- our LAN samples caught (Wi-Fi outage the plug buffered through and
-- flushed to cloud on reconnect), the cloud-verify job inserts one row
-- here at the end-of-day hour bucket, carrying the restoration delta.
-- The chart-series builder merges these into the normal hourly rendering
-- and tags the bar so the frontend can colour it distinctly.

CREATE TABLE IF NOT EXISTS device_synthetic_hourly (
    device_id   TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    bucket      TIMESTAMPTZ NOT NULL,
    kwh         DOUBLE PRECISION NOT NULL,
    source      TEXT NOT NULL DEFAULT 'cloud'
                  CHECK (source IN ('cloud', 'gap-recovery')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (device_id, bucket, source)
);

CREATE INDEX IF NOT EXISTS device_synthetic_hourly_bucket_idx
    ON device_synthetic_hourly (bucket DESC);
