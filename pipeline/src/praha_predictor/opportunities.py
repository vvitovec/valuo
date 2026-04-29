from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from praha_predictor.features import build_model_frame
from praha_predictor.modeling import ACTIVE_MODEL_PATH, score_exported_model
from praha_predictor.quality import normalized_address_fingerprint
from praha_predictor.signals import (
    comparable_lookup_from_dict,
    confidence_score_components,
    listing_quality_components,
)


def _normalize_timestamp(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    timestamp = pd.to_datetime(value, utc=True)
    return timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _prediction_interval(
    exported_model: dict[str, Any],
    estimated_price_czk: float,
) -> tuple[int, int]:
    low = np.exp(np.log(max(estimated_price_czk, 1.0)) + float(exported_model["residualQuantiles"]["low"]))
    high = np.exp(np.log(max(estimated_price_czk, 1.0)) + float(exported_model["residualQuantiles"]["high"]))
    return int(round(low)), int(round(high))


def _market_position(asking_price_czk: float, low: float, high: float) -> str:
    if asking_price_czk < low:
        return "under_market"
    if asking_price_czk > high:
        return "over_market"
    return "within_range"


def _prepare_dashboard_frame(current_frame: pd.DataFrame) -> pd.DataFrame:
    frame = current_frame.copy()
    frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True)
    frame["fingerprint"] = (
        frame["address_text"].map(normalized_address_fingerprint)
        + "|"
        + frame["floor_area_m2"].round(1).astype(str)
        + "|"
        + frame["price_czk"].round(-4).astype(int).astype(str)
        + "|"
        + frame["property_type"].astype(str)
    )
    frame["geo_duplicate_bucket"] = (
        frame["lat"].round(3).astype(str)
        + "|"
        + frame["lng"].round(3).astype(str)
        + "|"
        + frame["floor_area_m2"].round(1).astype(str)
        + "|"
        + frame["price_czk"].round(-4).astype(int).astype(str)
        + "|"
        + frame["property_type"].astype(str)
    )
    frame = frame.sort_values("observed_at", ascending=False)
    frame["duplicate_flag"] = frame.duplicated(subset=["fingerprint"], keep="first")
    frame["near_duplicate_flag"] = frame.duplicated(subset=["geo_duplicate_bucket"], keep="first")
    return frame.loc[~frame["duplicate_flag"] & ~frame["near_duplicate_flag"]].copy()


