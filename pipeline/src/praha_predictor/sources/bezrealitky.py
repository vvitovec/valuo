from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from html import unescape
from typing import Any

from praha_predictor.config import PipelineConfig
from praha_predictor.http import HttpFetchError, fetch_text
from praha_predictor.market_scope import canonicalize_market_area, url_matches_prague_metro_region
from praha_predictor.schemas import (
    NormalizedListing,
    RawSnapshot,
    RejectReason,
    RunContext,
    SourceProbeReport,
)
from praha_predictor.sources.base import ListingSourceAdapter


SITEMAP_INDEX = "https://www.bezrealitky.cz/sitemap/sitemap.xml"
DETAIL_URL_RE = re.compile(r"/(\d+)-nabidka-[^/]+$")
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">\s*(.*?)\s*</script>',
    re.S,
)
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
FALLBACK_SEED_URLS = [
    "https://www.bezrealitky.cz/nemovitosti-byty-domy/981957-nabidka-prodej-bytu-podebradska-praha",
    "https://www.bezrealitky.cz/nemovitosti-byty-domy/1001327-nabidka-prodej-bytu-hermanova-praha",
    "https://www.bezrealitky.cz/nemovitosti-byty-domy/941938-nabidka-prodej-bytu-v-borovickach-hlavni-mesto-praha",
    "https://www.bezrealitky.cz/nemovitosti-byty-domy/982703-nabidka-prodej-bytu-frantiska-stepanka-praha",
]


def extract_next_data_from_html(html: str) -> dict[str, Any]:
    match = NEXT_DATA_RE.search(html)
    if not match:
        raise ValueError("Missing __NEXT_DATA__ payload")
    return json.loads(unescape(match.group(1)))


def extract_advert_payload(page_data: dict[str, Any]) -> dict[str, Any]:
    page_props = page_data.get("props", {}).get("pageProps", {})
    advert = page_props.get("advert") or page_props.get("origAdvert")
    if not isinstance(advert, dict):
        raise ValueError("Missing advert payload in Next data")
    return {
        "advert": advert,
        "page_props": page_props,
        "region_tree": page_props.get("regionTree") or advert.get("regionTree") or [],
    }


def parse_listing_id(url: str) -> str:
    match = DETAIL_URL_RE.search(url)
    if not match:
        raise ValueError(f"Unable to parse listing id from url: {url}")
    return match.group(1)


def _coerce_bool(value: Any) -> bool | None:
    if value in (None, "", "UNDEFINED"):
        return None
    return bool(value)


