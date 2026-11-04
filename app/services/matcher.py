"""Which competitor item is our product?

Owner's rule (VISION.md Q13): an equal barcode is a match on its own ("auto");
everything else is only a *candidate* until the Price Checker confirms it once.
A different pack size is never a candidate: 400 g and 200 g are different products.
"""

from dataclasses import dataclass, replace
from decimal import Decimal
from difflib import SequenceMatcher

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
# A loose (bulk) item is never auto-matched (owner 2026-11-04): at most this sure.
LOOSE_MAX_SCORE = 0.9
# Words that only say an item is sold loose; not part of what the product is.
LOOSE_WORDS = frozenset({"bulk", "per", "kg", "kgs", "g", "gm", "gms", "grams", "l", "lt", "ltr",
                         "ml"})
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


def same_word(a: str, b: str) -> bool:
    """One word, allowing a plural or a typing slip ("Hawaian" / "Hawaiian",
    "Snak" / "Snack", "Nut" / "Nuts"); short words must be exact."""
    if a == b or a + "s" == b or b + "s" == a:
        return True
    if len(a) < 4 or len(b) < 4:
        return False
    if a.startswith(b) or b.startswith(a):
        return True
    # A slip changes a letter or two, never a whole syllable ("salted" / "unsalted").
    return abs(len(a) - len(b)) <= 1 and SequenceMatcher(None, a, b).ratio() >= 0.84


def missing_words(ours: Parsed, theirs: Parsed) -> set[str]:
    """Our words with no counterpart in their title."""
    return {w for w in ours.words if not any(same_word(w, t) for t in theirs.words)}


def brand_ok(ours: Parsed, theirs: Parsed) -> bool:
    """Owner (2026-10-29): never suggest a product without our brand in its title.
    Spelling slips count as the brand ("Morison"/"Morisons", "Sun Crush"/"Suncrush",
    "Pelwatte"/"Pelwette"); a different brand never does."""
    b = ours.brand
    if not b:
        return False
    if b in theirs.words or (len(b) >= 3 and b in theirs.text):
        return True
    if b not in ours.words:  # a two-word brand ("babycheramy"): only whole, in order
        return False
    for w in theirs.words:
        # A plural or an extra letter ("Morison" / "Morisons", "Mentos" / "Mento"),
        # never a fragment ("Bake" is not "Bakerdream").
        if (len(w) >= 4 and len(b) >= 4 and abs(len(w) - len(b)) <= 2
                and (w.startswith(b) or b.startswith(w))):
            return True
        if len(w) >= 5 and len(b) >= 5 and SequenceMatcher(None, b, w).ratio() >= 0.85:
            return True
    return False


def loose_ok(ours: Parsed, theirs: Parsed) -> bool:
    """The rule for a shop item sold loose (owner 2026-11-04): it is priced per amount, so
    its size need not equal our pack, but a weight compares only with a weight and a volume
    with a volume, and the names must hold the same words both ways (beyond "bulk", "per"
    and units). A branded product of ours never meets an unbranded bulk item that way."""
    if not theirs.loose or ours.size_unit not in ("g", "ml"):
        return False
    if theirs.size_unit is not None and theirs.size_unit != ours.size_unit:
        return False
    a = replace(ours, words=ours.words - LOOSE_WORDS)
    b = replace(theirs, words=theirs.words - LOOSE_WORDS)
    return bool(a.words) and not missing_words(a, b) and not missing_words(b, a)


def rule_ok(ours: Parsed, theirs: Parsed) -> bool:
    """The owner's brand + name + size rule, or the loose rule for a bulk item."""
    if theirs.loose:
        return loose_ok(ours, theirs)
    return brand_ok(ours, theirs) and same_size(ours, theirs) is True


def score(ours: Parsed, theirs: Parsed) -> float:
    """0..1: how much of our product's name the competitor's title covers,
    blended with overall overlap, then adjusted for pack size."""
    if not ours.words or not theirs.words:
        return 0.0
    if theirs.loose:
        return min(name_score(ours, theirs), LOOSE_MAX_SCORE) if loose_ok(ours, theirs) else 0.0
    # Owner (2026-10-29): brand, name and pack size (g / ml) must all agree.
    if not brand_ok(ours, theirs) or same_size(ours, theirs) is not True:
        return 0.0
    return name_score(ours, theirs)


def name_score(ours: Parsed, theirs: Parsed) -> float:
    """The wording alone, before the brand and size rule: shown on a card a person added
    by hand, never used to propose a match."""
    if not ours.words or not theirs.words:
        return 0.0
    matched = len(ours.words) - len(missing_words(ours, theirs))
    if not matched:
        return 0.0
    coverage = matched / len(ours.words)
    jaccard = matched / (len(ours.words) + len(theirs.words) - matched)
    value = 0.7 * coverage + 0.3 * jaccard
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
    if (parsed.loose or best.score < AUTO_SCORE or same_size(ours, parsed) is not True
            or not brand_ok(ours, parsed)):
        return None
    # Both name what it comes in, and it differs (Box vs Pouch): a person decides.
    if ours.packs and parsed.packs and not ours.packs & parsed.packs:
        return None
    # A shop listing the same product twice (same words, same size) is not a rival.
    rivals = [(c, p) for c, p in ranked[1:]
              if not (not missing_words(p, parsed) and not missing_words(parsed, p)
                      and same_size(p, parsed) is True)]
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
        if (price is None or theirs.loose or same_size(ours, theirs) is not True
                or not brand_ok(ours, theirs)):
            continue
        if ours.packs and theirs.packs and not ours.packs & theirs.packs:
            continue
        if abs(price - our_price) > our_price * PRICE_AGREEMENT:
            continue
        extra = theirs.words - ours.words
        if extra & TYPE_WORDS - ours.words:
            continue
        if missing_words(ours, theirs) and c.score < 0.9:
            continue
        fits.append((len(extra), abs(price - our_price), -c.score, i))
    return ranked[min(fits)[3]][0] if fits else None
