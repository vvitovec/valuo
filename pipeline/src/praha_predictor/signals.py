from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import h3
import numpy as np
import pandas as pd

from praha_predictor.config import PRAGUE_CENTER, REPO_ROOT
from praha_predictor.geospatial import haversine_km
from praha_predictor.text import strip_accents


H3_RESOLUTION = 8
COMPARABLE_SHRINKAGE = 5.0
TRANSIT_NODES_PATH = REPO_ROOT / "shared" / "transit-nodes.json"
OPTIONAL_INPUT_FIELDS = (
    "disposition",
    "land_area_m2",
    "floor_no",
    "total_floors",
    "ownership",
    "condition",
    "construction",
    "energy_label",
    "has_elevator",
    "has_parking",
    "has_cellar",
    "has_balcony_or_loggia",
)


def normalize_text_key(value: Any) -> str:
    if value is None:
        return "unknown"
    text = str(value).replace("\xa0", " ").strip().lower()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return "unknown"
    return strip_accents(text)


def normalize_ownership(value: Any) -> str:
    normalized = normalize_text_key(value)
    if normalized == "unknown":
        return normalized
    if "osob" in normalized:
        return "osobni"
    if "druz" in normalized:
        return "druzstevni"
    if normalized in {"jine", "ostatni"}:
        return "other"
    return normalized


def normalize_energy_label(value: Any) -> str:
    normalized = normalize_text_key(value)
    if normalized == "unknown":
        return normalized
    match = re.search(r"[a-g]", normalized)
    return match.group(0) if match else normalized


