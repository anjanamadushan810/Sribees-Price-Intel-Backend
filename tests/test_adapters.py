"""Adapters read real responses saved on 2026-10-27, and the polite client stops at a block.
No test touches the network."""

import json
import os
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://unused/unused")
os.environ.setdefault("JWT_SECRET", "x" * 40)
os.environ.setdefault("TOTP_ENCRYPTION_KEY", "unused")
os.environ.setdefault("SCRAPE_DELAY_SECONDS", "0")

from app.adapters import base, nexmart, opencart, shopify  # noqa: E402
from app.adapters.base import PoliteClient, SourceBlocked, SourceFailed  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def test_nexmart_page():
    items = nexmart.parse_page(json.loads((FIX / "nexmart_page.json").read_text("utf-8")),
                               "https://www.nexmart.lk")
    assert items, "fixture has items"
    first = items[0]
    assert first.external_id and first.name and first.price > 0
    assert any(i.mrp for i in items)


def test_shopify_page_takes_lowest_available_variant():
    items = shopify.parse_page(json.loads((FIX / "spar_page.json").read_text("utf-8")),
                               "https://spar2u.lk")
    assert items
    assert items[0].url.startswith("https://spar2u.lk/products/")
    data = {"products": [{"id": 1, "title": "X 1kg", "handle": "x", "variants": [
        {"price": "300.00", "available": False},
        {"price": "280.00", "available": True},
        {"price": "290.00", "available": True},
    ]}]}
    (item,) = shopify.parse_page(data, "https://s")
    assert item.price == D("280.00") and item.in_stock


def test_opencart_search_page():
    items = opencart.parse_search((FIX / "arpico_search.html").read_text("utf-8"))
    assert len(items) >= 2
    first = items[0]
    assert first.external_id == "6242"
    assert first.name == "Maliban Biscuit Cheese Bits Packet 170G"
    assert first.price == D("430.00")
    assert "search=" not in first.url


def test_money_reads_shop_formats():
    assert base.money("₨ 1,250.00") == D("1250.00")
    assert base.money(37) == D("37.00")
    assert base.money("0") is None and base.money(None) is None and base.money("n/a") is None


def _client(handler) -> PoliteClient:
    return PoliteClient("https://shop.test", transport=httpx.MockTransport(handler))


async def test_robots_disallow_stops_before_fetching():
    fetched = []

    def handler(request: httpx.Request):
        fetched.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /api/\n")
        return httpx.Response(200, json={})

    async with _client(handler) as client:
        with pytest.raises(SourceBlocked):
            await client.get("/api/products")
    assert fetched == ["/robots.txt"]


@pytest.mark.parametrize("status", [403, 429])
async def test_block_status_is_not_retried(status):
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(status)

    async with _client(handler) as client:
        with pytest.raises(SourceBlocked):
            await client.get("/products.json")
    assert calls.count("/products.json") == 1


