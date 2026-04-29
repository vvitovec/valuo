from __future__ import annotations

import re
from typing import Any

import pandas as pd

from praha_predictor.config import PRAGUE_CENTER
from praha_predictor.geospatial import haversine_km
from praha_predictor.market_scope import derive_location_cluster, infer_market_segment
from praha_predictor.signals import (
    ComparableLookup,
    apply_comparable_features,
    center_ring,
    compute_h3_cell,
    count_missing_core_inputs,
    input_quality_score,
    nearest_transit_distance_km,
    normalize_boolean_flag,
    normalize_energy_label,
    normalize_ownership,
    safe_float,
)


CORE_FEATURE_COLUMNS = [
    "district_prague",
    "property_type",
    "disposition",
    "floor_area_m2",
    "land_area_m2",
    "land_area_missing",
    "floor_no",
    "floor_no_missing",
    "total_floors",
    "total_floors_missing",
    "ownership",
    "condition",
    "construction",
    "energy_label",
    "has_elevator",
    "has_parking",
    "has_cellar",
    "has_balcony_or_loggia",
    "distance_to_center_km",
    "distance_to_metro_km",
    "distance_to_rail_km",
    "center_ring",
    "missing_core_feature_count",
    "listing_input_quality_score",
]

EXTENDED_FEATURE_COLUMNS = [
    *CORE_FEATURE_COLUMNS,
    "market_segment",
    "location_cluster",
    "h3_cell",
    "room_count",
    "area_per_room_m2",
    "floor_position_ratio",
    "local_ppm_h3_property",
    "local_ppm_location_cluster_property",
    "local_ppm_district_property",
    "local_ppm_shrunk",
    "comparables_count_h3",
]

SEGMENTED_FEATURE_COLUMNS = [
    "location_cluster",
    "h3_cell",
    "center_ring",
    "disposition",
    "floor_area_m2",
    "land_area_m2",
    "land_area_missing",
    "floor_no",
    "floor_no_missing",
    "total_floors",
    "total_floors_missing",
    "ownership",
    "condition",
    "construction",
    "energy_label",
    "has_elevator",
    "has_parking",
    "has_cellar",
    "has_balcony_or_loggia",
    "distance_to_center_km",
    "distance_to_metro_km",
    "distance_to_rail_km",
    "room_count",
    "area_per_room_m2",
    "floor_position_ratio",
    "missing_core_feature_count",
    "listing_input_quality_score",
    "local_ppm_h3_property",
    "local_ppm_location_cluster_property",
    "local_ppm_district_property",
    "local_ppm_shrunk",
    "comparables_count_h3",
]

FEATURE_TYPES = {
    "district_prague": "nominal",
    "property_type": "nominal",
    "disposition": "nominal",
    "floor_area_m2": "continuous",
    "land_area_m2": "continuous",
    "land_area_missing": "nominal",
    "floor_no": "continuous",
    "floor_no_missing": "nominal",
    "total_floors": "continuous",
    "total_floors_missing": "nominal",
    "ownership": "nominal",
    "condition": "nominal",
    "construction": "nominal",
    "energy_label": "nominal",
    "has_elevator": "nominal",
    "has_parking": "nominal",
    "has_cellar": "nominal",
    "has_balcony_or_loggia": "nominal",
    "distance_to_center_km": "continuous",
    "distance_to_metro_km": "continuous",
    "distance_to_rail_km": "continuous",
    "center_ring": "nominal",
    "market_segment": "nominal",
    "location_cluster": "nominal",
    "h3_cell": "nominal",
    "room_count": "continuous",
    "area_per_room_m2": "continuous",
    "floor_position_ratio": "continuous",
    "has_geocode_coordinates": "nominal",
    "geocode_resolution": "nominal",
    "missing_core_feature_count": "continuous",
    "listing_input_quality_score": "continuous",
    "local_ppm_h3_property": "continuous",
    "local_ppm_location_cluster_property": "continuous",
    "local_ppm_district_property": "continuous",
    "local_ppm_shrunk": "continuous",
    "comparables_count_h3": "continuous",
}

