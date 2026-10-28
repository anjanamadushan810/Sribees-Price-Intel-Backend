"""Keells: the public sitemap lists every product page (name in the URL), and the
public product page shows the price once its own JavaScript has run.

So matching needs one request (the sitemap) and prices are read only for products
already matched and due, one rendered page at a time.
"""

import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qsl, unquote, urlsplit

from app.adapters.base import Adapter, ListedItem, money
from app.adapters.browser import Renderer

PRICE = "#personalization-cart-item-price"
NAME = ".revamp-productNameV2"
CODE = "#personalization-cart-item-code"
ADD = "#personalization-cart-item-add"
_LOC = re.compile(r"<loc>\s*([^<]+?)\s*</loc>")


@dataclass(frozen=True)
class Listed:
    external_id: str
    name: str
    url: str


def slug_name(slug: str) -> str:
    """MD_NECTAR_ORANGE_1L -> Md Nectar Orange 1L (keeps sizes readable for matching)."""
    words = [w for w in re.split(r"[_\s]+", slug) if w]
    return " ".join(w if re.search(r"\d", w) else w.capitalize() for w in words)


def parse_sitemap(xml: str) -> list[Listed]:
    out, seen = [], set()
    for loc in _LOC.findall(xml):
        url = unescape(loc)
        parts = urlsplit(url)
        if not parts.path.lower().endswith("/productdetail"):
            continue
        # "itemcode=118742&J_&_J_BABY_SOAP": the name is everything after the first "&",
        # and may itself contain "&".
        head, _, slug = parts.query.partition("&")
        code = dict(parse_qsl(head)).get("itemcode")
        if not code or not slug or code in seen:
            continue
        seen.add(code)
        out.append(Listed(external_id=code, name=slug_name(unquote(slug)), url=url))
    return out


class KeellsAdapter(Adapter):
    mode = "sitemap"
    min_delay = 5.0
    needs_browser = True

    async def listing(self) -> list[Listed]:
        resp = await self.client.get("/sitemap.xml")
        return parse_sitemap(resp.text)

    async def price(self, renderer: Renderer, item: Listed) -> ListedItem | None:
        """The live price on the public product page; None when the page shows none."""
        path = urlsplit(item.url)
        if not await self.client.allowed(f"{path.path}?{path.query}"):
            return None
        page = await renderer.render(item.url, PRICE)
        try:
            # The price box is drawn with a placeholder first; wait until it belongs to
            # this item (the page writes the item code next to the price when loaded).
            try:
                await page.wait_for_function(
                    "([sel, code]) => document.querySelector(sel)?.textContent.trim() === code",
                    arg=[CODE, item.external_id], timeout=20_000)
            except Exception:
                return None
            price_el = await page.query_selector(PRICE)
            if price_el is None:
                return None
            price = money(await price_el.inner_text())
            if price is None:
                return None
            name_el = await page.query_selector(NAME)
            name = (await name_el.inner_text()).strip() if name_el else item.name
            add = await page.query_selector(ADD)
            in_stock = (add is not None and await add.is_enabled()
                        and "out of stock" not in (await add.inner_text()).lower())
            return ListedItem(external_id=item.external_id, name=name or item.name, price=price,
                              mrp=None, url=item.url, in_stock=in_stock)
        finally:
            await page.close()