def normalize_boolean_flag(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "unknown"
    if isinstance(value, str):
        normalized = normalize_text_key(value)
        if normalized in {"unknown", "ano", "yes", "true", "1"}:
            return "yes" if normalized != "unknown" else "unknown"
        if normalized in {"ne", "no", "false", "0"}:
            return "no"
    return "yes" if bool(value) else "no"


def safe_float(value: Any, fallback: float | None = None) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return fallback
    return float(value)


@lru_cache(maxsize=1)
def load_transit_nodes() -> dict[str, list[dict[str, float | str]]]:
    return json.loads(Path(TRANSIT_NODES_PATH).read_text(encoding="utf-8"))


def nearest_transit_distance_km(
    lat: float | None,
    lng: float | None,
    node_group: str,
) -> float | None:
    if lat is None or lng is None:
        return None
    nodes = load_transit_nodes().get(node_group, [])
    if not nodes:
        return None
    return min(
        haversine_km(float(lat), float(lng), float(node["lat"]), float(node["lng"]))
        for node in nodes
    )


def compute_h3_cell(lat: float | None, lng: float | None, *, resolution: int = H3_RESOLUTION) -> str:
    if lat is None or lng is None:
        return "unknown"
    return str(h3.latlng_to_cell(float(lat), float(lng), resolution))


def center_ring(distance_to_center_km: float | None) -> str:
    if distance_to_center_km is None or not np.isfinite(distance_to_center_km):
        return "unknown"
    if distance_to_center_km < 3:
        return "lt_3km"
    if distance_to_center_km < 6:
        return "3_6km"
    if distance_to_center_km < 10:
        return "6_10km"
    if distance_to_center_km < 16:
        return "10_16km"
    return "16km_plus"


def count_missing_core_inputs(record: dict[str, Any]) -> int:
    missing = 0
    for field in OPTIONAL_INPUT_FIELDS:
        value = record.get(field)
        if field in {"land_area_m2", "floor_no", "total_floors"}:
            if value is None or (isinstance(value, float) and pd.isna(value)):
                missing += 1
            continue
        if value is None:
            missing += 1
            continue
        normalized = normalize_text_key(value)
        if normalized in {"", "unknown"}:
            missing += 1
    return missing


def input_quality_score(
    *,
    missing_core_feature_count: int,
    has_geocode_coordinates: bool,
    geocode_resolution: str,
) -> float:
    score = 1.0
    if not has_geocode_coordinates:
        score -= 0.2
    if geocode_resolution == "fallback_manual":
        score -= 0.15
    if missing_core_feature_count >= 6:
        score -= 0.2
    elif missing_core_feature_count >= 4:
        score -= 0.1
    return float(min(1.0, max(0.0, score)))


@dataclass(frozen=True)
class ComparableLookup:
    h3_property_ppm: dict[str, float]
    h3_property_count: dict[str, int]
    location_property_ppm: dict[str, float]
    district_property_ppm: dict[str, float]
    property_fallback_ppm: dict[str, float]
    global_fallback_ppm: float
    h3_resolution: int = H3_RESOLUTION

    def to_dict(self) -> dict[str, Any]:
        return {
            "h3Resolution": self.h3_resolution,
            "h3PropertyPpm": self.h3_property_ppm,
            "h3PropertyCount": self.h3_property_count,
            "locationPropertyPpm": self.location_property_ppm,
            "districtPropertyPpm": self.district_property_ppm,
            "propertyFallbackPpm": self.property_fallback_ppm,
            "globalFallbackPpm": self.global_fallback_ppm,
        }


def comparable_lookup_from_dict(payload: dict[str, Any]) -> ComparableLookup:
    return ComparableLookup(
        h3_property_ppm={str(key): float(value) for key, value in payload.get("h3PropertyPpm", {}).items()},
        h3_property_count={str(key): int(value) for key, value in payload.get("h3PropertyCount", {}).items()},
        location_property_ppm={
            str(key): float(value) for key, value in payload.get("locationPropertyPpm", {}).items()
        },
        district_property_ppm={
            str(key): float(value) for key, value in payload.get("districtPropertyPpm", {}).items()
        },
        property_fallback_ppm={
            str(key): float(value) for key, value in payload.get("propertyFallbackPpm", {}).items()
        },
        global_fallback_ppm=float(payload.get("globalFallbackPpm", 0.0)),
        h3_resolution=int(payload.get("h3Resolution", H3_RESOLUTION)),
    )


def build_comparable_lookup(frame: pd.DataFrame) -> ComparableLookup:
    h3_grouped = (
        frame.groupby(["h3_cell", "property_type"])["price_per_m2"]
        .agg(["median", "size"])
        .reset_index()
    )
    location_grouped = frame.groupby(["location_cluster", "property_type"])["price_per_m2"].median().to_dict()
    district_grouped = frame.groupby(["district_prague", "property_type"])["price_per_m2"].median().to_dict()
    property_grouped = frame.groupby("property_type")["price_per_m2"].median().to_dict()
    return ComparableLookup(
        h3_property_ppm={
            f"{row.h3_cell}|{row.property_type}": float(row.median)
            for row in h3_grouped.itertuples(index=False)
        },
        h3_property_count={
            f"{row.h3_cell}|{row.property_type}": int(row.size)
            for row in h3_grouped.itertuples(index=False)
        },
        location_property_ppm={
            f"{location}|{property_type}": float(value)
            for (location, property_type), value in location_grouped.items()
        },
        district_property_ppm={
            f"{district}|{property_type}": float(value)
            for (district, property_type), value in district_grouped.items()
        },
        property_fallback_ppm={str(key): float(value) for key, value in property_grouped.items()},
        global_fallback_ppm=float(frame["price_per_m2"].median()),
    )


def derive_comparable_features(record: dict[str, Any], lookup: ComparableLookup) -> dict[str, float]:
    property_type = str(record.get("property_type") or "unknown")
    district = str(record.get("district_prague") or "unknown")
    location_cluster = str(record.get("location_cluster") or district)
    h3_cell = str(record.get("h3_cell") or "unknown")
    property_fallback = lookup.property_fallback_ppm.get(property_type, lookup.global_fallback_ppm)
    district_value = lookup.district_property_ppm.get(f"{district}|{property_type}", property_fallback)
    location_value = lookup.location_property_ppm.get(
        f"{location_cluster}|{property_type}",
        district_value,
    )
    h3_key = f"{h3_cell}|{property_type}"
    h3_value = lookup.h3_property_ppm.get(h3_key)
    h3_count = int(lookup.h3_property_count.get(h3_key, 0))
    if h3_value is None:
        h3_feature = location_value
        shrunk = location_value
    else:
        h3_feature = float(h3_value)
        shrunk = float(
            ((h3_feature * h3_count) + (location_value * COMPARABLE_SHRINKAGE))
            / max(h3_count + COMPARABLE_SHRINKAGE, 1.0)
        )
    return {
        "local_ppm_h3_property": float(h3_feature),
        "local_ppm_location_cluster_property": float(location_value),
        "local_ppm_district_property": float(district_value),
        "local_ppm_shrunk": float(shrunk),
        "comparables_count_h3": float(h3_count),
    }


def apply_comparable_features(frame: pd.DataFrame, lookup: ComparableLookup) -> pd.DataFrame:
    enriched = frame.copy()
    comparable_rows = [derive_comparable_features(record, lookup) for record in enriched.to_dict(orient="records")]
    comparable_frame = pd.DataFrame(comparable_rows, index=enriched.index)
    for column in comparable_frame.columns:
        enriched[column] = comparable_frame[column]
    return enriched


def confidence_score_components(
    *,
    estimated_price_czk: float,
    interval_low: float,
    interval_high: float,
    missing_core_feature_count: int,
    geocode_resolution: str,
    comparables_count: int,
) -> tuple[float, str, list[str]]:
    score = 1.0
    flags: list[str] = []
    if geocode_resolution == "fallback_manual":
        score -= 0.35
        flags.append("geocode_fallback")
    if comparables_count < 3:
        score -= 0.25
        flags.append("too_few_comparables")
    elif comparables_count < 5:
        score -= 0.15
        flags.append("limited_comparables")
    if missing_core_feature_count >= 6:
        score -= 0.2
        flags.append("many_missing_inputs")
    elif missing_core_feature_count >= 4:
        score -= 0.1
        flags.append("some_missing_inputs")
    half_width_ratio = (
        max(interval_high - estimated_price_czk, estimated_price_czk - interval_low)
        / max(estimated_price_czk, 1.0)
    )
    if half_width_ratio > 0.30:
        score -= 0.2
        flags.append("very_wide_prediction_interval")
    elif half_width_ratio > 0.20:
        score -= 0.1
        flags.append("wide_prediction_interval")
    score = float(min(1.0, max(0.0, score)))
    label = "high" if score >= 0.75 else "medium" if score >= 0.5 else "low"
    return score, label, flags


def listing_quality_components(
    *,
    asking_price_czk: float,
    floor_area_m2: float,
    estimated_price_czk: float,
    interval_low: float,
    interval_high: float,
    local_ppm_shrunk: float,
    missing_core_feature_count: int,
    geocode_resolution: str,
    has_geocode_coordinates: bool,
    comparables_count: int,
) -> tuple[float, list[str], list[str], bool]:
    score = 1.0
    flags: list[str] = []
    filter_reasons: list[str] = []
    if geocode_resolution == "fallback_manual":
        score -= 0.35
        flags.append("geocode_fallback")
    if comparables_count < 3:
        score -= 0.25
        flags.append("too_few_comparables")
    elif comparables_count < 5:
        score -= 0.15
        flags.append("limited_comparables")
    if missing_core_feature_count >= 6:
        score -= 0.2
        flags.append("many_missing_inputs")
    elif missing_core_feature_count >= 4:
        score -= 0.1
        flags.append("some_missing_inputs")
    asking_ppm = asking_price_czk / max(floor_area_m2, 1.0)
    ppm_ratio = asking_ppm / max(local_ppm_shrunk, 1.0)
    if ppm_ratio < 0.55 or ppm_ratio > 1.8:
        score -= 0.2
        flags.append("extreme_local_ppm_gap")
    half_width_ratio = (
        max(interval_high - estimated_price_czk, estimated_price_czk - interval_low)
        / max(estimated_price_czk, 1.0)
    )
    if half_width_ratio > 0.30:
        score -= 0.15
        flags.append("very_wide_prediction_interval")
    score = float(min(1.0, max(0.0, score)))
    if not has_geocode_coordinates:
        filter_reasons.append("missing_coordinates")
    if score < 0.6:
        filter_reasons.append("low_listing_quality_score")
    for blocking_flag in ("geocode_fallback", "too_few_comparables", "extreme_local_ppm_gap"):
        if blocking_flag in flags:
            filter_reasons.append(blocking_flag)
    is_filtered_default = len(filter_reasons) > 0
    return score, flags, filter_reasons, is_filtered_default
