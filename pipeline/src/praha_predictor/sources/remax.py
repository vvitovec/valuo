from __future__ import annotations

import hashlib
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


SITEMAP_URL = "https://www.remax-czech.cz/export/sitemap-listings.xml"
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
DETAIL_ID_RE = re.compile(r"/detail/(\d+)/")
ROW_RE = re.compile(
    r'<div class="pd-detail-info__row">\s*'
    r'<div class="pd-detail-info__label">(.*?)</div>\s*'
    r'<div class="pd-detail-info__value">(.*?)</div>\s*'
    r"</div>",
    re.S,
)
TITLE_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
ADDRESS_RE = re.compile(r'<h2 class="pd-header__address">(.*?)</h2>', re.S | re.I)
PRICE_RE = re.compile(r'<h2 class="pd-header__price">(.*?)</h2>', re.S | re.I)
GPS_RE = re.compile(r'data-gps="([^"]+)"')


def _clean_html_text(value: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", value)
    return " ".join(unescape(cleaned).replace("\xa0", " ").split())


def _parse_listing_id(url: str) -> str:
    match = DETAIL_ID_RE.search(url)
    if not match:
        raise ValueError(f"Unable to parse RE/MAX listing id from {url}")
    return match.group(1)


def _parse_detail_rows(html: str) -> dict[str, str]:
    details: dict[str, str] = {}
    for label, value in ROW_RE.findall(html):
        details[_clean_html_text(label).rstrip(":")] = _clean_html_text(value)
    return details


def _parse_dms_component(value: str) -> float:
    match = re.match(r"(\d+)°(\d+)'(\d+(?:\.\d+)?)\"?([NSEW])", value)
    if not match:
        raise ValueError(f"Unsupported DMS coordinate: {value}")
    degrees = float(match.group(1))
    minutes = float(match.group(2))
    seconds = float(match.group(3))
    direction = match.group(4)
    decimal = degrees + minutes / 60 + seconds / 3600
    if direction in {"S", "W"}:
        decimal *= -1
    return decimal


def _parse_gps(html: str) -> tuple[float | None, float | None]:
    match = GPS_RE.search(html)
    if not match:
        return None, None
    gps_text = _clean_html_text(match.group(1))
    parts = [part.strip() for part in gps_text.split(",")]
    if len(parts) != 2:
        return None, None
    try:
        return _parse_dms_component(parts[0]), _parse_dms_component(parts[1])
    except ValueError:
        return None, None


def _map_property_type(title_text: str, detail_rows: dict[str, str]) -> str | None:
    if re.search(r"prodej bytu", title_text, re.I) or detail_rows.get("Typ nemovitosti") == "Byty":
        return "flat"
    if re.search(r"prodej domu", title_text, re.I) or detail_rows.get("Typ nemovitosti") == "Domy":
        return "house"
    return None


def _map_disposition(title_text: str, detail_rows: dict[str, str]) -> str | None:
    disposition = detail_rows.get("Dispozice")
    if disposition:
        return disposition.lower()
    match = re.search(r"Prodej bytu ([^,]+)", title_text, re.I)
    if match:
        return match.group(1).split(" v ", 1)[0].replace(" ", "").lower()
    return None


def _map_ownership(title_text: str, detail_rows: dict[str, str]) -> str | None:
    ownership = detail_rows.get("Vlastnictví")
    if ownership:
        return ownership.lower()
    match = re.search(r"v ([^,]+) vlastnictví", title_text, re.I)
    return match.group(1).lower() if match else None


def _map_condition(detail_rows: dict[str, str]) -> str | None:
    mapping = {
        "novostavba": "new",
        "po rekonstrukci": "very_good",
        "velmi dobrý": "very_good",
        "dobrý": "good",
        "před rekonstrukcí": "before_reconstruction",
        "ve výstavbě": "new",
    }
    raw_value = detail_rows.get("Stav objektu")
    if not raw_value:
        return None
    lowered = raw_value.lower()
    return mapping.get(lowered, lowered)


def _map_construction(detail_rows: dict[str, str]) -> str | None:
    mapping = {
        "cihlová": "brick",
        "panelová": "panel",
        "smíšená": "mixed",
        "drevena": "wood",
        "dřevěná": "wood",
    }
    raw_value = detail_rows.get("Druh objektu")
    if not raw_value:
        return None
    lowered = raw_value.lower()
    return mapping.get(lowered, lowered)


def _map_energy_label(price_text: str, detail_rows: dict[str, str]) -> str | None:
    explicit = detail_rows.get("Energetická náročnost")
    if explicit and explicit[0].isalpha():
        return explicit[0].lower()
    match = re.search(r"\b([A-G])\b", price_text)
    if match:
        return match.group(1).lower()
    return None


def _extract_listing_native_district(title_text: str, address_text: str) -> str | None:
    for candidate in re.split(r"[,/]", f"{title_text}, {address_text}"):
        market_area = canonicalize_market_area(candidate, lat=None, lng=None)
        if market_area and market_area != "Praha okolí":
            return market_area
    return None


def _extract_price(price_text: str) -> float | None:
    if not price_text or "prodáno" in price_text.lower():
        return None
    match = re.search(r"(\d[\d\s\xa0]{4,})\s*Kč", price_text)
    if not match:
        return None
    return float(match.group(1).replace("\xa0", "").replace(" ", ""))


def _extract_area(title_text: str, detail_rows: dict[str, str]) -> float | None:
    for key in ("Podlahová plocha", "Užitná plocha"):
        value = detail_rows.get(key)
        if value:
            match = re.search(r"(\d+(?:[.,]\d+)?)\s*m²", value)
            if match:
                return float(match.group(1).replace(",", "."))
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*m²", title_text)
    if match:
        return float(match.group(1).replace(",", "."))
    return None


def _extract_land_area(detail_rows: dict[str, str]) -> float | None:
    for key in ("Plocha pozemku", "Zastavěná plocha"):
        value = detail_rows.get(key)
        if not value:
            continue
        match = re.search(r"(\d+(?:[.,]\d+)?)\s*m²", value)
        if match:
            return float(match.group(1).replace(",", "."))
    return None


def _probe_fields(payload: dict[str, Any]) -> dict[str, bool]:
    return {
        "price": payload.get("price_czk") is not None,
        "area": payload.get("floor_area_m2") is not None,
        "address_or_district": bool(payload.get("district_prague") or payload.get("address_text")),
        "property_type": payload.get("property_type") in {"flat", "house"},
        "offer_type": payload.get("offer_type") == "sale",
    }


class RemaxAdapter(ListingSourceAdapter):
    source_name = "remax"

    def __init__(self, config: PipelineConfig | None = None) -> None:
        self.config = config or PipelineConfig()

    def discover_listing_urls(self, run_context: RunContext) -> list[str]:
        xml_text = fetch_text(
            SITEMAP_URL,
            self.config,
            accept="application/xml,text/xml,*/*",
        ).text
        root = ET.fromstring(xml_text)
        urls = [
            loc.text
            for loc in root.findall(".//sm:loc", SITEMAP_NS)
            if loc.text
            and "/reality/detail/" in loc.text
            and re.search(r"prodej-(bytu|domu)", loc.text, re.I)
            and url_matches_prague_metro_region(loc.text)
        ]
        if run_context.max_listings:
            return urls[: run_context.max_listings]
        return urls

    def fetch_listing(self, url: str, run_context: RunContext) -> RawSnapshot:
        response = fetch_text(url, self.config)
        html = response.text
        title_match = TITLE_RE.search(html)
        address_match = ADDRESS_RE.search(html)
        price_match = PRICE_RE.search(html)
        detail_rows = _parse_detail_rows(html)
        title_text = _clean_html_text(title_match.group(1)) if title_match else ""
        address_text = _clean_html_text(address_match.group(1)) if address_match else ""
        price_text = _clean_html_text(price_match.group(1)) if price_match else ""
        lat, lng = _parse_gps(html)
        payload = {
            "title_text": title_text,
            "address_text": address_text,
            "price_text": price_text,
            "detail_rows": detail_rows,
            "district_prague": _extract_listing_native_district(title_text, address_text),
            "property_type": _map_property_type(title_text, detail_rows),
            "offer_type": "sale" if "prodej" in title_text.lower() else None,
            "disposition": _map_disposition(title_text, detail_rows),
            "ownership": _map_ownership(title_text, detail_rows),
            "condition": _map_condition(detail_rows),
            "construction": _map_construction(detail_rows),
            "energy_label": _map_energy_label(price_text, detail_rows),
            "floor_area_m2": _extract_area(title_text, detail_rows),
            "land_area_m2": _extract_land_area(detail_rows),
            "floor_no": float(detail_rows["Číslo podlaží"]) if detail_rows.get("Číslo podlaží") else None,
            "total_floors": float(detail_rows["Počet podlaží v objektu"]) if detail_rows.get("Počet podlaží v objektu") else None,
            "price_czk": _extract_price(price_text),
            "lat": lat,
            "lng": lng,
            "has_elevator": detail_rows.get("Výtah", "").lower() == "ano" if detail_rows.get("Výtah") else None,
            "has_parking": any(
                detail_rows.get(key, "").lower() == "ano"
                for key in ("Parkování", "Garáž", "Parkovací místo")
            ),
            "has_cellar": detail_rows.get("Sklep", "").lower() == "ano" if detail_rows.get("Sklep") else None,
            "has_balcony_or_loggia": any(
                detail_rows.get(key, "").lower() == "ano"
                for key in ("Balkón", "Lodžie", "Terasa")
            ),
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
                "discovery_method": "sitemap",
            },
        )

    def normalize(self, raw_snapshot: RawSnapshot) -> NormalizedListing | RejectReason:
        payload = raw_snapshot.payload
        lat = payload.get("lat")
        lng = payload.get("lng")
        market_area = canonicalize_market_area(
            payload.get("district_prague"),
            payload.get("address_text"),
            payload.get("title_text"),
            lat=lat,
            lng=lng,
            config=self.config,
        )
        price_czk = payload.get("price_czk")
        floor_area_m2 = payload.get("floor_area_m2")

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
        if not price_czk or not floor_area_m2:
            return RejectReason(
                source=raw_snapshot.source,
                source_listing_id=raw_snapshot.source_listing_id,
                observed_at=raw_snapshot.observed_at,
                listing_url=raw_snapshot.listing_url,
                content_hash=raw_snapshot.content_hash,
                reason="missing_price_or_area",
                details={"price": price_czk, "floor_area_m2": floor_area_m2},
            )

        detail_rows = payload.get("detail_rows", {})
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
            floor_area_m2=float(floor_area_m2),
            land_area_m2=float(payload["land_area_m2"]) if payload.get("land_area_m2") else None,
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
            price_czk=float(price_czk),
            price_per_m2=float(price_czk) / float(floor_area_m2),
            quality_flags=["remax_html_table_parse"]
            if detail_rows and not detail_rows.get("Energetická náročnost")
            else [],
        )

    def probe_source(self, sample_size: int) -> SourceProbeReport:
        run_context = RunContext(
            run_id="probe-remax",
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
        decision = "active_secondary" if coverage_score >= 0.8 else "reject_secondary"
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