def build_market_listing_scores(
    current_normalized_path: Path,
    frontier_index_path: Path,
    *,
    output_path: Path,
    active_model_path: Path = ACTIVE_MODEL_PATH,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if not active_model_path.exists():
        raise FileNotFoundError(f"Active model not found at {active_model_path}")
    if not current_normalized_path.exists():
        raise FileNotFoundError(f"Current normalized dataset not found at {current_normalized_path}")

    current_frame = pd.read_parquet(current_normalized_path)
    if current_frame.empty:
        rows: list[dict[str, Any]] = []
        output_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        return pd.DataFrame(), {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "rows": 0,
            "under_market": 0,
            "over_market": 0,
            "within_range": 0,
        }

    exported_model = json.loads(active_model_path.read_text(encoding="utf-8"))
    comparable_lookup = (
        comparable_lookup_from_dict(exported_model["comparableLookup"])
        if exported_model.get("comparableLookup")
        else None
    )
    dashboard_frame = _prepare_dashboard_frame(current_frame)
    model_frame = build_model_frame(dashboard_frame, comparable_lookup=comparable_lookup)

    frontier = (
        pd.read_parquet(frontier_index_path, columns=["source", "source_listing_id", "listing_url", "discovered_at"])
        if frontier_index_path.exists()
        else pd.DataFrame(columns=["source", "source_listing_id", "listing_url", "discovered_at"])
    )
    frontier = frontier.sort_values("discovered_at").drop_duplicates(
        subset=["source", "source_listing_id"],
        keep="first",
    )
    merged = dashboard_frame.merge(
        frontier,
        on=["source", "source_listing_id"],
        how="left",
        suffixes=("", "_frontier"),
    )
    if "listing_url_frontier" in merged.columns:
        merged["listing_url"] = merged["listing_url"].fillna(merged["listing_url_frontier"])

    rows: list[dict[str, Any]] = []
    model_records = model_frame.to_dict(orient="records")
    source_records = merged.to_dict(orient="records")
    for feature_payload, listing_row in zip(model_records, source_records, strict=False):
        scored = score_exported_model(exported_model, feature_payload)
        estimated_price_czk = float(scored["estimated_price_czk"])
        low, high = _prediction_interval(exported_model, estimated_price_czk)
        asking_price_czk = float(listing_row["price_czk"])
        deviation_czk = asking_price_czk - estimated_price_czk
        deviation_pct = deviation_czk / estimated_price_czk if estimated_price_czk else 0.0
        market_position = _market_position(asking_price_czk, low, high)
        confidence_score, _, confidence_flags = confidence_score_components(
            estimated_price_czk=estimated_price_czk,
            interval_low=low,
            interval_high=high,
            missing_core_feature_count=int(feature_payload.get("missing_core_feature_count", 0)),
            geocode_resolution=str(feature_payload.get("geocode_resolution") or "fallback_manual"),
            comparables_count=int(feature_payload.get("comparables_count_h3", 0)),
        )
        listing_quality_score, quality_flags, filter_reasons, is_filtered_default = listing_quality_components(
            asking_price_czk=asking_price_czk,
            floor_area_m2=float(listing_row["floor_area_m2"]),
            estimated_price_czk=estimated_price_czk,
            interval_low=low,
            interval_high=high,
            local_ppm_shrunk=float(
                feature_payload.get("local_ppm_shrunk")
                or (estimated_price_czk / max(float(listing_row["floor_area_m2"]), 1.0))
            ),
            missing_core_feature_count=int(feature_payload.get("missing_core_feature_count", 0)),
            geocode_resolution=str(feature_payload.get("geocode_resolution") or "fallback_manual"),
            has_geocode_coordinates=str(feature_payload.get("has_geocode_coordinates") or "no") == "yes",
            comparables_count=int(feature_payload.get("comparables_count_h3", 0)),
        )
        rows.append(
            {
                "source": str(listing_row["source"]),
                "source_listing_id": str(listing_row["source_listing_id"]),
                "discovered_at": _normalize_timestamp(listing_row.get("discovered_at") or listing_row.get("observed_at")),
                "observed_at": _normalize_timestamp(listing_row.get("observed_at")),
                "listing_url": str(listing_row["listing_url"]),
                "address_text": str(listing_row["address_text"]),
                "district_prague": str(listing_row["district_prague"]),
                "location_cluster": str(feature_payload["location_cluster"]),
                "property_type": str(listing_row["property_type"]),
                "asking_price_czk": int(round(asking_price_czk)),
                "predicted_price_czk": int(round(estimated_price_czk)),
                "typical_range_low_czk": low,
                "typical_range_high_czk": high,
                "deviation_czk": int(round(deviation_czk)),
                "deviation_pct": round(float(deviation_pct), 6),
                "market_position": market_position,
                "opportunity_score": round(abs(float(deviation_pct)), 6),
                "listing_quality_score": round(float(listing_quality_score), 6),
                "quality_flags": sorted(set(quality_flags)),
                "comparables_count": int(feature_payload.get("comparables_count_h3", 0)),
                "confidence_score": round(float(confidence_score), 6),
                "is_filtered_default": bool(is_filtered_default),
                "filter_reasons": sorted(set(filter_reasons)),
                "warning_flags": sorted(set(confidence_flags)),
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )

    rows.sort(key=lambda row: (row["opportunity_score"], abs(row["deviation_czk"])), reverse=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    frame = pd.DataFrame(rows)
    summary = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rows": int(len(frame)),
        "under_market": int((frame["market_position"] == "under_market").sum()) if not frame.empty else 0,
        "over_market": int((frame["market_position"] == "over_market").sum()) if not frame.empty else 0,
        "within_range": int((frame["market_position"] == "within_range").sum()) if not frame.empty else 0,
    }
    return frame, summary
