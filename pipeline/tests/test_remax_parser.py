from __future__ import annotations

from praha_predictor.schemas import RawSnapshot
from praha_predictor.sources.remax import RemaxAdapter


def test_remax_adapter_normalizes_parsed_payload() -> None:
    adapter = RemaxAdapter()
    raw_snapshot = RawSnapshot(
        source="remax",
        source_listing_id="403028",
        observed_at="2026-03-30T09:00:00+00:00",
        listing_url="https://www.remax-czech.cz/reality/detail/403028/prodej-bytu-3-kk-v-osobnim-vlastnictvi-100-m2-praha-4-modrany",
        content_hash="abc123",
        html="<html></html>",
        payload={
            "title_text": "Prodej bytu 3+kk v osobním vlastnictví 100 m², Praha 4 - Modřany",
            "address_text": "Praha 4 – Modřany",
            "price_text": "17 946 000 Kč",
            "detail_rows": {
                "Dispozice": "3+kk",
                "Číslo podlaží": "3",
                "Počet podlaží v objektu": "6",
                "Druh objektu": "Cihlová",
                "Stav objektu": "Po rekonstrukci",
                "Vlastnictví": "Osobní"
            },
            "district_prague": "Praha 4 - Modřany",
            "property_type": "flat",
            "offer_type": "sale",
            "disposition": "3+kk",
            "ownership": "osobní",
            "condition": "very_good",
            "construction": "brick",
            "energy_label": "g",
            "floor_area_m2": 100.0,
            "land_area_m2": None,
            "floor_no": 3.0,
            "total_floors": 6.0,
            "price_czk": 17946000.0,
            "lat": 50.004,
            "lng": 14.41,
            "has_elevator": True,
            "has_parking": False,
            "has_cellar": None,
            "has_balcony_or_loggia": None
        },
    )
    normalized = adapter.normalize(raw_snapshot)
    assert normalized.district_prague == "Praha 4"
    assert normalized.property_type == "flat"
    assert round(normalized.price_per_m2) == 179460

