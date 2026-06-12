-- User-toggleable per-device flag: only devices that opt-in are eligible
-- as automation targets (prevents picking the gateway from the bind drop-down
-- when defining a charger schedule, etc.)
ALTER TABLE devices ADD COLUMN IF NOT EXISTS allow_custom_automation BOOLEAN NOT NULL DEFAULT FALSE;

-- One row per registered automation. The Python code is the source of
-- truth for what exists (slug + device_type), while the row stores user
-- choices (bound device, schedule override, enabled) and the latest run
-- outcome.
CREATE TABLE IF NOT EXISTS automations (
    id BIGSERIAL PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    device_type TEXT NOT NULL,
    bound_device_id TEXT REFERENCES devices(device_id) ON DELETE SET NULL,
    cron_schedule TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_run_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ,
    last_run_status TEXT,
    last_run_log TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_automations_bound_device ON automations(bound_device_id);
CREATE INDEX IF NOT EXISTS idx_automations_enabled_next ON automations(enabled, next_run_at) WHERE enabled = TRUE;
