from __future__ import annotations

import json
import re
from functools import lru_cache

from praha_predictor.config import REPO_ROOT
from praha_predictor.text import strip_accents


DISTRICT_CONFIG_PATH = REPO_ROOT / "shared" / "prague-districts.json"


def normalize_district_key(value: str | None) -> str:
    raw_value = (value or "").replace("\xa0", " ").replace("–", "-").replace("—", "-").strip().lower()
    raw_value = re.sub(r"^hlavni mesto\s+", "", raw_value)
    raw_value = re.sub(r"^hlavni město\s+", "", raw_value)
    raw_value = re.sub(r"^mestska cast\s+", "", raw_value)
    raw_value = re.sub(r"^městská část\s+", "", raw_value)
    raw_value = re.sub(r"^obvod\s+", "", raw_value)
    raw_value = re.sub(r"^praha\s*-\s*", "", raw_value)
    raw_value = re.sub(r"^praha\s+", "praha ", raw_value)
    raw_value = re.sub(r"\s+", " ", raw_value)
    return strip_accents(raw_value).strip(" ,-")


@lru_cache(maxsize=1)
def load_district_alias_map() -> dict[str, str]:
    payload = json.loads(DISTRICT_CONFIG_PATH.read_text(encoding="utf-8"))
    alias_map: dict[str, str] = {}
    for item in payload["districts"]:
        canonical = item["canonical"]
        for alias in {canonical, *item["aliases"]}:
            alias_key = normalize_district_key(alias)
            if alias_key and alias_key not in alias_map:
                alias_map[alias_key] = canonical
    return alias_map


def canonicalize_prague_district(value: str | None) -> str | None:
    if not value:
        return None
    alias_map = load_district_alias_map()
    key = normalize_district_key(value)
    if not key:
        return None
    if key in alias_map:
        return alias_map[key]
    prefixed_numeric = re.match(r"^praha\s+(\d+)\s*-\s*.+$", key)
    if prefixed_numeric:
        return f"Praha {prefixed_numeric.group(1)}"
    if key.startswith("praha ") and key[6:].isdigit():
        return f"Praha {key[6:]}"
    return None


def choose_best_prague_district(
    listing_native_value: str | None,
    external_value: str | None,
    manual_value: str | None,
) -> str | None:
    for candidate in (listing_native_value, external_value, manual_value):
        canonical = canonicalize_prague_district(candidate)
        if canonical:
            return canonical
    return None


def is_prague_district(value: str | None) -> bool:
    return canonicalize_prague_district(value) is not None
