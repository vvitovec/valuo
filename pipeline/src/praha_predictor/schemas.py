from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RunContext:
    run_id: str
    observed_at: str
    max_listings: int | None = None
    source: str = "bezrealitky"


@dataclass
class RawSnapshot:
    source: str
    source_listing_id: str
    observed_at: str
    listing_url: str
    content_hash: str
    html: str
    payload: dict[str, Any]
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RejectReason:
    source: str
    source_listing_id: str
    observed_at: str
    listing_url: str
    content_hash: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NormalizedListing:
    source: str
    source_listing_id: str
    observed_at: str
    listing_url: str
    content_hash: str
    address_text: str
    district_prague: str
    lat: float
    lng: float
    property_type: str
    offer_type: str
    disposition: str | None
    floor_area_m2: float
    land_area_m2: float | None
    floor_no: float | None
    total_floors: float | None
    ownership: str | None
    condition: str | None
    construction: str | None
    energy_label: str | None
    has_elevator: bool | None
    has_parking: bool | None
    has_cellar: bool | None
    has_balcony_or_loggia: bool | None
    price_czk: float
    price_per_m2: float
    quality_flags: list[str] = field(default_factory=list)
    reject_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["quality_flags"] = list(self.quality_flags)
        return payload


@dataclass
class SourceProbeReport:
    source: str
    sampled_urls: list[str]
    sampled_count: int
    field_coverage: dict[str, float]
    accepted_count: int
    coverage_score: float
    decision: str
    sample_failures: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