FEATURE_COLUMNS = EXTENDED_FEATURE_COLUMNS


def parse_room_count(disposition: str | None) -> float:
    if disposition is None:
        return 0.0
    text = str(disposition).strip().lower()
    match = re.match(r"(\d+)\+", text)
    if match:
        return float(match.group(1))
    if text in {"atypicky", "atypicke", "unknown"}:
        return 0.0
    return 0.0


def _normalized_string(value: Any, *, normalizer: callable | None = None) -> str:
    if normalizer is not None:
        normalized = normalizer(value)
    elif value is None or (isinstance(value, float) and pd.isna(value)):
        normalized = "unknown"
    else:
        normalized = str(value).strip() or "unknown"
    return str(normalized or "unknown")


def _base_record_from_series(row: pd.Series) -> dict[str, Any]:
    lat = safe_float(row.get("lat"))
    lng = safe_float(row.get("lng"))
    has_coordinates = lat is not None and lng is not None
    distance_to_center_km = (
        haversine_km(float(lat), float(lng), PRAGUE_CENTER[0], PRAGUE_CENTER[1])
        if has_coordinates
        else None
    )
    location_cluster = (
        derive_location_cluster(
            row.get("address_text"),
            row.get("district_prague"),
            district_prague=row.get("district_prague"),
            lat=lat,
            lng=lng,
        )
        or str(row.get("district_prague") or "Praha okoli")
    )
    market_segment = infer_market_segment(location_cluster, row.get("district_prague"))
    room_count = parse_room_count(row.get("disposition"))
    floor_area_m2 = float(row["floor_area_m2"])
    floor_no = safe_float(row.get("floor_no"), -1.0)
    total_floors = safe_float(row.get("total_floors"), -1.0)
    raw_record = {
        "disposition": row.get("disposition"),
        "land_area_m2": row.get("land_area_m2"),
        "floor_no": row.get("floor_no"),
        "total_floors": row.get("total_floors"),
        "ownership": row.get("ownership"),
        "condition": row.get("condition"),
        "construction": row.get("construction"),
        "energy_label": row.get("energy_label"),
        "has_elevator": row.get("has_elevator"),
        "has_parking": row.get("has_parking"),
        "has_cellar": row.get("has_cellar"),
        "has_balcony_or_loggia": row.get("has_balcony_or_loggia"),
    }
    missing_count = count_missing_core_inputs(raw_record)
    geocode_resolution = "exact" if has_coordinates else "fallback_manual"
    quality_score = input_quality_score(
        missing_core_feature_count=missing_count,
        has_geocode_coordinates=has_coordinates,
        geocode_resolution=geocode_resolution,
    )
    return {
        "district_prague": _normalized_string(row.get("district_prague")),
        "property_type": _normalized_string(row.get("property_type")),
        "disposition": _normalized_string(row.get("disposition")),
        "floor_area_m2": floor_area_m2,
        "land_area_m2": safe_float(row.get("land_area_m2"), 0.0),
        "land_area_missing": "yes"
        if row.get("land_area_m2") is None or pd.isna(row.get("land_area_m2"))
        else "no",
        "floor_no": float(floor_no if floor_no is not None else -1.0),
        "floor_no_missing": "yes"
        if row.get("floor_no") is None or pd.isna(row.get("floor_no"))
        else "no",
        "total_floors": float(total_floors if total_floors is not None else -1.0),
        "total_floors_missing": "yes"
        if row.get("total_floors") is None or pd.isna(row.get("total_floors"))
        else "no",
        "ownership": _normalized_string(row.get("ownership"), normalizer=normalize_ownership),
        "condition": _normalized_string(row.get("condition")),
        "construction": _normalized_string(row.get("construction")),
        "energy_label": _normalized_string(row.get("energy_label"), normalizer=normalize_energy_label),
        "has_elevator": normalize_boolean_flag(row.get("has_elevator")),
        "has_parking": normalize_boolean_flag(row.get("has_parking")),
        "has_cellar": normalize_boolean_flag(row.get("has_cellar")),
        "has_balcony_or_loggia": normalize_boolean_flag(row.get("has_balcony_or_loggia")),
        "distance_to_center_km": safe_float(distance_to_center_km),
        "distance_to_metro_km": safe_float(nearest_transit_distance_km(lat, lng, "metroNodes")),
        "distance_to_rail_km": safe_float(nearest_transit_distance_km(lat, lng, "railNodes")),
        "center_ring": center_ring(distance_to_center_km),
        "market_segment": str(market_segment),
        "location_cluster": str(location_cluster),
        "h3_cell": compute_h3_cell(lat, lng),
        "room_count": room_count,
        "area_per_room_m2": float(floor_area_m2 / room_count) if room_count > 0 else floor_area_m2,
        "floor_position_ratio": float(floor_no / total_floors)
        if floor_no is not None and total_floors is not None and floor_no >= 0 and total_floors > 0
        else -1.0,
        "has_geocode_coordinates": "yes" if has_coordinates else "no",
        "geocode_resolution": geocode_resolution,
        "missing_core_feature_count": float(missing_count),
        "listing_input_quality_score": float(quality_score),
        "model_segment": f"{market_segment}_{row.get('property_type')}",
    }


