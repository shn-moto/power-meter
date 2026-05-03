CREATE EXTENSION IF NOT EXISTS timescaledb;

SELECT create_hypertable(
    'samples',
    'captured_at',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE,
    migrate_data => TRUE
);
