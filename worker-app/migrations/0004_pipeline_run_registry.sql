CREATE TABLE IF NOT EXISTS pipeline_run_registry (
  run_id TEXT NOT NULL,
  run_type TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  status TEXT NOT NULL,
  model_version_before TEXT,
  model_version_after TEXT,
  summary_json TEXT,
  error_json TEXT,
  PRIMARY KEY (run_id, run_type)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_run_registry_type_finished
  ON pipeline_run_registry (run_type, finished_at DESC);