def build_model_frame(
    curated_frame: pd.DataFrame,
    *,
    comparable_lookup: ComparableLookup | None = None,
) -> pd.DataFrame:
    frame = curated_frame.copy()
    base_records = [_base_record_from_series(row) for _, row in frame.iterrows()]
    model_frame = pd.DataFrame(base_records, index=frame.index)
    for column in ("price_czk", "price_per_m2", "source", "source_listing_id", "observed_at"):
        if column in frame.columns:
            model_frame[column] = frame[column].values
    if comparable_lookup is not None:
        model_frame = apply_comparable_features(model_frame, comparable_lookup)
    return model_frame


def build_baseline_lookup(
    curated_frame: pd.DataFrame,
    *,
    location_feature: str = "location_cluster",
) -> dict[str, float]:
    grouped = (
        curated_frame.groupby([location_feature, "property_type"])["price_per_m2"]
        .median()
        .to_dict()
    )
    baseline = {
        f"{location}|{property_type}": float(value)
        for (location, property_type), value in grouped.items()
    }
    property_fallback = curated_frame.groupby(["property_type"])["price_per_m2"].median().to_dict()
    baseline.update(
        {f"fallback|{property_type}": float(value) for property_type, value in property_fallback.items()}
    )
    baseline["fallback|all"] = float(curated_frame["price_per_m2"].median())
    return baseline


def prepare_training_matrices(
    curated_frame: pd.DataFrame,
    *,
    feature_columns: list[str] | None = None,
    comparable_lookup: ComparableLookup | None = None,
) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    frame = build_model_frame(curated_frame, comparable_lookup=comparable_lookup)
    selected_columns = feature_columns or FEATURE_COLUMNS
    x_frame = frame[selected_columns].copy()
    y_series = frame["price_czk"].astype(float)
    return x_frame, y_series, frame


