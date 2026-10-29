"""Glomark: the public sitemap lists every product page (name in the URL), and each
product page carries schema.org Product JSON-LD with price, brand and availability.
No browser needed. Names come from the sitemap (one request) for matching; product
pages are read only for matched products that are due.
"""

import html
import json
import re
from urllib.parse import unquote, urlsplit

from app.adapters.base import Adapter, ListedItem, SourceFailed, absolute, money
from app.adapters.keells import Listed

_LOC = re.compile(r"<loc>\s*([^<]+?)\s*</loc>")
_PRODUCT = re.compile(r"^/(?P<slug>[^/]+)/p/(?P<id>\d+)/?$")
_LD = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)


def slug_name(slug: str) -> str:
    """sweet%2527n-sweetner-stevia-50-sachets -> Sweet'n Sweetner Stevia 50 Sachets."""
    text = unquote(unquote(slug)).replace("-", " ")
    return " ".join(w if re.search(r"\d", w) else w.capitalize() for w in text.split())


def parse_sitemap(xml: str, base_url: str) -> list[Listed]:
    out, seen = [], set()
    for loc in _LOC.findall(xml):
        m = _PRODUCT.match(urlsplit(html.unescape(loc)).path)
        if not m or m["id"] in seen:
            continue
        seen.add(m["id"])
        out.append(Listed(external_id=m["id"], name=slug_name(m["slug"]),
                          url=f"{base_url}/{m['slug']}/p/{m['id']}"))
    return out


def parse_product(page: str, item: Listed) -> ListedItem | None:
    for block in _LD.findall(page):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("@type") != "Product":
            continue
        offers = data.get("offers") or []
        offer = offers[0] if isinstance(offers, list) and offers else offers
        if not isinstance(offer, dict):
            return None
        price = money(offer.get("price"))
        if price is None:
            return None
        brand = data.get("brand")
        brand = brand.get("name") if isinstance(brand, dict) else brand
        return ListedItem(
            external_id=item.external_id,
            name=html.unescape(data.get("name") or item.name).strip(),
            price=price,
            brand=html.unescape(brand) if brand else None,
            url=item.url,
            in_stock="instock" in str(offer.get("availability", "")).lower(),
            image=absolute(item.url, data["image"] if isinstance(data.get("image"), str) else None),
        )
    return None


class GlomarkAdapter(Adapter):
    mode = "sitemap"
    min_delay = 3.0
    needs_browser = False

    async def listing(self) -> list[Listed]:
        resp = await self.client.get("/sitemap.xml")
        return parse_sitemap(resp.text, self.client.base_url)

    async def price(self, _renderer, item: Listed) -> ListedItem | None:
        try:
            resp = await self.client.get(urlsplit(item.url).path)
        except SourceFailed:
            return None  # page gone: treat as not sold there
        return parse_product(resp.text, item)
