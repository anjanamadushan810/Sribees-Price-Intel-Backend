"""Nexmart: public JSON at /api/products (60 items per page, ~65 pages)."""

from collections.abc import AsyncIterator

from app.adapters.base import Adapter, ListedItem, SourceFailed, money
from app.core.config import get_settings

PAGE_SIZE = 60


def parse_page(data: dict, base_url: str) -> list[ListedItem]:
    items = []
    for row in data.get("items") or []:
        if not row.get("enabled", True):
            continue
        price = money(row.get("price"))
        if price is None or not row.get("sku") or not row.get("name"):
            continue
        items.append(ListedItem(
            external_id=str(row["sku"]),
            name=str(row["name"]).strip(),
            price=price,
            mrp=money(row.get("mrp")),
            brand=(row.get("brand") or "").strip() or None,
            pack=(row.get("pack") or "").strip() or None,
            url=base_url,  # Nexmart has no per-product page
            in_stock=bool(row.get("inStock", True)),
        ))
    return items


class NexmartAdapter(Adapter):
    mode = "catalogue"

    async def catalogue(self) -> AsyncIterator[ListedItem]:
        page, pages = 1, 1
        limit = get_settings().scrape_max_pages
        while page <= pages and page <= limit:
            resp = await self.client.get("/api/products", {"page": page, "limit": PAGE_SIZE})
            try:
                data = resp.json()
                pages = int(data.get("pages") or 1)
            except ValueError as exc:
                raise SourceFailed("Nexmart returned something that is not JSON") from exc
            for item in parse_page(data, self.client.base_url):
                yield item
            page += 1