def feature_payload_from_request(
    payload: dict[str, Any],
    *,
    comparable_lookup: ComparableLookup | None = None,
) -> dict[str, Any]:
    lat = safe_float(payload.get("lat"))
    lng = safe_float(payload.get("lng"))
    room_count = parse_room_count(payload.get("disposition"))
    floor_no = safe_float(payload.get("floorNo"), -1.0)
    total_floors = safe_float(payload.get("totalFloors"), -1.0)
    floor_area_m2 = float(payload["floorAreaM2"])
    location_cluster = str(payload.get("locationCluster") or payload["districtPrague"])
    market_segment = str(payload.get("marketSegment") or infer_market_segment(location_cluster, payload["districtPrague"]))
    geocode_resolution = str(payload.get("geocodeResolution") or ("exact" if lat is not None and lng is not None else "fallback_manual"))
    has_coordinates = lat is not None and lng is not None
    missing_count = count_missing_core_inputs(
        {
            "disposition": payload.get("disposition"),
            "land_area_m2": payload.get("landAreaM2"),
            "floor_no": payload.get("floorNo"),
            "total_floors": payload.get("totalFloors"),
            "ownership": payload.get("ownership"),
            "condition": payload.get("condition"),
            "construction": payload.get("construction"),
            "energy_label": payload.get("energyLabel"),
            "has_elevator": payload.get("hasElevator"),
            "has_parking": payload.get("hasParking"),
            "has_cellar": payload.get("hasCellar"),
            "has_balcony_or_loggia": payload.get("hasBalconyOrLoggia"),
        }
    )
    feature_payload = {
        "district_prague": payload["districtPrague"],
        "property_type": payload["propertyType"],
        "disposition": payload.get("disposition") or "unknown",
        "floor_area_m2": floor_area_m2,
        "land_area_m2": safe_float(payload.get("landAreaM2"), 0.0),
        "land_area_missing": "yes" if payload.get("landAreaM2") is None else "no",
        "floor_no": float(floor_no if floor_no is not None else -1.0),
        "floor_no_missing": "yes" if payload.get("floorNo") is None else "no",
        "total_floors": float(total_floors if total_floors is not None else -1.0),
        "total_floors_missing": "yes" if payload.get("totalFloors") is None else "no",
        "ownership": normalize_ownership(payload.get("ownership")),
        "condition": payload.get("condition") or "unknown",
        "construction": payload.get("construction") or "unknown",
        "energy_label": normalize_energy_label(payload.get("energyLabel")),
        "has_elevator": normalize_boolean_flag(payload.get("hasElevator")),
        "has_parking": normalize_boolean_flag(payload.get("hasParking")),
        "has_cellar": normalize_boolean_flag(payload.get("hasCellar")),
        "has_balcony_or_loggia": normalize_boolean_flag(payload.get("hasBalconyOrLoggia")),
        "distance_to_center_km": safe_float(payload.get("distanceToCenterKm")),
        "distance_to_metro_km": safe_float(
            payload.get("distanceToMetroKm"),
            nearest_transit_distance_km(lat, lng, "metroNodes"),
        ),
        "distance_to_rail_km": safe_float(
            payload.get("distanceToRailKm"),
            nearest_transit_distance_km(lat, lng, "railNodes"),
        ),
        "center_ring": center_ring(safe_float(payload.get("distanceToCenterKm"))),
        "market_segment": market_segment,
        "location_cluster": location_cluster,
        "h3_cell": str(payload.get("h3Cell") or compute_h3_cell(lat, lng)),
        "room_count": room_count,
        "area_per_room_m2": floor_area_m2 / room_count if room_count > 0 else floor_area_m2,
        "floor_position_ratio": floor_no / total_floors if floor_no is not None and total_floors is not None and floor_no >= 0 and total_floors > 0 else -1.0,
        "has_geocode_coordinates": "yes" if has_coordinates else "no",
        "geocode_resolution": geocode_resolution,
        "missing_core_feature_count": float(missing_count),
        "listing_input_quality_score": input_quality_score(
            missing_core_feature_count=missing_count,
            has_geocode_coordinates=has_coordinates,
            geocode_resolution=geocode_resolution,
        ),
        "model_segment": f"{market_segment}_{payload['propertyType']}",
    }
    if comparable_lookup is not None:
        feature_payload.update(apply_comparable_features(pd.DataFrame([feature_payload]), comparable_lookup).iloc[0].to_dict())
    return feature_payload
