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
