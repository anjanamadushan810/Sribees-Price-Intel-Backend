"""Any WooCommerce shop (OnlineKade): the public Store API,
/wp-json/wc/store/v1/products, 100 products per page.

OnlineKade's SKU is the pack's EAN barcode, so most of its products match ours by
barcode with no person involved."""

import html
from collections.abc import AsyncIterator
from decimal import Decimal

from app.adapters.base import Adapter, ListedItem, SourceFailed, absolute
from app.core.config import get_settings

PAGE_SIZE = 100
PATH = "/wp-json/wc/store/v1/products"


def _amount(prices: dict, key: str) -> Decimal | None:
    """Store API amounts are integers in minor units ("45000" with minor unit 2)."""
    raw = prices.get(key)
    if raw in (None, ""):
        return None
    try:
        value = Decimal(str(raw)) / (Decimal(10) ** int(prices.get("currency_minor_unit", 2)))
    except (ArithmeticError, ValueError):
        return None
    return value.quantize(Decimal("0.01")) if value > 0 else None


def parse_page(data: list, base_url: str) -> list[ListedItem]:
    items = []
    for product in data or []:
        prices = product.get("prices") or {}
        price = _amount(prices, "price")
        if price is None or not product.get("name") or product.get("type") == "variable":
            continue
        regular = _amount(prices, "regular_price")
        items.append(ListedItem(
            external_id=str(product["id"]),
            name=html.unescape(str(product["name"])).strip(),
            price=price,
            mrp=regular if regular and regular > price else None,
            barcode=(str(product.get("sku") or "").strip() or None),
            url=product.get("permalink") or base_url,
            in_stock=bool(product.get("is_in_stock", True)),
            image=absolute(base_url,
                           ((product.get("images") or [{}])[0] or {}).get("thumbnail")),
        ))
    return items


class WooCommerceAdapter(Adapter):
    mode = "catalogue"

    async def catalogue(self) -> AsyncIterator[ListedItem]:
        pages, page = 1, 1
        limit = get_settings().scrape_max_pages
        while page <= pages and page <= limit:
            resp = await self.client.get(PATH, {"per_page": PAGE_SIZE, "page": page})
            try:
                data = resp.json()
            except ValueError as exc:
                raise SourceFailed("WooCommerce returned something that is not JSON") from exc
            pages = int(resp.headers.get("x-wp-totalpages") or 1)
            for item in parse_page(data, self.client.base_url):
                yield item
            page += 1
