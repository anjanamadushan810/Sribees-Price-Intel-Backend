"""How often each product is checked (VISION.md Q3): it follows the product's
price_review_level from Online."""

from datetime import datetime, timedelta

INTERVAL = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=30),
    "long": timedelta(days=60),
}
# A run starts at a slightly different minute each day; without slack a product
# checked at 05:10 yesterday would not be due at 05:05 today.
SLACK = timedelta(hours=3)


def is_due(review_level: str, last_checked: datetime | None, now: datetime) -> bool:
    if last_checked is None:
        return True
    return now - last_checked >= INTERVAL.get(review_level, INTERVAL["long"]) - SLACK


def is_fresh(review_level: str, seen_at: datetime, now: datetime, stale_days: dict) -> bool:
    """A price older than its level's stale window must not drive a suggestion."""
    days = stale_days.get(review_level, stale_days.get("long", 70))
    return now - seen_at <= timedelta(days=days)
