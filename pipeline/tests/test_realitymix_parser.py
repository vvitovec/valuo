from praha_predictor.schemas import RawSnapshot
from praha_predictor.sources.realitymix import (
    RealityMixAdapter,
    _extract_floor_area,
    _extract_price,
    _parse_property_type,
    _parse_listing_urls,
    _parse_total_results,
)


def test_realitymix_parser_extracts_pagination_and_detail_urls() -> None:
    html = """
    <html>
      <body>
        <div class="paginator__total">Zobrazujeme výsledky 1-20 z celkem 2878 nalezených</div>
        <a href="https://realitymix.cz/detail/praha/prodej-bytu-2-kk-43-m-lipnicka-praha-kyje-8549299.html">detail</a>
        <a href="https://realitymix.cz/detail/praha/prodej-bytu-2-kk-43-m-lipnicka-praha-kyje-8549299.html#kontakt">kontakt</a>
      </body>
    </html>
    """
    assert _parse_total_results(html) == 2878
    assert _parse_listing_urls(html) == [
        "https://realitymix.cz/detail/praha/prodej-bytu-2-kk-43-m-lipnicka-praha-kyje-8549299.html"
    ]


def test_realitymix_adapter_normalizes_nearby_listing() -> None:
    adapter = RealityMixAdapter()
    raw_snapshot = RawSnapshot(
        source="realitymix",
        source_listing_id="8549299",
        observed_at="2026-03-30T09:00:00+00:00",
        listing_url="https://realitymix.cz/detail/stredocesky/prodej-domu-5-kk-150-m-ricany-8549299.html",
        content_hash="abc123",
        html="<html></html>",
        payload={
            "title_text": "Prodej domu 5+kk, 150 m², Říčany",
            "address_text": "Říčany",
            "property_type": "house",
            "offer_type": "sale",
            "disposition": "5+kk",
            "floor_area_m2": 150.0,
            "price_czk": 15500000.0,
            "ownership": None,
            "condition": "good",
            "construction": "brick",
            "energy_label": "c",
            "land_area_m2": 420.0,
            "floor_no": None,
            "total_floors": 2.0,
            "has_elevator": False,
            "has_parking": True,
            "has_cellar": None,
            "has_balcony_or_loggia": True,
            "lat": 49.9912,
            "lng": 14.6543,
        },
    )
    normalized = adapter.normalize(raw_snapshot)
    assert normalized.district_prague == "Praha okolí"
    assert normalized.property_type == "house"
    assert round(normalized.price_per_m2) == 103333


def test_realitymix_property_type_handles_mezonet_and_jednotka() -> None:
    assert _parse_property_type("Světlý designový mezonet poblíž Smíchovské náplavky") == "flat"
    assert _parse_property_type("Prodej jednotky 1+kk, 38 m2, Praha 3") == "flat"
    assert _parse_property_type("Prodej byty 3+1/L, 82 m2 - Praha - Bohnice") == "flat"
    assert _parse_property_type("Prodej domu/vily, 125 m²", "Prodej řadového domu 4+kk, zahrada, Praha 9 - Koloděje") == "house"


def test_realitymix_extracts_price_and_area_from_html_fallbacks() -> None:
    html = """
    <table>
      <tr class="advert-description__short-props-price">
        <td>Cena:</td>
        <td>22 890 000 Kč <button>Nabídněte cenu</button></td>
      </tr>
    </table>
    """
    short_props = {}
    detail_items = {"Užitná plocha": "320 m²"}
    assert _extract_price(short_props, html) == 22_890_000.0
    assert _extract_floor_area("Praha 6 - Řepy, zařízený rodinný dům", short_props, detail_items) == 320.0
