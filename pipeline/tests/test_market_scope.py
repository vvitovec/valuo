from praha_predictor.market_scope import (
    METRO_REGION_LABEL,
    canonicalize_market_area,
    derive_location_cluster,
    is_within_prague_metro_region,
)


def test_market_area_keeps_prague_district() -> None:
    market_area = canonicalize_market_area("Praha - Vysočany", lat=50.1026, lng=14.5084)
    assert market_area == "Praha 9"


def test_market_area_maps_nearby_city_to_prague_okoli() -> None:
    market_area = canonicalize_market_area("Říčany", lat=49.9912, lng=14.6543)
    assert market_area == METRO_REGION_LABEL
    assert is_within_prague_metro_region(49.9912, 14.6543) is True


def test_location_cluster_prefers_named_metro_subregion() -> None:
    cluster = derive_location_cluster("Brandýs nad Labem-Stará Boleslav", district_prague="Praha okolí", lat=50.1871, lng=14.6636)
    assert cluster == "Praha-východ"


def test_location_cluster_recovers_prague_district_from_outer_alias() -> None:
    cluster = derive_location_cluster("Dolní Chabry", district_prague="Praha okolí", lat=50.1481, lng=14.4829)
    assert cluster == "Praha 8"
