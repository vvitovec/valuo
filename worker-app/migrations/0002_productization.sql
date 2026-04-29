CREATE TABLE IF NOT EXISTS user_profile (
  user_id TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prediction_usage_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  experience_mode TEXT NOT NULL,
  event_kind TEXT NOT NULL,
  request_json TEXT,
  response_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_prediction_usage_user_created
  ON prediction_usage_event (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS subscription_entitlement (
  user_id TEXT PRIMARY KEY,
  stripe_customer_id TEXT,
  stripe_subscription_id TEXT,
  plan_code TEXT,
  status TEXT NOT NULL,
  current_period_end TEXT,
  cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS billing_customer_map (
  user_id TEXT PRIMARY KEY,
  stripe_customer_id TEXT NOT NULL UNIQUE,
  email TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_listing_score (
  source TEXT NOT NULL,
  source_listing_id TEXT NOT NULL,
  discovered_at TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  listing_url TEXT NOT NULL,
  address_text TEXT NOT NULL,
  district_prague TEXT NOT NULL,
  location_cluster TEXT,
  property_type TEXT NOT NULL,
  asking_price_czk REAL NOT NULL,
  predicted_price_czk REAL NOT NULL,
  typical_range_low_czk REAL NOT NULL,
  typical_range_high_czk REAL NOT NULL,
  deviation_czk REAL NOT NULL,
  deviation_pct REAL NOT NULL,
  market_position TEXT NOT NULL,
  opportunity_score REAL NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (source, source_listing_id)
);

CREATE INDEX IF NOT EXISTS idx_market_listing_score_window
  ON market_listing_score (discovered_at DESC, market_position, district_prague, property_type, source);
