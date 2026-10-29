"""Which competitor item is our product?

Owner's rule (VISION.md Q13): an equal barcode is a match on its own ("auto");
everything else is only a *candidate* until the Price Checker confirms it once.
A different pack size is never a candidate: 400 g and 200 g are different products.
"""

from dataclasses import dataclass
from decimal import Decimal

from app.services.normalize import Parsed

# Below this a name is not worth a person's time.
MIN_CANDIDATE_SCORE = 0.6
CANDIDATES_PER_COMPETITOR = 3
# Owner (VISION Q15): this sure, with both pack sizes known and equal, and clearly ahead
# of the runner-up, a match needs no person.
AUTO_SCORE = 0.95
AUTO_LEAD = 0.05
# Owner (2026-10-29): too many to check by hand. A price that agrees is the strongest
# evidence two titles are one product -- and when a "wrong" flavour costs the same, the
# comparison is still right. These words, though, make it a different kind of product.
PRICE_AGREEMENT = Decimal("0.05")
TYPE_WORDS = {
    "sambol", "chips", "paste", "sauce", "oil", "juice", "cordial", "flakes", "curry",
    "noodles", "soup", "jam", "cubes", "pickle", "chutney", "seeds", "pieces", "powder",
}
# Prices that are placeholders on our side (loose staples added at Rs 1,000) prove nothing.
PLACEHOLDER_PRICES = {Decimal("1000.00")}


@dataclass(frozen=True)
class Candidate:
    key: object
    score: float


def same_size(a: Parsed, b: Parsed) -> bool | None:
    """True / False when both titles carry a size, None when either is silent."""
    if a.size_value is None or b.size_value is None:
        return None
    if a.size_unit != b.size_unit:
        return False
    return abs(a.size_value - b.size_value) <= a.size_value * Decimal("0.02")


def score(ours: Parsed, theirs: Parsed) -> float:
    """0..1: how much of our product's name the competitor's title covers,
    blended with overall overlap, then adjusted for pack size."""
    if not ours.words or not theirs.words:
        return 0.0
    common = ours.words & theirs.words
    if not common:
        return 0.0
    coverage = len(common) / len(ours.words)
    jaccard = len(common) / len(ours.words | theirs.words)
    value = 0.7 * coverage + 0.3 * jaccard
    size = same_size(ours, theirs)
    if size is False:
        return 0.0
    if size is None:
        value *= 0.8  # cannot tell the pack; a person must look harder
    return round(min(value, 1.0), 4)


def best_candidates(
    ours: Parsed, items: list[tuple[object, Parsed]], limit: int = CANDIDATES_PER_COMPETITOR
) -> list[Candidate]:
    scored = [Candidate(key, score(ours, parsed)) for key, parsed in items]
    scored = [c for c in scored if c.score >= MIN_CANDIDATE_SCORE]
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:limit]


def auto_pick(ours: Parsed, ranked: list[tuple[Candidate, Parsed]]) -> Candidate | None:
    """The best candidate when it may be matched without a person, else None.
    `ranked` is best first, each with the competitor item's parsed title."""
    if not ranked:
        return None
    best, parsed = ranked[0]
    if best.score < AUTO_SCORE or same_size(ours, parsed) is not True:
        return None
    # Both name what it comes in, and it differs (Box vs Pouch): a person decides.
    if ours.packs and parsed.packs and not ours.packs & parsed.packs:
        return None
    # A shop listing the same product twice (same words, same size) is not a rival.
    rivals = [(c, p) for c, p in ranked[1:]
              if not (p.words == parsed.words and same_size(p, parsed) is True)]
    if rivals and best.score - rivals[0][0].score < AUTO_LEAD:
        return None
    return best


def price_pick(ours: Parsed, our_name: str, our_price: Decimal | None,
               ranked: list[tuple[Candidate, Parsed, Decimal | None]]) -> Candidate | None:
    """A candidate that may be matched because name, size and price all agree:
    every word of our name is in theirs (or the name scores 0.9+), the same known pack
    size, no clashing pack kind, no word that changes the kind of product, and a price
    within 5 % of ours. Among several, the one with the fewest extra words wins."""
    if (our_price is None or our_price in PLACEHOLDER_PRICES
            or any(ord(ch) > 127 for ch in our_name)):  # a Sinhala name hides words from us
        return None
    fits = []
    for i, (c, theirs, price) in enumerate(ranked):
        if price is None or same_size(ours, theirs) is not True:
            continue
        if ours.packs and theirs.packs and not ours.packs & theirs.packs:
            continue
        if abs(price - our_price) > our_price * PRICE_AGREEMENT:
            continue
        extra = theirs.words - ours.words
        if extra & TYPE_WORDS - ours.words:
            continue
        if ours.words - theirs.words and c.score < 0.9:
            continue
        fits.append((len(extra), abs(price - our_price), -c.score, i))
    return ranked[min(fits)[3]][0] if fits else None
