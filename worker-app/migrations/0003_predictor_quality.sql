ALTER TABLE market_listing_score ADD COLUMN listing_quality_score REAL;
ALTER TABLE market_listing_score ADD COLUMN quality_flags TEXT;
ALTER TABLE market_listing_score ADD COLUMN comparables_count INTEGER;
ALTER TABLE market_listing_score ADD COLUMN confidence_score REAL;
ALTER TABLE market_listing_score ADD COLUMN is_filtered_default INTEGER NOT NULL DEFAULT 0;
ALTER TABLE market_listing_score ADD COLUMN filter_reasons TEXT;
ALTER TABLE market_listing_score ADD COLUMN warning_flags TEXT;
