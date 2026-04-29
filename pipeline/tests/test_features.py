import pandas as pd

from praha_predictor.features import build_model_frame, feature_payload_from_request


def test_build_model_frame_adds_location_and_ratio_features() -> None:
    frame = pd.DataFrame(
        [
            {
                "address_text": "Říčany",
                "district_prague": "Praha okolí",
                "lat": 49.9912,
                "lng": 14.6543,
                "property_type": "flat",
                "disposition": "3+kk",
                "floor_area_m2": 81.0,
                "land_area_m2": None,
                "floor_no": 2.0,
                "total_floors": 4.0,
                "ownership": "osobni",
                "condition": "very_good",
                "construction": "brick",
                "energy_label": "c",
                "has_elevator": True,
                "has_parking": False,
                "has_cellar": False,
                "has_balcony_or_loggia": True,
                "price_czk": 9500000.0,
                "price_per_m2": 117283.95,
            }
        ]
    )
    model_frame = build_model_frame(frame)
    row = model_frame.iloc[0]
    assert row["location_cluster"] == "Praha-východ"
    assert row["market_segment"] == "metro"
    assert row["room_count"] == 3.0
    assert row["area_per_room_m2"] == 27.0
    assert row["floor_position_ratio"] == 0.5
    assert row["model_segment"] == "metro_flat"


def test_feature_payload_from_request_adds_extended_features() -> None:
    payload = feature_payload_from_request(
        {
            "districtPrague": "Praha 9",
            "propertyType": "flat",
            "disposition": "2+kk",
            "floorAreaM2": 54,
            "distanceToCenterKm": 6.8,
            "locationCluster": "Praha 9",
            "marketSegment": "prague",
        }
    )
    assert payload["location_cluster"] == "Praha 9"
    assert payload["market_segment"] == "prague"
    assert payload["room_count"] == 2.0
    assert payload["model_segment"] == "prague_flat"


def test_feature_payload_preserves_unknown_boolean_semantics_for_missing_inputs() -> None:
    payload = feature_payload_from_request(
        {
            "districtPrague": "Praha 9",
            "propertyType": "flat",
            "disposition": "2+kk",
            "floorAreaM2": 54,
            "locationCluster": "Praha 9",
            "marketSegment": "prague",
            "distanceToCenterKm": None,
        }
    )
    assert payload["has_elevator"] == "unknown"
    assert payload["has_parking"] == "unknown"
    assert payload["distance_to_center_km"] is None
    assert payload["geocode_resolution"] == "fallback_manual"
