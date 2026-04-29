from __future__ import annotations

import hashlib
import json
import math
import re
from functools import lru_cache
from html import unescape
from typing import Any
from urllib.parse import urlencode

from praha_predictor.config import PipelineConfig
from praha_predictor.http import HttpFetchError, fetch_text
from praha_predictor.market_scope import canonicalize_market_area
from praha_predictor.schemas import (
    NormalizedListing,
    RawSnapshot,
    RejectReason,
    RunContext,
    SourceProbeReport,
)
from praha_predictor.sources.base import ListingSourceAdapter


SEGMENT_URLS = [
    "https://realitymix.cz/reality/byty/prodej/praha",
    "https://realitymix.cz/reality/domy/prodej/praha",
    "https://realitymix.cz/reality/byty/prodej/stredocesky",
    "https://realitymix.cz/reality/domy/prodej/stredocesky",
]
DETAIL_LINK_RE = re.compile(r'href="(https://realitymix\.cz/detail/[^"#?]+)"', re.I)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)
HEADING_TITLE_RE = re.compile(r'<h1 class="advert-detail-heading__title">(.*?)</h1>', re.I | re.S)
HEADING_ADDRESS_RE = re.compile(r'<p class="advert-detail-heading__address">(.*?)</p>', re.I | re.S)
SHORT_PROP_ROW_RE = re.compile(r"<tr[^>]*>\s*<td>\s*(.*?)\s*</td>\s*<td>\s*(.*?)\s*</td>\s*</tr>", re.I | re.S)
DETAIL_ITEM_RE = re.compile(r'<li class="detail-information__data-item">\s*<span>(.*?)</span>\s*<span>(.*?)</span>\s*</li>', re.I | re.S)
GPS_LAT_RE = re.compile(r'data-gps-lat="([^"]+)"', re.I)
GPS_LON_RE = re.compile(r'data-gps-lon="([^"]+)"', re.I)
ADDRESS_RE = re.compile(r'data-address="([^"]+)"', re.I)
DETAIL_ID_RE = re.compile(r"-(\d+)\.html$", re.I)
PAGINATOR_RE = re.compile(r"Zobrazujeme výsledky\s+\d+-\d+\s+z celkem\s+(\d+)\s+nalezených", re.I)


