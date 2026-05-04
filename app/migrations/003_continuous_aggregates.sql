DROP MATERIALIZED VIEW IF EXISTS samples_monthly CASCADE;

DROP MATERIALIZED VIEW IF EXISTS samples_daily CASCADE;

DROP MATERIALIZED VIEW IF EXISTS samples_hourly CASCADE;

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
