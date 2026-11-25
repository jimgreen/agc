-- 秦岭 AGC 实时算法后台与 Web HMI 持久化表结构
-- 由 agc_schema.apply_schema 统一提交并同步 RTDB 原生结构目录

CREATE TABLE IF NOT EXISTS agc_control_settings (
    setting_key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agc_strategy_snapshots (
    cycle INTEGER PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    sample_timestamp TEXT NOT NULL DEFAULT '',
    loop_mode TEXT NOT NULL DEFAULT 'open',
    auto_enabled INTEGER NOT NULL DEFAULT 0,
    data_quality TEXT NOT NULL DEFAULT 'ok',
    dispatch_allowed INTEGER NOT NULL DEFAULT 0,
    command_count INTEGER NOT NULL DEFAULT 0,
    changed_count INTEGER NOT NULL DEFAULT 0,
    dispatched_count INTEGER NOT NULL DEFAULT 0,
    load_kw REAL NOT NULL DEFAULT 0,
    renewable_current_kw REAL NOT NULL DEFAULT 0,
    renewable_target_kw REAL NOT NULL DEFAULT 0,
    renewable_max_available_kw REAL NOT NULL DEFAULT 0,
    storage_current_kw REAL NOT NULL DEFAULT 0,
    storage_target_kw REAL NOT NULL DEFAULT 0,
    storage_soc REAL,
    diesel_current_kw REAL NOT NULL DEFAULT 0,
    diesel_target_kw REAL NOT NULL DEFAULT 0,
    acdc_current_kw REAL NOT NULL DEFAULT 0,
    acdc_target_kw REAL NOT NULL DEFAULT 0,
    electrolyzer_current_kw REAL NOT NULL DEFAULT 0,
    electrolyzer_target_kw REAL NOT NULL DEFAULT 0,
    fuel_cell_current_kw REAL NOT NULL DEFAULT 0,
    fuel_cell_target_kw REAL NOT NULL DEFAULT 0,
    hydrogen_soc REAL,
    hydrogen_pressure_mpa REAL,
    wind_speed REAL,
    solar_irradiance REAL,
    summary_text TEXT NOT NULL DEFAULT '',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    decision_detail_json TEXT NOT NULL DEFAULT '[]',
    warnings_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_agc_snapshots_recorded_at
    ON agc_strategy_snapshots(recorded_at);

CREATE TABLE IF NOT EXISTS agc_strategy_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    category TEXT NOT NULL,
    dev_type TEXT NOT NULL,
    dev_name TEXT NOT NULL,
    connection_side TEXT NOT NULL DEFAULT '',
    control_mode TEXT NOT NULL DEFAULT '',
    online INTEGER NOT NULL DEFAULT 1,
    commandable INTEGER NOT NULL DEFAULT 0,
    set_type TEXT NOT NULL DEFAULT '',
    current_kw REAL,
    available_kw REAL,
    target_kw REAL,
    delta_kw REAL,
    soc REAL,
    changed INTEGER NOT NULL DEFAULT 0,
    status_label TEXT NOT NULL DEFAULT '',
    dispatch_status TEXT NOT NULL DEFAULT 'calculated',
    dispatch_message TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_agc_strategy_commands_cycle
    ON agc_strategy_commands(cycle);
CREATE INDEX IF NOT EXISTS idx_agc_strategy_commands_dev
    ON agc_strategy_commands(dev_type, dev_name, cycle DESC);

CREATE TABLE IF NOT EXISTS agc_trend_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    epoch_ms INTEGER NOT NULL,
    time_label TEXT NOT NULL,
    load_kw REAL,
    ac_load_kw REAL,
    dc_load_kw REAL,
    renewable_current_kw REAL,
    renewable_target_kw REAL,
    renewable_max_available_kw REAL,
    ac_renewable_current_kw REAL,
    ac_renewable_target_kw REAL,
    dc_renewable_current_kw REAL,
    dc_renewable_target_kw REAL,
    wind_current_kw REAL,
    wind_target_kw REAL,
    pv_current_kw REAL,
    pv_target_kw REAL,
    storage_current_kw REAL,
    storage_target_kw REAL,
    ac_storage_current_kw REAL,
    ac_storage_target_kw REAL,
    dc_storage_current_kw REAL,
    dc_storage_target_kw REAL,
    storage_soc REAL,
    ac_storage_soc REAL,
    dc_storage_soc REAL,
    diesel_current_kw REAL,
    diesel_target_kw REAL,
    diesel_min_kw REAL,
    acdc_current_kw REAL,
    acdc_target_kw REAL,
    electrolyzer_current_kw REAL,
    electrolyzer_target_kw REAL,
    fuel_cell_current_kw REAL,
    fuel_cell_target_kw REAL,
    hydrogen_soc REAL,
    hydrogen_pressure_mpa REAL,
    wind_speed REAL,
    solar_irradiance REAL
);

CREATE INDEX IF NOT EXISTS idx_agc_trend_epoch
    ON agc_trend_points(epoch_ms);
CREATE INDEX IF NOT EXISTS idx_agc_trend_cycle
    ON agc_trend_points(cycle);

CREATE TABLE IF NOT EXISTS agc_control_logs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    event_type TEXT NOT NULL DEFAULT 'cycle',
    cycle INTEGER NOT NULL DEFAULT 0,
    subject TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_agc_control_logs_time
    ON agc_control_logs(logged_at);
CREATE INDEX IF NOT EXISTS idx_agc_control_logs_level
    ON agc_control_logs(level);

CREATE TABLE IF NOT EXISTS agc_command_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issued_at TEXT NOT NULL,
    epoch_ms INTEGER NOT NULL,
    cycle INTEGER NOT NULL DEFAULT 0,
    origin TEXT NOT NULL DEFAULT 'automatic',
    kind TEXT NOT NULL DEFAULT 'yt',
    dev_type TEXT NOT NULL,
    dev_name TEXT NOT NULL,
    set_type TEXT NOT NULL,
    measured_value REAL,
    command_value REAL,
    rtu_id INTEGER,
    pnt_no INTEGER,
    status TEXT NOT NULL DEFAULT 'calculated',
    message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_agc_cmd_records_time
    ON agc_command_records(epoch_ms);
CREATE INDEX IF NOT EXISTS idx_agc_cmd_records_dev
    ON agc_command_records(dev_type, dev_name, set_type, epoch_ms);

CREATE TABLE IF NOT EXISTS agc_runtime_logs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    log_type TEXT NOT NULL DEFAULT 'system',
    target TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_agc_runtime_logs_time
    ON agc_runtime_logs(logged_at);
CREATE INDEX IF NOT EXISTS idx_agc_runtime_logs_type
    ON agc_runtime_logs(log_type);

CREATE TABLE IF NOT EXISTS agc_device_configs (
    dev_type TEXT NOT NULL,
    dev_name TEXT NOT NULL,
    set_type TEXT NOT NULL DEFAULT 'p_set',
    control_state TEXT NOT NULL DEFAULT 'closed',
    p_min REAL,
    p_max REAL,
    control_step REAL,
    control_deadband REAL NOT NULL DEFAULT 0,
    curve_interp TEXT NOT NULL DEFAULT 'step',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (dev_type, dev_name, set_type)
);

CREATE TABLE IF NOT EXISTS agc_device_curves (
    dev_type TEXT NOT NULL,
    dev_name TEXT NOT NULL,
    set_type TEXT NOT NULL DEFAULT 'p_set',
    point_index INTEGER NOT NULL CHECK (point_index BETWEEN 0 AND 95),
    minute_of_day INTEGER NOT NULL CHECK (minute_of_day BETWEEN 0 AND 1439),
    target_value REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (dev_type, dev_name, set_type, point_index)
);

CREATE INDEX IF NOT EXISTS idx_agc_device_curves_dev
    ON agc_device_curves(dev_type, dev_name, set_type, point_index);