def _clean_html_text(value: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", value)
    return " ".join(unescape(cleaned).replace("\xa0", " ").split())


def _parse_listing_id(url: str) -> str:
    match = DETAIL_ID_RE.search(url)
    if not match:
        raise ValueError(f"Unable to parse RealityMix listing id from {url}")
    return match.group(1)


def _parse_total_results(html: str) -> int | None:
    match = PAGINATOR_RE.search(html)
    return int(match.group(1)) if match else None


def _parse_listing_urls(html: str) -> list[str]:
    urls: list[str] = []
    for detail_url in DETAIL_LINK_RE.findall(html):
        if not re.search(r"/detail/.+-\d+\.html$", detail_url):
            continue
        urls.append(detail_url)
    return list(dict.fromkeys(urls))


def _parse_title(html: str) -> str:
    match = TITLE_RE.search(html)
    return _clean_html_text(match.group(1)) if match else ""


def _parse_heading_title(html: str) -> str:
    match = HEADING_TITLE_RE.search(html)
    return _clean_html_text(match.group(1)) if match else ""


def _parse_heading_address(html: str) -> str:
    match = HEADING_ADDRESS_RE.search(html)
    return _clean_html_text(match.group(1)) if match else ""


def _parse_short_props(html: str) -> dict[str, str]:
    payload: dict[str, str] = {}
    for key, value in SHORT_PROP_ROW_RE.findall(html):
        payload[_clean_html_text(key).rstrip(":")] = _clean_html_text(value)
    return payload


def _parse_detail_items(html: str) -> dict[str, str]:
    payload: dict[str, str] = {}
    for key, value in DETAIL_ITEM_RE.findall(html):
        payload[_clean_html_text(key).rstrip(":")] = _clean_html_text(value)
    return payload


def _parse_property_type(*texts: str) -> str | None:
    lowered = " | ".join(texts).lower()
    if re.search(r"\b(byt(?:u|y)?|jednotk[ay]|mezonet|apartm[aá]n|studio|garsoni[eé]ra|duplex)\b", lowered):
        return "flat"
    if re.search(r"\b(d[uů]m|domu|d[uů]m/vily|vila|vily|rodinn[yý]\s+d[uů]m|rd\b|rrd\b|radov[eý]\s+d[uů]m|bungalov)\b", lowered):
        return "house"
    return None


def _extract_disposition(title_text: str, short_props: dict[str, str]) -> str | None:
    combined = short_props.get("Dispozice/podlahová plocha", "")
    match = re.search(r"(\d\+[^/\s,]+)", combined)
    if match:
        return match.group(1).lower()
    match = re.search(r"prodej bytu\s+(\d\+[^,\s]+)", title_text, re.I)
    return match.group(1).lower() if match else None


def _extract_floor_area(title_text: str, short_props: dict[str, str], detail_items: dict[str, str]) -> float | None:
    combined = short_props.get("Dispozice/podlahová plocha", "")
    match = re.search(r"/\s*(\d+(?:[.,]\d+)?)\s*m²", combined)
    if match:
        return float(match.group(1).replace(",", "."))
    for key in (
        "Užitná plocha",
        "Celková podlahová plocha",
        "Podlahová plocha",
        "Plocha bytu",
    ):
        value = detail_items.get(key)
        if not value:
            continue
        match = re.search(r"(\d+(?:[.,]\d+)?)\s*m²", value)
        if match:
            return float(match.group(1).replace(",", "."))
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*m²", title_text)
    if match:
        return float(match.group(1).replace(",", "."))
    return None


def _extract_price(short_props: dict[str, str], html: str) -> float | None:
    price_text = short_props.get("Cena", "")
    match = re.search(r"(\d[\d\s\xa0]{4,})\s*Kč", price_text)
    if not match:
        match = re.search(r"<td>Cena:</td>\s*<td>(.*?)</td>", html, re.I | re.S)
        if match:
            price_text = _clean_html_text(match.group(1))
            match = re.search(r"(\d[\d\s\xa0]{4,})\s*Kč", price_text)
    if not match:
        match = re.search(r"frm\[rozpocet\]=(\d{6,})", html)
        if match:
            return float(match.group(1))
        return None
    return float(match.group(1).replace("\xa0", "").replace(" ", ""))


@lru_cache(maxsize=512)
def _geocode_address(address_text: str, user_agent: str, timeout_seconds: int, retries: int, retry_backoff_seconds: float) -> tuple[float | None, float | None]:
    if not address_text:
        return None, None
    config = PipelineConfig(
        user_agent=user_agent,
        request_timeout_seconds=timeout_seconds,
        request_retries=retries,
        retry_backoff_seconds=retry_backoff_seconds,
    )
    query = urlencode(
        {
            "q": f"{address_text}, Česko",
            "format": "jsonv2",
            "limit": 1,
            "addressdetails": 1,
        }
    )
    try:
        response = fetch_text(
            f"https://nominatim.openstreetmap.org/search?{query}",
            config,
            accept="application/json,text/plain,*/*",
        )
        payload = json.loads(response.text)
        if not payload:
            return None, None
        first = payload[0]
        return float(first["lat"]), float(first["lon"])
    except Exception:
        return None, None


def _extract_gps(html: str) -> tuple[float | None, float | None]:
    lat_match = GPS_LAT_RE.search(html)
    lon_match = GPS_LON_RE.search(html)
    if not lat_match or not lon_match:
        return None, None
    return float(lat_match.group(1)), float(lon_match.group(1))


def _extract_address(html: str, title_text: str) -> str:
    match = ADDRESS_RE.search(html)
    if match:
        address = _clean_html_text(match.group(1))
        if address:
            return address
    heading_address = _parse_heading_address(html)
    if heading_address:
        return heading_address
    parts = [part.strip() for part in title_text.split(",")]
    return parts[1] if len(parts) > 1 else title_text


def _extract_energy_label(detail_items: dict[str, str]) -> str | None:
    value = detail_items.get("Energetická náročnost budovy")
    if value and value[0].isalpha():
        return value[0].lower()
    return None


def _extract_ownership(detail_items: dict[str, str]) -> str | None:
    value = detail_items.get("Vlastnictví")
    return value.lower() if value else None


def _extract_condition(detail_items: dict[str, str]) -> str | None:
    mapping = {
        "novostavba": "new",
        "velmi dobrý": "very_good",
        "dobrý": "good",
        "po rekonstrukci": "very_good",
        "před rekonstrukcí": "before_reconstruction",
    }
    value = detail_items.get("Stav objektu")
    if not value:
        return None
    lowered = value.lower()
    return mapping.get(lowered, lowered)


def _extract_construction(detail_items: dict[str, str]) -> str | None:
    mapping = {
        "cihlová": "brick",
        "panelová": "panel",
        "smíšená": "mixed",
        "dřevěná": "wood",
        "drevena": "wood",
    }
    value = detail_items.get("Konstrukce") or detail_items.get("Druh objektu")
    if not value:
        return None
    lowered = value.lower()
    return mapping.get(lowered, lowered)


def _extract_float_field(detail_items: dict[str, str], key: str) -> float | None:
    value = detail_items.get(key)
    if not value:
        return None
    match = re.search(r"(\d+(?:[.,]\d+)?)", value)
    if not match:
        return None
    return float(match.group(1).replace(",", "."))


def _extract_yes_no_flag(detail_items: dict[str, str], *keys: str) -> bool | None:
    for key in keys:
        value = detail_items.get(key)
        if not value:
            continue
        lowered = value.lower()
        if lowered == "ano":
            return True
        if lowered == "ne":
            return False
        match = re.search(r"(\d+(?:[.,]\d+)?)", value)
        if match:
            return float(match.group(1).replace(",", ".")) > 0
        return True
    return None


def _probe_fields(payload: dict[str, Any]) -> dict[str, bool]:
    return {
        "price": payload.get("price_czk") is not None,
        "area": payload.get("floor_area_m2") is not None,
        "address_or_district": bool(payload.get("address_text")),
        "property_type": payload.get("property_type") in {"flat", "house"},
        "offer_type": payload.get("offer_type") == "sale",
    }


class RealityMixAdapter(ListingSourceAdapter):
    source_name = "realitymix"

    def __init__(self, config: PipelineConfig | None = None) -> None:
        self.config = config or PipelineConfig()

    def discover_listing_urls(self, run_context: RunContext) -> list[str]:
        target = run_context.max_listings or self.config.max_listings_default
        collected: list[str] = []
        seen: set[str] = set()
        pages_per_segment = max(3, math.ceil((target / len(SEGMENT_URLS)) / 20 * 1.3))

        for segment_url in SEGMENT_URLS:
            first_page_html = fetch_text(segment_url, self.config).text
            total_results = _parse_total_results(first_page_html) or 0
            max_pages = max(1, min(math.ceil(total_results / 20), pages_per_segment))
            page_urls = [segment_url] + [f"{segment_url}?stranka={page}" for page in range(2, max_pages + 1)]
            for page_url in page_urls:
                html = first_page_html if page_url == segment_url else fetch_text(page_url, self.config).text
                for detail_url in _parse_listing_urls(html):
                    if detail_url not in seen:
                        seen.add(detail_url)
                        collected.append(detail_url)
                if len(collected) >= target:
                    return collected[:target]
        return collected[:target]

    def fetch_listing(self, url: str, run_context: RunContext) -> RawSnapshot:
        response = fetch_text(url, self.config)
        html = response.text
        title_text = _parse_title(html)
        heading_title = _parse_heading_title(html)
        short_props = _parse_short_props(html)
        detail_items = _parse_detail_items(html)
        lat, lng = _extract_gps(html)
        address_text = _extract_address(html, title_text)
        if lat is None or lng is None:
            lat, lng = _geocode_address(
                address_text,
                self.config.user_agent,
                self.config.request_timeout_seconds,
                self.config.request_retries,
                self.config.retry_backoff_seconds,
            )
        payload = {
            "title_text": title_text,
            "heading_title": heading_title,
            "address_text": address_text,
            "short_props": short_props,
            "detail_items": detail_items,
            "property_type": _parse_property_type(title_text, heading_title, address_text),
            "offer_type": "sale",
            "disposition": _extract_disposition(title_text, short_props),
            "floor_area_m2": _extract_floor_area(title_text, short_props, detail_items),
            "price_czk": _extract_price(short_props, html),
            "ownership": _extract_ownership(detail_items),
            "condition": _extract_condition(detail_items),
            "construction": _extract_construction(detail_items),
            "energy_label": _extract_energy_label(detail_items),
            "land_area_m2": _extract_float_field(detail_items, "Plocha parcely"),
            "floor_no": _extract_float_field(detail_items, "Patro") or _extract_float_field(detail_items, "Číslo podlaží v domě"),
            "total_floors": _extract_float_field(detail_items, "Počet podlaží") or _extract_float_field(detail_items, "Počet podlaží objektu"),
            "has_elevator": _extract_yes_no_flag(detail_items, "Výtah"),
            "has_parking": _extract_yes_no_flag(detail_items, "Parkování", "Garáž", "Parkovací místo"),
            "has_cellar": _extract_yes_no_flag(detail_items, "Sklep"),
            "has_balcony_or_loggia": _extract_yes_no_flag(detail_items, "Balkon", "Lodžie", "Terasa"),
            "lat": lat,
            "lng": lng,
        }
        listing_id = _parse_listing_id(url)
        content_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()[:16]
        return RawSnapshot(
            source=self.source_name,
            source_listing_id=listing_id,
            observed_at=run_context.observed_at,
            listing_url=url,
            content_hash=content_hash,
            html=html,
            payload=payload,
            meta={
                "run_id": run_context.run_id,
                "latency_ms": response.latency_ms,
                "probe_fields": _probe_fields(payload),
                "discovery_method": "list_pagination",
            },
        )

    def normalize(self, raw_snapshot: RawSnapshot) -> NormalizedListing | RejectReason:
        payload = raw_snapshot.payload
        lat = payload.get("lat")
        lng = payload.get("lng")
        market_area = canonicalize_market_area(
            payload.get("address_text"),
            payload.get("title_text"),
            lat=lat,
            lng=lng,
            config=self.config,
        )
        if payload.get("offer_type") != "sale":
            return RejectReason(
                source=raw_snapshot.source,
                source_listing_id=raw_snapshot.source_listing_id,
                observed_at=raw_snapshot.observed_at,
                listing_url=raw_snapshot.listing_url,
                content_hash=raw_snapshot.content_hash,
                reason="wrong_offer_type",
            )
        if payload.get("property_type") not in {"flat", "house"}:
            return RejectReason(
                source=raw_snapshot.source,
                source_listing_id=raw_snapshot.source_listing_id,
                observed_at=raw_snapshot.observed_at,
                listing_url=raw_snapshot.listing_url,
                content_hash=raw_snapshot.content_hash,
                reason="unsupported_property_type",
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
        if not payload.get("price_czk") or not payload.get("floor_area_m2"):
            return RejectReason(
                source=raw_snapshot.source,
                source_listing_id=raw_snapshot.source_listing_id,
                observed_at=raw_snapshot.observed_at,
                listing_url=raw_snapshot.listing_url,
                content_hash=raw_snapshot.content_hash,
                reason="missing_price_or_area",
                details={
                    "price": payload.get("price_czk"),
                    "floor_area_m2": payload.get("floor_area_m2"),
                },
            )
        return NormalizedListing(
            source=raw_snapshot.source,
            source_listing_id=raw_snapshot.source_listing_id,
            observed_at=raw_snapshot.observed_at,
            listing_url=raw_snapshot.listing_url,
            content_hash=raw_snapshot.content_hash,
            address_text=str(payload.get("address_text") or payload.get("title_text") or ""),
            district_prague=market_area,
            lat=float(lat),
            lng=float(lng),
            property_type=str(payload["property_type"]),
            offer_type="sale",
            disposition=payload.get("disposition"),
            floor_area_m2=float(payload["floor_area_m2"]),
            land_area_m2=float(payload["land_area_m2"]) if payload.get("land_area_m2") is not None else None,
            floor_no=float(payload["floor_no"]) if payload.get("floor_no") is not None else None,
            total_floors=float(payload["total_floors"]) if payload.get("total_floors") is not None else None,
            ownership=payload.get("ownership"),
            condition=payload.get("condition"),
            construction=payload.get("construction"),
            energy_label=payload.get("energy_label"),
            has_elevator=payload.get("has_elevator"),
            has_parking=payload.get("has_parking"),
            has_cellar=payload.get("has_cellar"),
            has_balcony_or_loggia=payload.get("has_balcony_or_loggia"),
            price_czk=float(payload["price_czk"]),
            price_per_m2=float(payload["price_czk"]) / float(payload["floor_area_m2"]),
            quality_flags=["realitymix_html_parse"],
        )

    def probe_source(self, sample_size: int) -> SourceProbeReport:
        run_context = RunContext(
            run_id="probe-realitymix",
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
        decision = "active_tertiary" if coverage_score >= 0.8 else "reject_tertiary"
        return SourceProbeReport(
            source=self.source_name,
            sampled_urls=urls,
            sampled_count=len(urls),
            field_coverage=coverage,
            accepted_count=accepted_count,
            coverage_score=coverage_score,
            decision=decision,
            sample_failures=sample_failures[:10],
        )
