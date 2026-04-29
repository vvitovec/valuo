from __future__ import annotations

from praha_predictor.districts import canonicalize_prague_district, choose_best_prague_district


def test_canonicalize_prague_district_maps_city_parts_and_numeric_variants() -> None:
    assert canonicalize_prague_district("Praha - Vysočany") == "Praha 9"
    assert canonicalize_prague_district("obvod Praha 9") == "Praha 9"
    assert canonicalize_prague_district("Smíchov") == "Praha 5"


def test_choose_best_prague_district_prefers_listing_native() -> None:
    assert choose_best_prague_district("Praha - Smíchov", "Praha 5", "Praha 4") == "Praha 5"

