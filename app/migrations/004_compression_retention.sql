ALTER TABLE samples SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'device_id',
    timescaledb.compress_orderby   = 'captured_at DESC'
);

SELECT add_compression_policy('samples', INTERVAL '14 days');

SELECT add_retention_policy('samples', INTERVAL '1 year');
