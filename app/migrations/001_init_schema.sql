CREATE TABLE devices (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    room TEXT NOT NULL,
    device_id TEXT NOT NULL,
    category_code TEXT,
    device_kind TEXT NOT NULL DEFAULT 'switch',
    is_energy_meter BOOLEAN NOT NULL DEFAULT FALSE,
    product_id TEXT,
    product_name TEXT,
    icon TEXT,
    onboarding_source TEXT NOT NULL DEFAULT 'config',
    power_dps_key TEXT,
    power_scale DOUBLE PRECISION,
    voltage_dps_keys JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_devices_device_id_unique ON devices(device_id);

CREATE TABLE device_connections (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id) ON DELETE CASCADE,
    local_key TEXT NOT NULL,
    ip_address TEXT NOT NULL,
    version DOUBLE PRECISION NOT NULL DEFAULT 3.5,
    power_dps_key TEXT,
    power_scale DOUBLE PRECISION NOT NULL DEFAULT 1,
    voltage_dps_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
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
    voltage_v DOUBLE PRECISION,
    raw_dps JSONB NOT NULL,
    source TEXT NOT NULL DEFAULT 'live',
    source_event_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_samples_device_time_source ON samples(device_id, captured_at, source);
CREATE INDEX idx_samples_device_time_desc ON samples(device_id, captured_at DESC);

CREATE TABLE device_events (
    id BIGSERIAL PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    event_at TIMESTAMPTZ NOT NULL,
    event_type TEXT,
    event_code TEXT,
    source_event_id TEXT,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_device_events_unique ON device_events(device_id, source_event_id);
CREATE INDEX idx_device_events_device_time ON device_events(device_id, event_at);

CREATE TABLE device_cloud_artifacts (
    id BIGSERIAL PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    artifact_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_device_cloud_artifacts_unique
    ON device_cloud_artifacts(device_id, artifact_type);
CREATE INDEX idx_device_cloud_artifacts_type
    ON device_cloud_artifacts(device_id, artifact_type);
