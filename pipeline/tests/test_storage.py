from __future__ import annotations

from pathlib import Path

from praha_predictor.schemas import RawSnapshot
import praha_predictor.storage as storage


def test_write_raw_snapshot_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "RAW_DIR", tmp_path / "raw")
    snapshot = RawSnapshot(
        source="bezrealitky",
        source_listing_id="123",
        observed_at="2026-03-30T09:00:00+00:00",
        listing_url="https://example.com/123",
        content_hash="abc123",
        html="<html></html>",
        payload={"advert": {"id": "123"}},
    )
    first = storage.write_raw_snapshot(snapshot)
    second = storage.write_raw_snapshot(snapshot)
    assert first == second
    assert first.exists()