def _coerce_float(value: Any) -> float | None:
    if value in (None, "", "UNDEFINED"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_optional_text(raw_value: str | None) -> str | None:
    if raw_value in (None, "", "UNDEFINED"):
        return None
    return str(raw_value).lower()


def _map_disposition(raw_value: str | None) -> str | None:
    if not raw_value or raw_value == "UNDEFINED":
        return None
    return raw_value.replace("DISP_", "").replace("_", "+").lower()


def _extract_listing_native_district(advert_payload: dict[str, Any]) -> str | None:
    region_tree = advert_payload.get("region_tree") or []
    for region in region_tree:
        name = region.get("name")
        if not isinstance(name, str):
            continue
        market_area = canonicalize_market_area(name, lat=None, lng=None)
        if market_area and market_area != "Praha okolí":
            return market_area

    advert = advert_payload["advert"]
    address_text = advert.get("address") or advert.get("street") or ""
    for part in re.split(r"[,/]", str(address_text)):
        market_area = canonicalize_market_area(part, lat=None, lng=None)
        if market_area and market_area != "Praha okolí":
            return market_area
    return None


def _extract_related_urls(advert_payload: dict[str, Any]) -> list[str]:
    advert = advert_payload["advert"]
    page_props = advert_payload.get("page_props", {})
    related_adverts = (
        advert.get("relatedAdverts", {}).get("list")
        or page_props.get("advert", {}).get("relatedAdverts", {}).get("list")
        or []
    )
    urls: list[str] = []
    for related in related_adverts:
        uri = related.get("uri")
        if not isinstance(uri, str):
            continue
        related_url = f"https://www.bezrealitky.cz/nemovitosti-byty-domy/{uri}"
        if ("-bytu-" in related_url or "-domu-" in related_url) and "-nabidka-prodej-" in related_url:
            urls.append(related_url)
    return list(dict.fromkeys(urls))


def _probe_fields(advert_payload: dict[str, Any]) -> dict[str, bool]:
    advert = advert_payload["advert"]
    return {
        "price": advert.get("price") not in (None, 0),
        "area": advert.get("surface") not in (None, 0),
        "address_or_district": bool(
            _extract_listing_native_district(advert_payload) or advert.get("address")
        ),
        "property_type": advert.get("estateType") in {"BYT", "DUM"},
        "offer_type": advert.get("offerType") == "PRODEJ",
    }


class BezrealitkyAdapter(ListingSourceAdapter):
    source_name = "bezrealitky"

    def __init__(self, config: PipelineConfig | None = None) -> None:
        self.config = config or PipelineConfig()

    def discover_listing_urls(self, run_context: RunContext) -> list[str]:
        try:
            index_xml = fetch_text(SITEMAP_INDEX, self.config, accept="application/xml,text/xml,*/*").text
            root = ET.fromstring(index_xml)
            sitemap_urls = [
                loc.text
                for loc in root.findall(".//sm:loc", SITEMAP_NS)
                if loc.text and "sitemap_detail_" in loc.text
            ]
            urls: list[str] = []
            for sitemap_url in sitemap_urls:
                sitemap_xml = fetch_text(
                    sitemap_url,
                    self.config,
                    accept="application/xml,text/xml,*/*",
                ).text
                sitemap_root = ET.fromstring(sitemap_xml)
                urls.extend(
                    loc.text
                    for loc in sitemap_root.findall(".//sm:loc", SITEMAP_NS)
                    if loc.text
                    and "/nemovitosti-byty-domy/" in loc.text
                    and "-nabidka-prodej-" in loc.text
                    and ("-bytu-" in loc.text or "-domu-" in loc.text)
                    and url_matches_prague_metro_region(loc.text)
                )
                if run_context.max_listings and len(urls) >= run_context.max_listings:
                    break
            unique_urls = list(dict.fromkeys(urls))
            if run_context.max_listings:
                unique_urls = unique_urls[: run_context.max_listings]
            if not unique_urls:
                return self._discover_from_seed_graph(run_context)
            return unique_urls
        except Exception:
            return self._discover_from_seed_graph(run_context)

    def _discover_from_seed_graph(self, run_context: RunContext) -> list[str]:
        seen: set[str] = set()
        queue = list(FALLBACK_SEED_URLS)
        discovered: list[str] = []
        while queue and (run_context.max_listings is None or len(discovered) < run_context.max_listings):
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            discovered.append(url)
            try:
                snapshot = self.fetch_listing(url, run_context)
                for related_url in snapshot.meta.get("related_listing_urls", []):
                    if related_url not in seen:
                        queue.append(related_url)
            except Exception:
                continue
        return discovered[: run_context.max_listings] if run_context.max_listings else discovered

    def fetch_listing(self, url: str, run_context: RunContext) -> RawSnapshot:
        response = fetch_text(url, self.config)
        html = response.text
        next_data = extract_next_data_from_html(html)
        advert_payload = extract_advert_payload(next_data)
        listing_id = parse_listing_id(url)
        content_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()[:16]
        return RawSnapshot(
            source=self.source_name,
            source_listing_id=listing_id,
            observed_at=run_context.observed_at,
            listing_url=url,
            content_hash=content_hash,
            html=html,
            payload=advert_payload,
            meta={
                "run_id": run_context.run_id,
                "latency_ms": response.latency_ms,
                "probe_fields": _probe_fields(advert_payload),
                "related_listing_urls": _extract_related_urls(advert_payload),
                "discovery_method": "sitemap",
            },
        )

    def normalize(self, raw_snapshot: RawSnapshot) -> NormalizedListing | RejectReason:
        advert = raw_snapshot.payload["advert"]
        gps = advert.get("gps") or {}
        lat = float(gps.get("lat")) if gps.get("lat") is not None else None
        lng = float(gps.get("lng")) if gps.get("lng") is not None else None
        market_area = canonicalize_market_area(
            _extract_listing_native_district(raw_snapshot.payload),
            advert.get("city"),
            advert.get("address"),
            lat=lat,
            lng=lng,
            config=self.config,
        )

        if advert.get("currency") != "CZK":
            return RejectReason(
                source=raw_snapshot.source,
                source_listing_id=raw_snapshot.source_listing_id,
                observed_at=raw_snapshot.observed_at,
                listing_url=raw_snapshot.listing_url,
                content_hash=raw_snapshot.content_hash,
                reason="non_czk_currency",
                details={"currency": advert.get("currency")},
            )
        if advert.get("offerType") != "PRODEJ":
            return RejectReason(
                source=raw_snapshot.source,
                source_listing_id=raw_snapshot.source_listing_id,
                observed_at=raw_snapshot.observed_at,
                listing_url=raw_snapshot.listing_url,
                content_hash=raw_snapshot.content_hash,
                reason="wrong_offer_type",
                details={"offer_type": advert.get("offerType")},
            )
        if advert.get("estateType") not in {"BYT", "DUM"}:
            return RejectReason(
                source=raw_snapshot.source,
                source_listing_id=raw_snapshot.source_listing_id,
                observed_at=raw_snapshot.observed_at,
                listing_url=raw_snapshot.listing_url,
                content_hash=raw_snapshot.content_hash,
                reason="unsupported_property_type",
                details={"estate_type": advert.get("estateType")},
            )
        if not market_area:
            return RejectReason(
                source=raw_snapshot.source,
                source_listing_id=raw_snapshot.source_listing_id,
                observed_at=raw_snapshot.observed_at,
                listing_url=raw_snapshot.listing_url,
                content_hash=raw_snapshot.content_hash,
                reason="outside_target_region",
            )
        if lat is None or lng is None:
            return RejectReason(
                source=raw_snapshot.source,
                source_listing_id=raw_snapshot.source_listing_id,
                observed_at=raw_snapshot.observed_at,
                listing_url=raw_snapshot.listing_url,
                content_hash=raw_snapshot.content_hash,
                reason="missing_coordinates",
            )

        price = advert.get("price")
        area = advert.get("surface")
        if not price or not area:
            return RejectReason(
                source=raw_snapshot.source,
                source_listing_id=raw_snapshot.source_listing_id,
                observed_at=raw_snapshot.observed_at,
                listing_url=raw_snapshot.listing_url,
                content_hash=raw_snapshot.content_hash,
                reason="missing_price_or_area",
                details={"price": price, "surface": area},
            )

        floor_area_m2 = float(area)
        price_czk = float(price)
        quality_flags: list[str] = []
        if advert.get("buildingCondition") in {"BAD", "WORSE"}:
            quality_flags.append("weaker_condition_signal")

        return NormalizedListing(
            source=raw_snapshot.source,
            source_listing_id=raw_snapshot.source_listing_id,
            observed_at=raw_snapshot.observed_at,
            listing_url=raw_snapshot.listing_url,
            content_hash=raw_snapshot.content_hash,
            address_text=str(advert.get("address") or advert.get("street") or ""),
            district_prague=market_area,
            lat=lat,
            lng=lng,
            property_type="flat" if advert.get("estateType") == "BYT" else "house",
            offer_type="sale",
            disposition=_map_disposition(advert.get("disposition")),
            floor_area_m2=floor_area_m2,
            land_area_m2=_coerce_float(advert.get("landSurface")),
            floor_no=_coerce_float(advert.get("floor")),
            total_floors=_coerce_float(advert.get("numberOfFloors")),
            ownership=_normalize_optional_text(advert.get("ownership")),
            condition=_normalize_optional_text(
                advert.get("condition") or advert.get("buildingCondition")
            ),
            construction=_normalize_optional_text(advert.get("construction")),
            energy_label=_normalize_optional_text(advert.get("penb")),
            has_elevator=_coerce_bool(advert.get("lift")),
            has_parking=_coerce_bool(advert.get("parking")),
            has_cellar=_coerce_bool(advert.get("cellar")),
            has_balcony_or_loggia=_coerce_bool(advert.get("balcony") or advert.get("loggia")),
            price_czk=price_czk,
            price_per_m2=price_czk / floor_area_m2,
            quality_flags=quality_flags,
        )

    def probe_source(self, sample_size: int) -> SourceProbeReport:
        run_context = RunContext(
            run_id="probe-bezrealitky",
            observed_at="1970-01-01T00:00:00+00:00",
            max_listings=sample_size,
            source=self.source_name,
        )
        urls = self.discover_listing_urls(run_context)[:sample_size]
        field_hits = {
            "price": 0,
            "area": 0,
            "address_or_district": 0,
            "property_type": 0,
            "offer_type": 0,
        }
        accepted_count = 0
        sample_failures: list[dict[str, Any]] = []
        for url in urls:
            try:
                snapshot = self.fetch_listing(url, run_context)
                probe_fields = snapshot.meta.get("probe_fields", {})
                for field_name in field_hits:
                    field_hits[field_name] += int(bool(probe_fields.get(field_name)))
                normalized = self.normalize(snapshot)
                if isinstance(normalized, RejectReason):
                    sample_failures.append({"url": url, "reason": normalized.reason})
                else:
                    accepted_count += 1
            except HttpFetchError as error:
                sample_failures.append({"url": url, "reason": error.failure_class})
        sampled_count = max(len(urls), 1)
        coverage = {
            field_name: round(hit_count / sampled_count, 4)
            for field_name, hit_count in field_hits.items()
        }
        coverage_score = round(min(coverage.values()), 4) if coverage else 0.0
        return SourceProbeReport(
            source=self.source_name,
            sampled_urls=urls,
            sampled_count=len(urls),
            field_coverage=coverage,
            accepted_count=accepted_count,
            coverage_score=coverage_score,
            decision="active_primary",
            sample_failures=sample_failures[:10],
        )
