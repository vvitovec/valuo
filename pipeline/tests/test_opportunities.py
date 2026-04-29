from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from praha_predictor.config import ARTIFACTS_DIR
from praha_predictor.opportunities import build_market_listing_scores


def test_build_market_listing_scores_exports_dashboard_ready_rows(tmp_path: Path) -> None:
    current_path = tmp_path / "current-normalized.parquet"
    frontier_path = tmp_path / "listing-frontier.parquet"
    output_path = tmp_path / "market-opportunities.json"
    active_model_path = ARTIFACTS_DIR / "active-model.json"

    pd.DataFrame(
        [
            {
                "source": "realitymix",
                "source_listing_id": "listing-1",
                "observed_at": "2026-03-30T12:00:00+00:00",
                "listing_url": "https://example.com/listing-1",
                "content_hash": "hash-1",
                "address_text": "Praha 4, Háje",
                "district_prague": "Praha 4",
                "lat": 50.0288,
                "lng": 14.5280,
                "property_type": "flat",
                "offer_type": "sale",
                "disposition": "2+kk",
                "floor_area_m2": 43.0,
                "land_area_m2": None,
                "floor_no": 2.0,
                "total_floors": 12.0,
                "ownership": "osobní",
                "condition": "before_reconstruction",
                "construction": "panel",
                "energy_label": "b",
                "has_elevator": True,
                "has_parking": False,
                "has_cellar": True,
                "has_balcony_or_loggia": False,
                "price_czk": 6900000.0,
                "price_per_m2": 160465.11,
                "quality_flags": [],
                "reject_reason": None,
            },
            {
                "source": "remax",
                "source_listing_id": "listing-2",
                "observed_at": "2026-03-30T12:00:00+00:00",
                "listing_url": "https://example.com/listing-2",
                "content_hash": "hash-2",
                "address_text": "Praha 6, Dejvice",
                "district_prague": "Praha 6",
                "lat": 50.1026,
                "lng": 14.3914,
                "property_type": "flat",
                "offer_type": "sale",
                "disposition": "3+kk",
                "floor_area_m2": 91.0,
                "land_area_m2": None,
                "floor_no": 3.0,
                "total_floors": 6.0,
                "ownership": "osobní",
                "condition": "very_good",
                "construction": "brick",
                "energy_label": "c",
                "has_elevator": True,
                "has_parking": True,
                "has_cellar": False,
                "has_balcony_or_loggia": True,
                "price_czk": 19600000.0,
                "price_per_m2": 215384.61,
                "quality_flags": [],
                "reject_reason": None,
            },
        ]
    ).to_parquet(current_path, index=False)

    pd.DataFrame(
        [
            {
                "source": "realitymix",
                "source_listing_id": "listing-1",
                "listing_url": "https://example.com/listing-1",
                "discovered_at": "2026-03-30T11:00:00+00:00",
            },
            {
                "source": "remax",
                "source_listing_id": "listing-2",
                "listing_url": "https://example.com/listing-2",
                "discovered_at": "2026-03-30T11:30:00+00:00",
            },
        ]
    ).to_parquet(frontier_path, index=False)

    frame, summary = build_market_listing_scores(
        current_path,
        frontier_path,
        output_path=output_path,
        active_model_path=active_model_path,
    )

    exported_rows = json.loads(output_path.read_text(encoding="utf-8"))
    assert len(frame) == 2
    assert len(exported_rows) == 2
    assert summary["rows"] == 2
    assert {row["market_position"] for row in exported_rows}.issubset(
        {"under_market", "within_range", "over_market"}
    )
    assert all("discovered_at" in row for row in exported_rows)
    assert all("listing_quality_score" in row for row in exported_rows)
    assert all("confidence_score" in row for row in exported_rows)
    assert all("is_filtered_default" in row for row in exported_rows)
