"""Turn fresh competitor prices into pending suggestions, one per branch and product."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BranchProduct, CompetitorItem, Product, ProductMatch, Setting, Suggestion
from app.services import pricing, schedule, unitprice

DEFAULT_PRICING = {"min_margin_pct": 5,
                   "stale_days": {"daily": 2, "weekly": 9, "monthly": 35, "long": 70}}


async def pricing_settings(db: AsyncSession) -> dict:
    row = await db.get(Setting, "pricing")
    return {**DEFAULT_PRICING, **(row.value if row else {})}


async def offers_by_product(db: AsyncSession, now: datetime, stale_days: dict
                            ) -> dict:
    """Fresh, in-stock prices of every live match, keyed by our product."""
    rows = await db.execute(
        select(CompetitorItem, Product)
        .join(ProductMatch, CompetitorItem.item_id == ProductMatch.item_id)
        .join(Product, Product.product_id == ProductMatch.product_id)
        .where(ProductMatch.status.in_(("auto", "confirmed")),
               CompetitorItem.in_stock.is_(True), CompetitorItem.last_price.is_not(None))
    )
    offers: dict = {}
    for item, prod in rows:
        # A bulk price counts at our pack size (owner 2026-11-04), or not at all.
        price = unitprice.for_item(item, prod)
        if price is not None and schedule.is_fresh(prod.review_level, item.last_seen_at, now,
                                                   stale_days):
            offers.setdefault(prod.product_id, []).append(
                pricing.Offer(item.competitor_id, price))
    return offers


async def refresh(db: AsyncSession) -> dict:
    now = datetime.now(UTC)
    settings = await pricing_settings(db)
    margin = Decimal(str(settings["min_margin_pct"]))
    offers = await offers_by_product(db, now, settings["stale_days"])
    pending = {(s.branch_id, s.product_id): s for s in await db.scalars(
        select(Suggestion).where(Suggestion.status == "pending"))}
    counts = {"suggest": 0, "cheaper": 0, "cost_missing": 0, "at_floor": 0, "no_data": 0,
              "check": 0, "new": 0}

    rows = await db.scalars(
        select(BranchProduct)
        .join(Product, Product.product_id == BranchProduct.product_id)
        .where(BranchProduct.is_active.is_(True), BranchProduct.selling_price.is_not(None),
               Product.is_active.is_(True)))
    judged: set = set()
    for bp in rows:
        judged.add((bp.branch_id, bp.product_id))
        verdict = pricing.decide(current=bp.selling_price, cost=bp.cost_price,
                                 offers=offers.get(bp.product_id, []), min_margin_pct=margin,
                                 finance_floor=bp.finance_min_price)
        counts[verdict.outcome] += 1
        old = pending.get((bp.branch_id, bp.product_id))
        if verdict.outcome != "suggest":
            if old is not None:
                old.status = "superseded"
            continue
        if (old is not None and old.suggested_price == verdict.suggested_price
                and old.current_price == bp.selling_price):
            continue
        if old is not None:
            old.status = "superseded"
            await db.flush()
        db.add(Suggestion(
            branch_id=bp.branch_id, product_id=bp.product_id, current_price=bp.selling_price,
            suggested_price=verdict.suggested_price, cheapest_price=verdict.cheapest.price,
            cheapest_competitor_id=verdict.cheapest.competitor_id, cost_price=bp.cost_price,
            floor_price=verdict.floor_price, reason=verdict.reason,
        ))
        counts["new"] += 1
    # A pending suggestion for a product no longer sold (deleted on Online, off the branch)
    # could only fail when accepted.
    for key, old in pending.items():
        if key not in judged:
            old.status = "superseded"
    await db.flush()
    return counts


async def supersede_for(db: AsyncSession, branch_id, product_id) -> None:
    """A cost change makes the old suggestion's floor wrong."""
    await db.execute(update(Suggestion).where(
        Suggestion.branch_id == branch_id, Suggestion.product_id == product_id,
        Suggestion.status == "pending").values(status="superseded"))
