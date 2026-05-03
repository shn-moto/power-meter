ALTER MATERIALIZED VIEW samples_hourly  SET (timescaledb.materialized_only = false);

ALTER MATERIALIZED VIEW samples_daily   SET (timescaledb.materialized_only = false);

ALTER MATERIALIZED VIEW samples_monthly SET (timescaledb.materialized_only = false);
