CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE devices (
    name TEXT NOT NULL,
    room TEXT NOT NULL,
    device_id TEXT PRIMARY KEY,
    category_code TEXT,
    device_kind TEXT NOT NULL DEFAULT 'switch',
    is_energy_meter BOOLEAN NOT NULL DEFAULT FALSE,
    product_id TEXT,
    product_name TEXT,
    icon TEXT,
    onboarding_source TEXT NOT NULL DEFAULT 'config',
    total_power_dps_key TEXT,
    total_power_scale DOUBLE PRECISION NOT NULL DEFAULT 1,
    power_type TEXT NOT NULL DEFAULT 'total',
    visualized_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE device_connections (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id) ON DELETE CASCADE,
    local_key TEXT NOT NULL,
    ip_address TEXT NOT NULL,
    version DOUBLE PRECISION NOT NULL DEFAULT 3.5,
    total_power_dps_key TEXT,
    total_power_scale DOUBLE PRECISION NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_device_connections_ip ON device_connections(ip_address);

CREATE TABLE device_capabilities (
    id BIGSERIAL PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    capability_source TEXT NOT NULL,
    capability_code TEXT NOT NULL,
    capability_name TEXT,
    value_type TEXT,
    dp_id INTEGER,
    values_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_device_capabilities_unique
    ON device_capabilities(device_id, capability_source, capability_code);
CREATE INDEX idx_device_capabilities_device ON device_capabilities(device_id);

CREATE TABLE samples (
    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    captured_at TIMESTAMPTZ NOT NULL,
    power_w DOUBLE PRECISION NOT NULL,
    raw_dps JSONB NOT NULL,
    source TEXT NOT NULL DEFAULT 'live',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_samples_device_time_source ON samples(device_id, captured_at, source);
CREATE INDEX idx_samples_device_time_desc ON samples(device_id, captured_at DESC);

CREATE TABLE device_profiles (
    device_id TEXT PRIMARY KEY,
    profile_version INTEGER NOT NULL,
    source_path TEXT NOT NULL,
    payload JSONB NOT NULL,
    content_hash TEXT NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_device_profiles_source_path
    ON device_profiles(source_path);

CREATE INDEX idx_device_profiles_content_hash
    ON device_profiles(content_hash);

SELECT create_hypertable(
    'samples',
    'captured_at',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE,
    migrate_data => TRUE
);

CREATE MATERIALIZED VIEW samples_hourly
WITH (timescaledb.continuous) AS
SELECT
    device_id,
    time_bucket(INTERVAL '1 hour', captured_at) AS bucket,
    avg(power_w)                                AS avg_power_w,
    max(power_w)                                AS peak_power_w,
    min(power_w)                                AS min_power_w,
    count(*)                                    AS sample_count,
    avg(power_w)                                AS energy_wh,
    first(power_w, captured_at)                 AS first_power_w,
    last(power_w, captured_at)                  AS last_power_w,
    first(raw_dps, captured_at)                 AS first_raw_dps,
    last(raw_dps, captured_at)                  AS last_raw_dps,
    min(captured_at)                            AS first_captured_at,
    max(captured_at)                            AS last_captured_at
FROM samples
GROUP BY device_id, bucket
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'samples_hourly',
    start_offset      => INTERVAL '3 hours',
    end_offset        => INTERVAL '5 minutes',
    schedule_interval => INTERVAL '1 minute'
);

CREATE MATERIALIZED VIEW samples_daily
WITH (timescaledb.continuous) AS
SELECT
    device_id,
    time_bucket(INTERVAL '1 day', bucket)       AS bucket,
    avg(avg_power_w)                            AS avg_power_w,
    max(peak_power_w)                           AS peak_power_w,
    min(min_power_w)                            AS min_power_w,
    sum(sample_count)                           AS sample_count,
    sum(energy_wh)                              AS energy_wh,
    first(first_power_w, bucket)                AS first_power_w,
    last(last_power_w, bucket)                  AS last_power_w,
    first(first_raw_dps, bucket)                AS first_raw_dps,
    last(last_raw_dps, bucket)                  AS last_raw_dps,
    min(first_captured_at)                      AS first_captured_at,
    max(last_captured_at)                       AS last_captured_at
FROM samples_hourly
GROUP BY device_id, time_bucket(INTERVAL '1 day', bucket)
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'samples_daily',
    start_offset      => INTERVAL '3 days',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '10 minutes'
);

CREATE MATERIALIZED VIEW samples_monthly
WITH (timescaledb.continuous) AS
SELECT
    device_id,
    time_bucket(INTERVAL '1 month', bucket)     AS bucket,
    avg(avg_power_w)                            AS avg_power_w,
    max(peak_power_w)                           AS peak_power_w,
    min(min_power_w)                            AS min_power_w,
    sum(sample_count)                           AS sample_count,
    sum(energy_wh)                              AS energy_wh,
    first(first_power_w, bucket)                AS first_power_w,
    last(last_power_w, bucket)                  AS last_power_w,
    first(first_raw_dps, bucket)                AS first_raw_dps,
    last(last_raw_dps, bucket)                  AS last_raw_dps,
    min(first_captured_at)                      AS first_captured_at,
    max(last_captured_at)                       AS last_captured_at
FROM samples_daily
GROUP BY device_id, time_bucket(INTERVAL '1 month', bucket)
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'samples_monthly',
    start_offset      => INTERVAL '4 months',
    end_offset        => INTERVAL '1 month',
    schedule_interval => INTERVAL '1 hour'
);

ALTER MATERIALIZED VIEW samples_hourly  SET (timescaledb.materialized_only = false);

ALTER MATERIALIZED VIEW samples_daily   SET (timescaledb.materialized_only = false);

ALTER MATERIALIZED VIEW samples_monthly SET (timescaledb.materialized_only = false);

ALTER TABLE samples SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'device_id',
    timescaledb.compress_orderby   = 'captured_at DESC'
);

SELECT add_compression_policy('samples', INTERVAL '14 days');

SELECT add_retention_policy('samples', INTERVAL '1 year');
