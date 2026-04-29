CREATE TABLE IF NOT EXISTS geocode_cache (
  cache_key TEXT PRIMARY KEY,
  lat REAL NOT NULL,
  lng REAL NOT NULL,
  district_prague TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prediction_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  request_json TEXT NOT NULL,
  response_json TEXT NOT NULL,
  model_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_registry (
  model_version TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  model_kind TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  promotion_reason TEXT,
  curated_row_count INTEGER
);

CREATE TABLE IF NOT EXISTS source_run_registry (
  run_id TEXT NOT NULL,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL,
  report_json TEXT NOT NULL,
  PRIMARY KEY (run_id, source)
);

CREATE TABLE IF NOT EXISTS geocode_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  cache_key TEXT NOT NULL,
  address TEXT NOT NULL,
  manual_district TEXT NOT NULL,
  resolved_district TEXT NOT NULL,
  lat REAL,
  lng REAL,
  status TEXT NOT NULL
);
