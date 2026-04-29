from __future__ import annotations

from praha_predictor.schemas import RawSnapshot
from praha_predictor.sources.bezrealitky import (
    BezrealitkyAdapter,
    extract_advert_payload,
    extract_next_data_from_html,
)


def make_next_data_html(advert_key: str) -> str:
    return f"""
    <html><body>
      <script id="__NEXT_DATA__" type="application/json">
        {{
          "props": {{
            "pageProps": {{
              "{advert_key}": {{
                "id": "981957",
                "address": "Poděbradská, Praha - Vysočany",
                "city": "Praha",
                "estateType": "BYT",
                "offerType": "PRODEJ",
                "disposition": "DISP_1_KK",
                "surface": 34,
                "price": 6400000,
                "currency": "CZK",
                "ownership": "OSOBNI",
                "condition": "VERY_GOOD",
                "construction": "MIXED",
                "penb": "B",
                "parking": true,
                "lift": true,
                "gps": {{"lat": 50.1026895, "lng": 14.508437}}
              }},
              "regionTree": [
                {{"name": "Praha"}},
                {{"name": "Praha-Vysočany"}}
              ]
            }}
          }}
        }}
      </script>
    </body></html>
    """


def test_extract_next_data_from_html() -> None:
    next_data = extract_next_data_from_html(make_next_data_html("advert"))
    assert next_data["props"]["pageProps"]["advert"]["address"] == "Poděbradská, Praha - Vysočany"


def test_extract_advert_payload_supports_both_shapes() -> None:
    advert_payload = extract_advert_payload(extract_next_data_from_html(make_next_data_html("advert")))
    orig_payload = extract_advert_payload(extract_next_data_from_html(make_next_data_html("origAdvert")))
    assert advert_payload["advert"]["id"] == "981957"
    assert orig_payload["advert"]["id"] == "981957"


def test_adapter_normalizes_expected_fields() -> None:
    adapter = BezrealitkyAdapter()
    raw_snapshot = RawSnapshot(
        source="bezrealitky",
        source_listing_id="981957",
        observed_at="2026-03-30T09:00:00+00:00",
        listing_url="https://www.bezrealitky.cz/nemovitosti-byty-domy/981957-nabidka-prodej-bytu-podebradska-praha",
        content_hash="abc123",
        html=make_next_data_html("advert"),
        payload=extract_advert_payload(extract_next_data_from_html(make_next_data_html("advert"))),
    )
    normalized = adapter.normalize(raw_snapshot)
    assert normalized.district_prague == "Praha 9"
    assert normalized.property_type == "flat"
    assert normalized.price_per_m2 > 0
