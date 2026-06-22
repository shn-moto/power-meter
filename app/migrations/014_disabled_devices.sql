-- A device with disabled=true must vanish from every surface: dashboard
-- cards, monthly aggregates, the poll loop, the ingest endpoint. The
-- samples it already produced stay in the DB (no point throwing away
-- history), but no place that lists devices should ever pick this row up.
ALTER TABLE devices
    ADD COLUMN IF NOT EXISTS disabled BOOLEAN NOT NULL DEFAULT FALSE;
