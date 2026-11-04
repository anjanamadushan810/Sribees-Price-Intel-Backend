"""A shop's price at our pack size (owner, 2026-11-04).

A pack is compared as it is. An item sold loose is priced for an amount (its size_value:
Nexmart "Green Gram - Bulk" Rs 97.50 for 100 g), so it is scaled to our pack before any
comparison: Rs 97.50 / 100 g against our 1 kg is Rs 975.00. A loose item with no amount
is priced per 1 kg (or 1 l) of our unit. When the units differ (g against ml) or our pack
size is unknown, a loose price cannot be compared and is left out (None).
"""

from decimal import ROUND_HALF_UP, Decimal

ONE_KG = Decimal(1000)
CENT = Decimal("0.01")


def comparable(price: Decimal | None, *, loose: bool, size_value: Decimal | None,
               size_unit: str | None, our_size: Decimal | None,
               our_unit: str | None) -> Decimal | None:
    if price is None or not loose:
        return price
    if our_size is None or our_unit not in ("g", "ml"):
        return None
    if size_value is None:
        size_value, size_unit = ONE_KG, our_unit
    if size_unit != our_unit or size_value <= 0:
        return None
    return (price * our_size / size_value).quantize(CENT, rounding=ROUND_HALF_UP)


def for_item(item, product) -> Decimal | None:
    """`comparable` for a CompetitorItem against our Product."""
    return comparable(item.last_price, loose=item.loose, size_value=item.size_value,
                      size_unit=item.size_unit, our_size=product.size_value,
                      our_unit=product.size_unit)
