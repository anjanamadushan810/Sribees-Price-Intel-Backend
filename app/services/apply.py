"""Accept / reject a suggestion. Accept changes the branch price on SRIBEESonline
(VISION.md Q11, Q12): that branch only, never the global price, always audited."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BranchProduct, Product, Suggestion
from app.services import audit, pricing
from app.services.catalog_sync import selling_price
from app.services.errors import Conflict, NotFound
from app.services.online_client import OnlineClient, OnlineError

CENT = Decimal("0.01")


async def _load(db: AsyncSession, suggestion_id: uuid.UUID) -> Suggestion:
    s = await db.get(Suggestion, suggestion_id, with_for_update=True)
    if s is None:
        raise NotFound("suggestion not found")
    if s.status != "pending":
        raise Conflict(f"this suggestion is already {s.status}")
    return s


async def reject(db: AsyncSession, suggestion_id: uuid.UUID, user_id: uuid.UUID,
                 ip: str | None) -> Suggestion:
    s = await _load(db, suggestion_id)
    s.status, s.decided_by, s.decided_at = "rejected", user_id, datetime.now(UTC)
    await audit.record(db, user_id=user_id, action="suggestion.reject", entity="suggestion",
                       entity_id=s.suggestion_id, ip_address=ip)
    return s


async def accept(db: AsyncSession, online: OnlineClient, suggestion_id: uuid.UUID,
                 user_id: uuid.UUID, ip: str | None) -> Suggestion:
    s = await _load(db, suggestion_id)
    bp = await db.get(BranchProduct, (s.branch_id, s.product_id), with_for_update=True)
    now = datetime.now(UTC)
    s.decided_by, s.decided_at = user_id, now

    async def fail(message: str) -> Suggestion:
        s.status, s.apply_error = "failed", message
        await audit.record(db, user_id=user_id, action="price.apply_failed",
                           entity="suggestion", entity_id=s.suggestion_id,
                           after={"error": message}, ip_address=ip)
        return s

    if bp is None or bp.inventory_id is None:
        return await fail("the branch no longer stocks this product on Online")
    try:
        product = await db.get(Product, s.product_id)
        row = await online.inventory_row(str(s.branch_id), str(bp.inventory_id),
                                         product.sku or product.name)
        if row is None:
            return await fail("the branch no longer stocks this product on Online")
        live = selling_price(row.get("effective_price"), row.get("effective_discount"))
        if live != s.current_price:
            return await fail(f"the price on Online changed to Rs {live} after this suggestion; "
                        "it will be re-checked on the next run")
        body = pricing.online_update_for(
            target=s.suggested_price,
            branch_price=Decimal(str(row["branch_price"])) if row.get("branch_price") is not None
            else None,
            global_price=Decimal(str(row.get("global_price") or 0)),
            discount_pct=Decimal(str(row["discount_percentage"]))
            if row.get("discount_percentage") is not None else None,
            is_on_sale=bool(row.get("is_on_sale")),
        )
        after = await online.set_price(str(bp.inventory_id), body)
    except OnlineError as exc:
        return await fail(str(exc))

    charged = selling_price(after.get("effective_price"), after.get("effective_discount"))
    s.status, s.applied_at = "applied", datetime.now(UTC)
    if charged is None or abs(charged - s.suggested_price) > CENT:
        s.apply_error = f"Online now charges Rs {charged}, not Rs {s.suggested_price}"
    bp.selling_price = charged
    bp.branch_price = Decimal(str(after["branch_price"])) if after.get("branch_price") is not None \
        else None
    bp.discount_pct = Decimal(str(after["discount_percentage"])) \
        if after.get("discount_percentage") is not None else None
    bp.synced_at = s.applied_at
    await audit.record(
        db, user_id=user_id, action="price.apply", entity="branch_product",
        entity_id=f"{s.branch_id}:{s.product_id}",
        before={"selling_price": str(s.current_price)},
        after={"selling_price": str(charged), "sent": body,
               "suggestion_id": str(s.suggestion_id),
               "cheapest_competitor_id": s.cheapest_competitor_id},
        ip_address=ip,
    )
    return s
