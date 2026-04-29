from __future__ import annotations

import json
import re
from functools import lru_cache
from math import atan2, cos, degrees, radians, sin

from praha_predictor.config import PRAGUE_CENTER, PipelineConfig, REPO_ROOT
from praha_predictor.districts import canonicalize_prague_district
from praha_predictor.geospatial import haversine_km
from praha_predictor.text import strip_accents


METRO_REGION_LABEL = "Praha okolí"
METRO_SUBREGION_CONFIG_PATH = REPO_ROOT / "shared" / "metro-subregions.json"
METRO_REGION_URL_HINTS = (
    "praha",
    "stredocesky",
    "praha-vychod",
    "praha-zapad",
    "kladno",
    "beroun",
    "benesov",
    "melnik",
    "ricany",
    "brandys",
    "cernosice",
    "jesenice",
    "hostivice",
    "roztoky",
    "kralupy",
    "celakovice",
    "unhost",
    "rudna",
)


def normalize_location_key(value: str | None) -> str:
    raw_value = (value or "").replace("\xa0", " ").replace("–", "-").replace("—", "-").strip().lower()
    raw_value = re.sub(r"\s+", " ", raw_value)
    return strip_accents(raw_value).strip(" ,-")


@lru_cache(maxsize=1)
def load_metro_subregion_alias_map() -> dict[str, str]:
    payload = json.loads(METRO_SUBREGION_CONFIG_PATH.read_text(encoding="utf-8"))
    alias_map: dict[str, str] = {}
    for item in payload["subregions"]:
        canonical = item["canonical"]
        for alias in {canonical, *item["aliases"]}:
            alias_key = normalize_location_key(alias)
            if alias_key and alias_key not in alias_map:
                alias_map[alias_key] = canonical
    return alias_map


def canonicalize_metro_subregion(value: str | None) -> str | None:
    if not value:
        return None
    alias_map = load_metro_subregion_alias_map()
    key = normalize_location_key(value)
    if not key:
        return None
    return alias_map.get(key)


def _bearing_from_prague(lat: float, lng: float) -> float:
    lat1 = radians(PRAGUE_CENTER[0])
    lat2 = radians(lat)
    diff_lng = radians(lng - PRAGUE_CENTER[1])
    y = sin(diff_lng) * cos(lat2)
    x = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(diff_lng)
    return (degrees(atan2(y, x)) + 360.0) % 360.0


def directional_metro_cluster(lat: float | None, lng: float | None) -> str:
    if lat is None or lng is None:
        return METRO_REGION_LABEL
    distance = haversine_km(float(lat), float(lng), PRAGUE_CENTER[0], PRAGUE_CENTER[1])
    if distance <= 12:
        return "Praha okraj"
    bearing = _bearing_from_prague(float(lat), float(lng))
    if 45 <= bearing < 135:
        return "Praha okolí východ"
    if 135 <= bearing < 225:
        return "Praha okolí jih"
    if 225 <= bearing < 315:
        return "Praha okolí západ"
    return "Praha okolí sever"


def is_within_prague_metro_region(
    lat: float | None,
    lng: float | None,
    config: PipelineConfig | None = None,
) -> bool:
    if lat is None or lng is None:
        return False
    config = config or PipelineConfig()
    return (
        haversine_km(float(lat), float(lng), PRAGUE_CENTER[0], PRAGUE_CENTER[1])
        <= config.metro_region_radius_km
    )


def canonicalize_market_area(
    *location_candidates: str | None,
    lat: float | None,
    lng: float | None,
    config: PipelineConfig | None = None,
) -> str | None:
    for candidate in location_candidates:
        if not candidate:
            continue
        candidate_text = str(candidate)
        for part in [candidate_text, *re.split(r"[,/|]", candidate_text)]:
            canonical_district = canonicalize_prague_district(part.strip())
            if canonical_district:
                return canonical_district
    if is_within_prague_metro_region(lat, lng, config):
        return METRO_REGION_LABEL
    return None


def derive_location_cluster(
    *location_candidates: str | None,
    district_prague: str | None = None,
    lat: float | None,
    lng: float | None,
    config: PipelineConfig | None = None,
) -> str | None:
    for candidate in (district_prague, *location_candidates):
        if not candidate:
            continue
        candidate_text = str(candidate)
        for part in [candidate_text, *re.split(r"[,/|]", candidate_text)]:
            normalized = part.strip()
            canonical_district = canonicalize_prague_district(normalized)
            if canonical_district:
                return canonical_district
            metro_subregion = canonicalize_metro_subregion(normalized)
            if metro_subregion:
                return metro_subregion
    market_area = canonicalize_market_area(
        district_prague,
        *location_candidates,
        lat=lat,
        lng=lng,
        config=config,
    )
    if market_area and market_area != METRO_REGION_LABEL:
        return market_area
    if is_within_prague_metro_region(lat, lng, config):
        return directional_metro_cluster(lat, lng)
    return market_area


def infer_market_segment(location_cluster: str | None, district_prague: str | None = None) -> str:
    if canonicalize_prague_district(location_cluster) or canonicalize_prague_district(district_prague):
        return "prague"
    return "metro"


def url_matches_prague_metro_region(url: str) -> bool:
    normalized_url = normalize_location_key(url)
    return any(hint in normalized_url for hint in METRO_REGION_URL_HINTS)
