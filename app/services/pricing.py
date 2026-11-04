"""The owner's price rule (VISION.md Q5): match the cheapest competitor, but never
go below the floor. Never suggests a rise; never suggests without a floor.

The floor is Sribees Finance's minimum price when it has one (owner 2026-11-04: costs
live in Finance -- latest purchase cost with transport, bags and labels, margin,
cashback and referral); otherwise the hand-typed cost + min_margin_pct.
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

RUPEE = Decimal("1")
# A shop price under half of ours is more likely a wrong match or a wrong price on our
# side (a placeholder, a different pack) than a real bargain: never chase it blindly.
OUTLIER_SHARE = Decimal("0.5")


@dataclass(frozen=True)
class Offer:
    competitor_id: int
    price: Decimal


@dataclass(frozen=True)
class Verdict:
    # suggest | cheaper (we already win) | cost_missing | at_floor | no_data
    # | check (the only cheaper prices are implausibly low: a person should look)
    outcome: str
    suggested_price: Decimal | None = None
    floor_price: Decimal | None = None
    cheapest: Offer | None = None
    reason: str | None = None  # match_cheapest | floor


def floor_price(cost: Decimal, min_margin_pct: Decimal) -> Decimal:
    """Cost plus margin, rounded UP to a whole rupee so rounding never eats the margin."""
    return (cost * (1 + min_margin_pct / 100)).quantize(RUPEE, rounding=ROUND_CEILING)


def decide(
    *,
    current: Decimal,
    cost: Decimal | None,
    offers: list[Offer],
    min_margin_pct: Decimal,
    finance_floor: Decimal | None = None,
) -> Verdict:
    if not offers:
        return Verdict("no_data")
    plausible = [o for o in offers if o.price >= current * OUTLIER_SHARE]
    if not plausible:
        return Verdict("check", cheapest=min(offers, key=lambda o: (o.price, o.competitor_id)))
    cheapest = min(plausible, key=lambda o: (o.price, o.competitor_id))
    if current <= cheapest.price:
        return Verdict("cheaper", cheapest=cheapest)
    if finance_floor is not None:
        floor = finance_floor.quantize(RUPEE, rounding=ROUND_CEILING)
    elif cost is None:
        return Verdict("cost_missing", cheapest=cheapest)
    else:
        floor = floor_price(cost, min_margin_pct)
    target = cheapest.price.quantize(RUPEE, rounding=ROUND_FLOOR)
    reason = "match_cheapest"
    if target < floor:
        target, reason = floor, "floor"
    if target >= current:
        return Verdict("at_floor", floor_price=floor, cheapest=cheapest)
    return Verdict("suggest", suggested_price=target, floor_price=floor, cheapest=cheapest,
                   reason=reason)


def online_update_for(
    *, target: Decimal, branch_price: Decimal | None, global_price: Decimal | None,
    discount_pct: Decimal | None, is_on_sale: bool,
) -> dict:
    """The body for Online's PUT /admin/inventory/{id} that makes the customer pay `target`.

    A Quick Sale row keeps its MRP as branch_price and carries the cut as a discount
    (memory: sribees-matugama-first-branch), so there the discount is recomputed
    instead of lowering the struck-through price."""
    if is_on_sale and discount_pct and discount_pct > 0:
        base = branch_price if branch_price is not None else global_price
        if base and base > target:
            pct = ((base - target) / base * 100).quantize(Decimal("0.000001"))
            return {"discount_percentage": float(pct)}
    return {"branch_price": str(target.quantize(Decimal("0.01")))}
