"""Any Shopify shop (SPAR): the public /products.json, 250 products per page.

SPAR lists one variant per store with the same price, so the lowest available
variant price is the shop's price."""

import re
from collections.abc import AsyncIterator

from app.adapters.base import Adapter, ListedItem, SourceFailed, absolute, money
from app.core.config import get_settings

PAGE_SIZE = 250
_HANDLE = re.compile(r"/products/([^/?#.]+)")


def parse_page(data: dict, base_url: str) -> list[ListedItem]:
    items = []
    for product in data.get("products") or []:
        variants = product.get("variants") or []
        available = [v for v in variants if v.get("available")]
        pool = available or variants
        priced = [(money(v.get("price")), v) for v in pool]
        priced = [(p, v) for p, v in priced if p is not None]
        if not priced or not product.get("title"):
            continue
        price, variant = min(priced, key=lambda pv: pv[0])
        items.append(ListedItem(
            external_id=str(product["id"]),
            name=str(product["title"]).strip(),
            price=price,
            mrp=money(variant.get("compare_at_price")),
            brand=None,  # SPAR puts its own name in "vendor"
            barcode=(variant.get("barcode") or None),
            url=f"{base_url}/products/{product.get('handle')}",
            in_stock=bool(available),
            image=absolute(base_url, ((product.get("images") or [{}])[0] or {}).get("src")),
        ))
    return items


class ShopifyAdapter(Adapter):
    mode = "catalogue"
    min_delay = 5.0  # Shopify answers 429 to a 2-second pace
    reads_pages = True

    def canonical(self, path: str) -> str:
        # /collections/flour/products/x?variant=1 is the same page as /products/x
        found = _HANDLE.search(path)
        return f"/products/{found.group(1)}" if found else path

    async def read_page(self, path: str) -> ListedItem | None:
        found = _HANDLE.search(path)
        if not found:
            return None
        resp = await self.client.get(f"/products/{found.group(1)}.json")
        try:
            product = resp.json().get("product")
        except (ValueError, AttributeError) as exc:
            raise SourceFailed("Shopify returned something that is not JSON") from exc
        items = parse_page({"products": [product] if product else []}, self.client.base_url)
        return items[0] if items else None

    async def catalogue(self) -> AsyncIterator[ListedItem]:
        limit = get_settings().scrape_max_pages
        for page in range(1, limit + 1):
            resp = await self.client.get("/products.json", {"limit": PAGE_SIZE, "page": page})
            try:
                data = resp.json()
            except ValueError as exc:
                raise SourceFailed("Shopify returned something that is not JSON") from exc
            items = parse_page(data, self.client.base_url)
            if not data.get("products"):
                return
            for item in items:
                yield item
