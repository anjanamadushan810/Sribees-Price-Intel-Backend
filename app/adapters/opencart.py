"""OpenCart shops (Arpico at myarpico.com): the HTML search page, one product name at
a time, only for products that are due."""

import re

from selectolax.parser import HTMLParser

from app.adapters.base import Adapter, ListedItem, money

_PRODUCT_ID = re.compile(r"product_id=(\d+)")


def parse_search(html: str) -> list[ListedItem]:
    tree = HTMLParser(html)
    items = []
    for card in tree.css(".product-thumb"):
        link = card.css_first(".product-title a")
        if link is None:
            continue
        href = link.attributes.get("href") or ""
        found = _PRODUCT_ID.search(href)
        new = card.css_first(".price-new") or card.css_first(".price")
        old = card.css_first(".price-old")
        price = money(new.text()) if new else None
        if not found or price is None:
            continue
        out_of_stock = card.css_first(".out-of-stock") is not None
        items.append(ListedItem(
            external_id=found.group(1),
            name=link.text(strip=True),
            price=price,
            mrp=money(old.text()) if old else None,
            url=re.sub(r"&search=[^&]*", "", href.replace("&amp;", "&")),
            in_stock=not out_of_stock,
        ))
    return items


class OpenCartAdapter(Adapter):
    mode = "search"

    async def search(self, query: str) -> list[ListedItem]:
        resp = await self.client.get(
            "/index.php",
            {"route": "product/search", "search": query, "limit": 25, "language": "en-gb"},
        )
        return parse_search(resp.text)