async def test_short_retry_after_is_honoured_once():
    calls = []

    def handler(request: httpx.Request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as client:
        resp = await client.get("/products.json")
    assert resp.json() == {"ok": True} and len(calls) == 2


async def test_second_429_is_a_block():
    def handler(request: httpx.Request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(429, headers={"retry-after": "0"})

    async with _client(handler) as client:
        with pytest.raises(SourceBlocked):
            await client.get("/products.json")


async def test_bot_challenge_page_is_a_block():
    def handler(request: httpx.Request):
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text='<html><script src="/__zenedge/assets/f.js"></script></html>')

    async with _client(handler) as client:
        with pytest.raises(SourceBlocked):
            await client.get("/")


async def test_unexpected_status_fails_without_block():
    def handler(request: httpx.Request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(404)

    async with _client(handler) as client:
        with pytest.raises(SourceFailed):
            await client.get("/gone")


def test_keells_sitemap_gives_codes_and_names():
    from app.adapters import keells

    items = keells.parse_sitemap((FIX / "keells_sitemap.xml").read_text("utf-8"))
    assert len(items) == 5
    first = items[0]
    assert first.external_id == "112840"
    assert first.name == "Md Nectar Orange 1L"
    assert "&amp;" not in first.url and first.url.startswith("https://keellssuper.com/productDetail")


def test_keells_slug_keeps_sizes():
    from app.adapters.keells import slug_name
    from app.services.normalize import parse

    assert slug_name("ANCHOR_MILK_POWDER_400G") == "Anchor Milk Powder 400G"
    assert parse(slug_name("ANCHOR_MILK_POWDER_400G")).size_value == D("400.000")


def test_keells_name_may_contain_ampersand():
    from app.adapters import keells

    xml = ("<urlset><url><loc>https://keellssuper.com/productDetail?itemcode=118742"
           "&amp;J_&amp;_J_BABY_MILK_SOAP_100G</loc></url></urlset>")
    (item,) = keells.parse_sitemap(xml)
    assert item.external_id == "118742" and item.name == "J & J Baby Milk Soap 100G"


def test_cargills_card_reads_price_mrp_and_pack():
    from app.adapters.cargills import parse_card

    item = parse_card({"name": "Ambewela Non Fat Milk", "pack": "1.00 l",
                       "prices": "Rs. 570.00 MRP: Rs. 600.00",
                       "href": "/ProductDetails/Dairy/Ambewela-Non-Fat-Milk?ID=ogz9ml",
                       "out": False},
                      "https://cargillsonline.com")
    assert item.price == D("570.00") and item.mrp == D("600.00")
    assert item.url == "https://cargillsonline.com/ProductDetails/Dairy/Ambewela-Non-Fat-Milk?ID=ogz9ml"
    from app.services.normalize import parse
    assert parse(item.name, item.pack).size_value == D("1000.000")
    big = parse_card({"name": "Diamond Milk Powder", "pack": "400.00 g",
                      "prices": "Rs. 1,200.50 MRP: Rs. 1225.00", "href": "/ProductDetails/x?ID=1"},
                     "https://cargillsonline.com")
    assert big.price == D("1200.50")
    assert parse_card({"name": "x", "prices": "", "href": "/p"}, "https://c") is None


def test_glomark_sitemap_and_product_json_ld():
    from app.adapters import glomark

    items = glomark.parse_sitemap((FIX / "glomark_sitemap.xml").read_text("utf-8"),
                                  "https://glomark.lk")
    assert len(items) == 4 and all(i.url.startswith("https://glomark.lk/") for i in items)
    stevia = glomark.Listed("16832", "x", "https://glomark.lk/x/p/16832")
    item = glomark.parse_product((FIX / "glomark_product.html").read_text("utf-8"), stevia)
    assert item.price == D("250.00") and item.name == "SWEET'N SWEETNER STEVIA 50 SACHETS"
    assert item.brand == "SWEET'N LOW" and item.in_stock is False
    assert glomark.slug_name("sweet%2527n-sweetner-stevia-50-sachets") == \
        "Sweet'n Sweetner Stevia 50 Sachets"


def test_real_page_with_vendor_script_is_not_a_challenge():
    from app.adapters.base import _looks_challenged

    page = '<html><script src="/__zenedge/assets/f.js"></script>' + "x" * 10_000
    assert not _looks_challenged(page)
    assert _looks_challenged('<html><script src="/__zenedge/assets/f.js"></script></html>')
    assert _looks_challenged("<title>Just a moment...</title>")


def test_woocommerce_page_reads_minor_units_and_barcode_sku():
    from app.adapters import woocommerce
    from app.services.normalize import barcode_of

    items = woocommerce.parse_page(
        json.loads((FIX / "woocommerce_page.json").read_text("utf-8")), "https://onlinekade.lk")
    first = items[0]
    assert first.price == D("450.00") and first.mrp is None  # regular == price
    assert barcode_of(first.barcode) == "4791010001434"
    assert first.url.startswith("https://onlinekade.lk/product/")
    sale = woocommerce.parse_page([{"id": 1, "name": "X &amp; Y 1kg", "type": "simple",
                                    "prices": {"price": "9000", "regular_price": "10000",
                                               "currency_minor_unit": 2},
                                    "is_in_stock": False}], "https://s")[0]
    assert sale.name == "X & Y 1kg" and sale.price == D("90.00") and sale.mrp == D("100.00")
    assert sale.in_stock is False


def test_ranjan_search_cards():
    from app.adapters import ranjan

    items = ranjan.parse_search((FIX / "ranjan_search.html").read_text("utf-8"),
                                "https://ranjanlanka.lk")
    assert len(items) == 3
    assert all(i.price > 0 and i.name for i in items)
    assert items[0].url.startswith("https://ranjanlanka.lk/product/?product=")


async def test_post_obeys_the_same_rules():
    def handler(request: httpx.Request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /shop/\n")
        return httpx.Response(200, text="ok")

    async with _client(handler) as client:
        with pytest.raises(SourceBlocked):
            await client.post("/shop/", files={"search": (None, "milk")})
