-- AGC Web HMI persistent data model.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS system_settings (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    value_type TEXT NOT NULL DEFAULT 'text',
    description TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    kind TEXT NOT NULL,
    device_id TEXT NOT NULL,
    name TEXT NOT NULL,
    control_state TEXT NOT NULL DEFAULT 'remote',
    run_status INTEGER NOT NULL DEFAULT 0,
    p_min REAL NOT NULL DEFAULT 0,
    p_max REAL NOT NULL DEFAULT 0,
    control_step REAL NOT NULL DEFAULT 0,
    control_deadband REAL NOT NULL DEFAULT 0,
    preset_power REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, device_id)
);

CREATE TABLE IF NOT EXISTS device_curves (
    kind TEXT NOT NULL,
    device_id TEXT NOT NULL,
    point_index INTEGER NOT NULL CHECK (point_index BETWEEN 0 AND 95),
    minute_of_day INTEGER NOT NULL CHECK (minute_of_day BETWEEN 0 AND 1439),
    power REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, device_id, point_index),
    FOREIGN KEY (kind, device_id) REFERENCES devices(kind, device_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS control_cycles (
    cycle INTEGER PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    control_mode TEXT NOT NULL,
    running INTEGER NOT NULL DEFAULT 0,
    load_power REAL NOT NULL DEFAULT 0,
    renewable_power REAL NOT NULL DEFAULT 0,
    storage_power REAL NOT NULL DEFAULT 0,
    diesel_power REAL NOT NULL DEFAULT 0,
    balance_power REAL NOT NULL DEFAULT 0,
    wind_speed REAL NOT NULL DEFAULT 0,
    solar_irradiance REAL NOT NULL DEFAULT 0,
    env_temperature REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS device_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    device_id TEXT NOT NULL,
    name TEXT NOT NULL,
    control_state TEXT NOT NULL,
    run_status INTEGER NOT NULL DEFAULT 0,
    realtime_power REAL NOT NULL DEFAULT 0,
    command_power REAL NOT NULL DEFAULT 0,
    preset_power REAL NOT NULL DEFAULT 0,
    control_step REAL NOT NULL DEFAULT 0,
    control_deadband REAL NOT NULL DEFAULT 0,
    UNIQUE (cycle, kind, device_id)
);

CREATE INDEX IF NOT EXISTS idx_control_cycles_recorded_at ON control_cycles(recorded_at);
CREATE INDEX IF NOT EXISTS idx_control_cycles_time_cycle ON control_cycles(julianday(recorded_at), cycle DESC);
CREATE INDEX IF NOT EXISTS idx_device_samples_lookup ON device_samples(kind, device_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_device_samples_cycle ON device_samples(cycle);
CREATE INDEX IF NOT EXISTS idx_device_samples_device_cycle ON device_samples(kind, device_id, cycle DESC);

CREATE TABLE IF NOT EXISTS event_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_logs_logged_at ON event_logs(logged_at);
CREATE INDEX IF NOT EXISTS idx_event_logs_level ON event_logs(level);
