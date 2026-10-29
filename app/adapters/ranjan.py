"""Ranjan Lanka: the shop's own search box (a form POST to /shop/ with `search`), read
only for products that are due. Its search matches the typed words as one phrase, so
we send the first two words of our name (brand and kind) and fall back to the first.

A result card gives the name and "770.00 LKR" (with a struck-through old price when
there is an offer)."""

import re

from selectolax.parser import HTMLParser

from app.adapters.base import Adapter, ListedItem, absolute, money

_PRODUCT_ID = re.compile(r"product=(\d+)")


def parse_search(page: str, base_url: str) -> list[ListedItem]:
    tree = HTMLParser(page)
    items, seen = [], set()
    for card in tree.css(".product-description"):
        link = card.css_first(".pull-left a")
        # The photo sits in the card's block just before the description.
        holder = card.prev
        while holder is not None and holder.tag != "div":
            holder = holder.prev
        img = holder.css_first("img.image1") if holder is not None else None
        price_el = card.css_first(".title-price")
        if link is None or price_el is None:
            continue
        found = _PRODUCT_ID.search(link.attributes.get("href") or "")
        price = money(price_el.text())
        if not found or price is None or found.group(1) in seen:
            continue
        seen.add(found.group(1))
        old = card.css_first(".line-through")
        mrp = money(old.text()) if old else None
        items.append(ListedItem(
            external_id=found.group(1),
            name=link.text(strip=True),
            price=price,
            mrp=mrp if mrp and mrp > price else None,
            url=f"{base_url}/product/?product={found.group(1)}&view=",
            image=absolute(base_url, img.attributes.get("src") if img else None),
        ))
    return items


class RanjanAdapter(Adapter):
    mode = "search"

    async def search(self, query: str) -> list[ListedItem]:
        words = query.split()
        tries = [" ".join(words[:2])] + ([words[0]] if len(words) > 1 else [])
        for phrase in tries:
            resp = await self.client.post("/shop/", files={"search": (None, phrase)})
            items = parse_search(resp.text, self.client.base_url)
            if items:
                return items
        return []
