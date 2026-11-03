"""A person pastes a shop's product page for one of our products (the Match Queue's
"Add manual match", owner 2026-11-03). The page's item becomes a 'candidate' card in the
queue, to be confirmed there like any other.

Only the shop's own site is ever read, through the same polite client as the nightly run
(robots.txt, pace, no way past a bot check): the pasted address gives a path, never a host.
An item the nightly run already stored is used as it is, without asking the shop again.
"""

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import build
from app.adapters.base import PoliteClient, SourceBlocked, SourceFailed
from app.models import Competitor, CompetitorItem, Product, ProductMatch
from app.services import audit, collect, matcher
from app.services.errors import Conflict, IntelError, NotFound
from app.services.health import LIVE
from app.services.normalize import parse


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def page_path(competitor: Competitor, url: str) -> str:
    """The path (and query) of a page on this shop's own site; anything else is refused."""
    url = url.strip()
    if "://" not in url:
        url = f"https://{url}"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise IntelError("that is not a web address")
    if _host(url) != _host(competitor.base_url or ""):
        raise IntelError(f"that link is not on {competitor.name}'s website")
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


async def _stored(db: AsyncSession, competitor: Competitor, adapter, path: str):
    conds = []
    if (ext := adapter.external_id(path)) is not None:
        conds.append(CompetitorItem.external_id == ext)
    page = f"{competitor.base_url.rstrip('/')}{adapter.canonical(path)}".rstrip("/")
    conds.append(func.rtrim(CompetitorItem.url, "/") == page)
    return await db.scalar(select(CompetitorItem).where(
        CompetitorItem.competitor_id == competitor.competitor_id, or_(*conds)).limit(1))


async def find_item(db: AsyncSession, competitor: Competitor, url: str,
                    transport=None) -> CompetitorItem:
    path = page_path(competitor, url)
    async with PoliteClient(competitor.base_url, transport=transport) as client:
        adapter = build(competitor.adapter or "", client)
        if adapter is None:
            raise IntelError("this shop is not read automatically")
        item = await _stored(db, competitor, adapter, path)
        if item is not None:
            return item
        if not adapter.reads_pages:
            raise IntelError(f"{competitor.name}'s pages cannot be read from a link yet; "
                             "this item has not been seen in its search results either")
        try:
            async with adapter:
                listed = await adapter.read_page(path)
        except SourceBlocked as exc:
            raise Conflict(f"{competitor.name} did not let us read that page ({exc})") from exc
        except SourceFailed as exc:
            raise IntelError(f"could not read that page: {exc}") from exc
    if listed is None:
        raise IntelError("no product with a price was found on that page")
    ids = await collect._store(db, competitor.competitor_id, [listed], datetime.now(UTC))
    return await db.get(CompetitorItem, ids[listed.external_id])


def flags(product: Product, item: CompetitorItem) -> list[str]:
    """What a person should look at twice: the owner's brand + size rule, shown as a quiet
    note on the card (owner 2026-11-03: a person may still confirm)."""
    ours = replace(parse(product.name), size_value=product.size_value,
                   size_unit=product.size_unit)
    theirs = replace(parse(item.name), size_value=item.size_value, size_unit=item.size_unit)
    out = []
    size = matcher.same_size(ours, theirs)
    if size is False:
        out.append("size differs")
    elif size is None:
        out.append("size unknown")
    if not matcher.brand_ok(ours, theirs):
        out.append("brand differs")
    return out


async def add(db: AsyncSession, product_id: uuid.UUID, competitor_id: int, url: str,
              user_id: uuid.UUID, ip: str | None, transport=None) -> ProductMatch:
    product = await db.get(Product, product_id)
    competitor = await db.get(Competitor, competitor_id)
    if product is None or not product.is_active or competitor is None:
        raise NotFound("product or shop not found")
    if competitor.kind != "scraped" or not competitor.base_url:
        raise IntelError("this shop's prices are entered by hand (Shelf prices)")
    item = await find_item(db, competitor, url, transport)
    live = await db.scalar(select(ProductMatch).where(
        ProductMatch.product_id == product_id, ProductMatch.competitor_id == competitor_id,
        ProductMatch.status.in_(LIVE)))
    if live is not None:
        if live.item_id == item.item_id:
            raise Conflict("this product is already matched to that item")
        raise Conflict(f"this product is already matched there ('{live.item.name}'); "
                       "reject that match first")
    m = await db.scalar(select(ProductMatch).where(
        ProductMatch.product_id == product_id, ProductMatch.item_id == item.item_id))
    if m is not None and m.status in ("changed", "gone"):
        raise Conflict("this pair is waiting in Match health")
    if m is None:
        m = ProductMatch(product_id=product_id, item_id=item.item_id,
                         competitor_id=competitor_id, score=0)
        db.add(m)
    # A pair once rejected may come back: a person asked for it by name.
    m.status, m.method, m.decided_by, m.decided_at = "candidate", "manual", None, None
    await db.flush()
    await audit.record(db, user_id=user_id, action="match.add", entity="match",
                       entity_id=m.match_id, after={"product_id": str(product_id),
                                                    "item": item.name, "url": url[:300]},
                       ip_address=ip)
    return m
