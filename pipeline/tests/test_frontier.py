from __future__ import annotations

from pathlib import Path

import praha_predictor.storage as storage


def test_frontier_upsert_and_retrieve(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "WAREHOUSE_PATH", tmp_path / "warehouse.duckdb")
    monkeypatch.setattr(storage, "INDEX_DIR", tmp_path / "index")
    monkeypatch.setattr(storage, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(storage, "NORMALIZED_DIR", tmp_path / "normalized")
    monkeypatch.setattr(storage, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(storage, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(
        storage,
        "TABLE_EXPORTS",
        {
            "source_runs": tmp_path / "index" / "source-runs.parquet",
            "listing_frontier": tmp_path / "index" / "listing-frontier.parquet",
            "listing_current_index": tmp_path / "index" / "listing-current-index.parquet",
            "source_health_reports": tmp_path / "index" / "source-health-reports.parquet",
        },
    )

    storage.upsert_frontier_urls(
        "bezrealitky",
        [
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/a",
        ],
        discovered_at="2026-03-30T09:00:00+00:00",
        discovery_method="sitemap",
    )
    urls = storage.get_frontier_candidate_urls("bezrealitky", 10)
    assert urls == ["https://example.com/a", "https://example.com/b"]
    stats = storage.get_frontier_stats("bezrealitky")
    assert stats == {"active_count": 2, "unfetched_count": 2}
