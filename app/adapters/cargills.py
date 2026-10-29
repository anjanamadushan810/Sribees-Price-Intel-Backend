"""Cargills: the public search page (/product/<words>?PS=<words>), rendered in a browser
because its AngularJS fills the product cards in. Only products that are due by
price_review_level are searched, one page per 5 s.

A card gives name, pack size ("400.00 g"), price and MRP ("Rs. 570.00 MRP: Rs. 600.00").
"""

import re
from urllib.parse import quote, urlsplit

from app.adapters.base import Adapter, ListedItem, absolute, money
from app.adapters.browser import Renderer

CARD = ".cargillProdNeed"
_PRICES = re.compile(r"Rs\.?\s*([\d,]+(?:\.\d+)?)")

# One object per card, read in the page so a re-render cannot mix two cards up.
_READ_CARDS = """() => [...document.querySelectorAll('.cargillProdNeed')].map(c => ({
  name: c.querySelector('p[title]')?.getAttribute('title') || '',
  pack: c.querySelector('.dropbtn1')?.textContent.trim() || '',
  prices: c.querySelector('.strike1 h4')?.textContent.trim() || '',
  href: c.querySelector('a[href*="/ProductDetails/"]')?.getAttribute('href') || '',
  image: (c.querySelector('img[ng-src*="/VendorItems/"]')?.getAttribute('ng-src')
          || c.querySelector('img[src*="/VendorItems/"]')?.getAttribute('src') || ''),
  out: c.innerText.toLowerCase().includes('out of stock'),
}))"""


def parse_card(card: dict, base_url: str) -> ListedItem | None:
    found = _PRICES.findall(card.get("prices") or "")
    price = money(found[0]) if found else None
    name, href = (card.get("name") or "").strip(), card.get("href") or ""
    if price is None or not name or not href:
        return None
    mrp = money(found[1]) if len(found) > 1 else None
    parts = urlsplit(href)
    return ListedItem(
        external_id=f"{parts.path}?{parts.query}"[:120],
        name=name,
        price=price,
        mrp=mrp if mrp and mrp >= price else None,
        pack=(card.get("pack") or None),
        url=f"{base_url}{href}" if href.startswith("/") else href,
        in_stock=not card.get("out"),
        image=absolute(base_url, card.get("image")),
    )


class CargillsAdapter(Adapter):
    mode = "search"
    min_delay = 5.0

    async def __aenter__(self) -> "CargillsAdapter":
        self.renderer = await Renderer(self.min_delay).__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.renderer.__aexit__(*exc)

    async def search(self, query: str) -> list[ListedItem]:
        words = quote(query.strip())
        path = f"/product/{words}"
        if not await self.client.allowed(path):
            return []
        page = await self.renderer.render(f"{self.client.base_url}{path}?PS={words}", CARD)
        try:
            cards = await page.evaluate(_READ_CARDS)
        finally:
            await page.close()
        items = [parse_card(c, self.client.base_url) for c in cards]
        return [i for i in items if i is not None]
