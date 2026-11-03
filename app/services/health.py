"""Match health (owner, 2026-11-03): a shop may change or drop an item we matched.

After every good collection run each live match at that shop is compared with what the
shop showed when the match was made:

* a different pack size or barcode, or a title that no longer passes the owner's brand +
  name + size rule (barcode matches: the barcode alone) -> 'changed': its price stops
  counting until a person keeps it or rejects it;
* only the wording moved and the rule still holds -> the new title is remembered, nothing
  else (a shop tidying its titles is not news);
* not listed any more -> after GONE_AFTER runs in a row (a search shop: once the item's
  own name finds nothing either) -> 'gone'; its price stops counting and a replacement
  may be proposed under the usual rules. Listed again later -> live again by itself.
"""

import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CompetitorItem, Product, ProductMatch
from app.services import audit, matcher
from app.services.errors import Conflict
from app.services.normalize import barcode_of, parse

log = logging.getLogger("intel.health")

LIVE = ("auto", "confirmed")
HELD = ("changed", "gone")
GONE_AFTER = 2
# A catalogue run that saw far fewer items than the shop had is a broken read, not a shop
# that emptied its shelves: nothing is marked missing from it.
MIN_SEEN_SHARE = 0.8


def remember(m: ProductMatch, item: CompetitorItem) -> None:
    m.seen_name, m.seen_barcode = item.name, item.barcode
    m.seen_size_value, m.seen_size_unit = item.size_value, item.size_unit


def _size(value, unit) -> str:
    return f"{value.normalize():f} {unit}" if value is not None else "no size"


def what_changed(m: ProductMatch, item: CompetitorItem, product: Product) -> str | None:
    """Why this match needs a person again, or None when it still holds."""
    if (m.seen_size_value is not None and item.size_value is not None
            and (m.seen_size_unit != item.size_unit
                 or abs(m.seen_size_value - item.size_value) > m.seen_size_value / 50)):
        return (f"size {_size(m.seen_size_value, m.seen_size_unit)} -> "
                f"{_size(item.size_value, item.size_unit)}")
    if m.seen_barcode and item.barcode and m.seen_barcode != item.barcode:
        return f"barcode {m.seen_barcode} -> {item.barcode}"
    if m.method == "barcode":
        ours = barcode_of(product.barcode)
        return None if ours and ours == item.barcode else "barcode no longer ours"
    if item.name == m.seen_name:
        return None
    ours = replace(parse(product.name), size_value=product.size_value,
                   size_unit=product.size_unit)
    theirs = replace(parse(item.name), size_value=item.size_value, size_unit=item.size_unit)
    if not matcher.brand_ok(ours, theirs) or matcher.same_size(ours, theirs) is not True:
        return f"title '{m.seen_name}' -> '{item.name}'"[:300]
    return None


async def _hold(db: AsyncSession, m: ProductMatch, status: str, note: str,
                now: datetime) -> None:
    m.held_status, m.status = m.status, status
    m.health_note, m.health_at = note[:300], now
    await audit.record(db, user_id=None, action=f"match.{status}", entity="match",
                       entity_id=m.match_id, before={"status": m.held_status},
                       after={"item": m.item.name, "note": m.health_note})


async def judge(db: AsyncSession, competitor_id: int, started: datetime, *,
                expect_all: bool) -> dict:
    """After a good run at one shop. expect_all: the run read the whole shop (catalogue /
    sitemap), so a matched item it did not see is missing; search shops report misses
    themselves (miss())."""
    now = datetime.now(UTC)
    counts = {"changed": 0, "gone": 0, "back": 0, "renamed": 0}
    rows = (await db.execute(
        select(ProductMatch, Product).join(Product, Product.product_id == ProductMatch.product_id)
        .where(ProductMatch.competitor_id == competitor_id,
               ProductMatch.status.in_(LIVE + ("gone",))))).all()

    if expect_all:
        listed = await db.scalar(select(func.count()).select_from(CompetitorItem).where(
            CompetitorItem.competitor_id == competitor_id))
        seen = await db.scalar(select(func.count()).select_from(CompetitorItem).where(
            CompetitorItem.competitor_id == competitor_id,
            CompetitorItem.last_seen_at >= started))
        if listed and seen < listed * MIN_SEEN_SHARE:
            log.warning("shop %s: saw %s of %s items; no item marked missing",
                        competitor_id, seen, listed)
            expect_all = False

    live_products = {m.product_id for m, _ in rows if m.status in LIVE}
    for m, product in rows:
        item = m.item
        seen_now = item.last_seen_at >= started
        if m.status == "gone":
            # Back on the shelf -- unless a replacement was matched there meanwhile.
            if seen_now and m.product_id not in live_products:
                live_products.add(m.product_id)
                m.status, m.held_status, m.misses = m.held_status, None, 0
                m.health_note, m.health_at = None, now
                counts["back"] += 1
                await audit.record(db, user_id=None, action="match.back", entity="match",
                                   entity_id=m.match_id, after={"item": item.name})
            continue
        if m.seen_name is None:
            remember(m, item)
            continue
        if seen_now:
            m.misses = 0
        elif expect_all:
            m.misses += 1
            if m.misses >= GONE_AFTER:
                await _hold(db, m, "gone", f"not listed since {item.last_seen_at:%Y-%m-%d}",
                            now)
                counts["gone"] += 1
                continue
        note = what_changed(m, item, product)
        if note:
            await _hold(db, m, "changed", note, now)
            counts["changed"] += 1
        elif item.name != m.seen_name or item.barcode != m.seen_barcode:
            remember(m, item)
            counts["renamed"] += 1
    await db.flush()
    return counts


async def miss(db: AsyncSession, m: ProductMatch) -> None:
    """A search shop: our product's search and the item's own name both missed it."""
    await _hold(db, m, "gone",
                f"not found when searched on {datetime.now(UTC):%Y-%m-%d}", datetime.now(UTC))


async def keep(db: AsyncSession, match_id: uuid.UUID, user_id: uuid.UUID,
               ip: str | None) -> ProductMatch:
    """A person looked at a changed match and says it is still our product."""
    m = await db.get(ProductMatch, match_id, with_for_update=True)
    if m is None or m.status != "changed":
        raise Conflict("this match is not waiting for a re-check")
    live = await db.scalar(select(ProductMatch).where(
        ProductMatch.product_id == m.product_id, ProductMatch.competitor_id == m.competitor_id,
        ProductMatch.status.in_(LIVE)))
    if live is not None:
        raise Conflict("this product is already matched there")
    before = m.health_note
    m.status, m.held_status, m.misses = "confirmed", None, 0
    m.decided_by, m.decided_at = user_id, datetime.now(UTC)
    m.health_note, m.health_at = None, m.decided_at
    remember(m, m.item)
    await audit.record(db, user_id=user_id, action="match.keep", entity="match",
                       entity_id=match_id, before={"note": before},
                       after={"item": m.item.name}, ip_address=ip)
    return m
