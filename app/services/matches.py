"""A person's yes / no on a proposed match. Shared by the web admin and the Price app."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CompetitorItem, PriceObservation, ProductMatch
from app.services import audit
from app.services.errors import Conflict

LIVE = ("auto", "confirmed")


async def seed_history(db: AsyncSession, item: CompetitorItem) -> None:
    """A newly confirmed item starts its history with the price it was seen at."""
    if item.last_price is not None:
        db.add(PriceObservation(item_id=item.item_id, price=item.last_price, mrp=item.last_mrp,
                                in_stock=item.in_stock, source="scrape",
                                seen_at=item.last_seen_at))


async def confirm(db: AsyncSession, match_id: uuid.UUID, user_id: uuid.UUID,
                  ip: str | None) -> ProductMatch:
    m = await db.get(ProductMatch, match_id, with_for_update=True)
    if m is None or m.status != "candidate":
        raise Conflict("this match is no longer waiting")
    live = await db.scalar(select(ProductMatch).where(
        ProductMatch.product_id == m.product_id, ProductMatch.competitor_id == m.competitor_id,
        ProductMatch.status.in_(LIVE)))
    if live is not None:
        raise Conflict("this product is already matched there")
    now = datetime.now(UTC)
    m.status, m.decided_by, m.decided_at = "confirmed", user_id, now
    # The other candidates for this product at this shop are now wrong.
    others = await db.scalars(select(ProductMatch).where(
        ProductMatch.product_id == m.product_id, ProductMatch.competitor_id == m.competitor_id,
        ProductMatch.status == "candidate", ProductMatch.match_id != m.match_id))
    for o in others:
        o.status, o.decided_by, o.decided_at = "rejected", user_id, now
    await seed_history(db, m.item)
    await audit.record(db, user_id=user_id, action="match.confirm", entity="match",
                       entity_id=match_id, after={"product_id": str(m.product_id),
                                                  "item": m.item.name},
                       ip_address=ip)
    return m


async def reject(db: AsyncSession, match_id: uuid.UUID, user_id: uuid.UUID,
                 ip: str | None) -> ProductMatch:
    m = await db.get(ProductMatch, match_id, with_for_update=True)
    if m is None or m.status == "rejected":
        raise Conflict("this match is already rejected")
    before = m.status
    m.status, m.decided_by, m.decided_at = "rejected", user_id, datetime.now(UTC)
    await audit.record(db, user_id=user_id, action="match.reject", entity="match",
                       entity_id=match_id, before={"status": before}, ip_address=ip)
    return m
