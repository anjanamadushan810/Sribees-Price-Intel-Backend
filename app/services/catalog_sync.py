"""Copy SRIBEESonline's catalogue and branch prices into our tables.

Online stays the source of truth; we keep only what matching and pricing need.
Cost prices are ours (typed by branch managers) and are never overwritten here.
"""

import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import absolute
from app.core.config import get_settings
from app.models import Branch, BranchProduct, Product
from app.services.normalize import barcode_of, parse_size
from app.services.online_client import OnlineClient, OnlineError

_WEIGHT_UNITS = {"kg": ("g", 1000), "g": ("g", 1), "l": ("ml", 1000), "ml": ("ml", 1)}
LEVELS = {"daily", "weekly", "monthly", "long"}


def _dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def selling_price(effective_price, effective_discount) -> Decimal | None:
    """What the customer pays: the effective price less any effective discount."""
    price = _dec(effective_price)
    if price is None:
        return None
    discount = _dec(effective_discount) or Decimal(0)
    if discount > 0:
        price = price * (1 - discount / 100)
    return price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def product_size(name: str, weight, weight_unit: str | None) -> tuple[Decimal | None, str | None]:
    value, unit = parse_size(name)
    if value is not None:
        return value, unit
    w = _dec(weight)
    base = _WEIGHT_UNITS.get((weight_unit or "kg").lower())
    if w and w > 0 and base:
        return (w * base[1]).quantize(Decimal("0.001")), base[0]
    return None, None


def _photo(images) -> str | None:
    """The primary photo (else the first), as an absolute URL a browser can load."""
    if not images:
        return None
    best = next((i for i in images if i.get("is_primary")), images[0])
    settings = get_settings()
    return absolute(settings.online_media_base or settings.online_api_url, best.get("image_url"))


async def sync(db: AsyncSession, online: OnlineClient) -> dict:
    now = datetime.now(UTC)
    counts = {"branches": 0, "products": 0, "branch_products": 0}

    for b in await online.branches():
        await db.execute(insert(Branch).values(
            branch_id=uuid.UUID(b["branch_id"]), name=b["name"],
            is_active=bool(b.get("is_active", True)), synced_at=now,
        ).on_conflict_do_update(index_elements=["branch_id"], set_={
            "name": b["name"], "is_active": bool(b.get("is_active", True)), "synced_at": now,
        }))
        counts["branches"] += 1

    seen: set[uuid.UUID] = set()
    async for p in online.products():
        pid = uuid.UUID(p["product_id"])
        seen.add(pid)
        size_value, size_unit = product_size(p["name"], p.get("weight"), p.get("weight_unit"))
        level = p.get("price_review_level") if p.get("price_review_level") in LEVELS else "long"
        category = (p.get("category") or {}).get("name") if isinstance(p.get("category"), dict) \
            else None
        values = {
            "image_url": _photo(p.get("images")),
            "name": p["name"][:255], "name_si": (p.get("name_si") or None),
            "sku": p.get("sku"), "barcode": barcode_of(p.get("sku")), "category": category,
            "size_value": size_value, "size_unit": size_unit, "review_level": level,
            "mrp": _dec(p.get("compare_at_price")), "is_active": bool(p.get("is_active", True)),
            "synced_at": now,
        }
        await db.execute(insert(Product).values(product_id=pid, **values)
                         .on_conflict_do_update(index_elements=["product_id"], set_=values))
        counts["products"] += 1
    # Gone from Online: keep the history, stop tracking.
    if seen:
        await db.execute(update(Product).where(Product.product_id.not_in(seen))
                         .values(is_active=False))

    async for row in online.inventory():
        pid = uuid.UUID(row["product_id"])
        if pid not in seen:
            continue
        values = {
            "inventory_id": uuid.UUID(row["inventory_id"]),
            "branch_price": _dec(row.get("branch_price")),
            "global_price": _dec(row.get("global_price")),
            "discount_pct": _dec(row.get("discount_percentage")),
            "is_on_sale": bool(row.get("is_on_sale")),
            "selling_price": selling_price(row.get("effective_price"),
                                           row.get("effective_discount")),
            "is_active": bool(row.get("is_active", True)),
            "synced_at": now,
        }
        await db.execute(insert(BranchProduct).values(
            branch_id=uuid.UUID(row["branch_id"]), product_id=pid, **values,
        ).on_conflict_do_update(index_elements=["branch_id", "product_id"], set_=values))
        counts["branch_products"] += 1
    # A branch row Online no longer lists -- its product deleted (Online deletes outright)
    # or taken off that branch -- is no longer sold there: no comparison, no suggestion.
    # Kept, not deleted, so a product that comes back picks up where it left off.
    # A product Online no longer lists takes its rows with it whenever Online did list
    # products; a row merely missing from the inventory only when the inventory came back
    # at all (an empty answer is more likely a fault than every row gone).
    gone = 0
    if seen:
        gone += (await db.execute(update(BranchProduct).where(
            BranchProduct.is_active.is_(True), BranchProduct.product_id.not_in(seen),
        ).values(is_active=False))).rowcount or 0
    if counts["branch_products"]:
        gone += (await db.execute(update(BranchProduct).where(
            BranchProduct.is_active.is_(True), BranchProduct.synced_at < now,
        ).values(is_active=False))).rowcount or 0
    if gone:
        counts["branch_products_gone"] = gone
    counts["finance_floors"] = await _finance_floors(db, online, now)
    return counts


async def _finance_floors(db: AsyncSession, online: OnlineClient, now: datetime) -> int | None:
    """Read Sribees Finance's minimum price for every active branch product. An item
    Finance has no floor for is cleared, so a stale floor never lingers. An Online that
    does not have the route yet (or any error) leaves the floors as they were."""
    by_branch: dict[uuid.UUID, list[uuid.UUID]] = {}
    for branch_id, product_id in await db.execute(
            select(BranchProduct.branch_id, BranchProduct.product_id)
            .where(BranchProduct.is_active.is_(True))):
        by_branch.setdefault(branch_id, []).append(product_id)
    found: dict[tuple[uuid.UUID, uuid.UUID], Decimal] = {}
    try:
        for branch_id, ids in by_branch.items():
            for i in range(0, len(ids), 200):
                floors = await online.price_floors(str(branch_id), [str(x) for x in ids[i:i + 200]])
                for pid, rows in floors.items():
                    for f in rows:
                        if f.get("variant_id") is None and f.get("min_price") is not None:
                            found[(branch_id, uuid.UUID(pid))] = Decimal(str(f["min_price"]))
    except OnlineError:
        return None
    for (branch_id, product_id), price in found.items():
        await db.execute(update(BranchProduct).where(
            BranchProduct.branch_id == branch_id, BranchProduct.product_id == product_id,
        ).values(finance_min_price=price, finance_floor_at=now))
    for branch_id, ids in by_branch.items():
        stale = [p for p in ids if (branch_id, p) not in found]
        if stale:
            await db.execute(update(BranchProduct).where(
                BranchProduct.branch_id == branch_id, BranchProduct.product_id.in_(stale),
                BranchProduct.finance_min_price.is_not(None),
            ).values(finance_min_price=None, finance_floor_at=now))
    return len(found)
