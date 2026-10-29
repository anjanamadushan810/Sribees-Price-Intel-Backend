"""Turn shop titles into comparable parts: words, pack size, barcode.

"MALIBAN BISCUIT CHOCOLATE CREAM - 400 g" and "Maliban Chocolate Cream Biscuit 400G"
both become words {maliban, biscuit, chocolate, cream} and size (400, "g").
Sizes are kept in base units (g, ml, pcs) so 1kg == 1000g.
"""

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

_UNITS: dict[str, tuple[str, Decimal]] = {
    "g": ("g", Decimal(1)),
    "gr": ("g", Decimal(1)),
    "gm": ("g", Decimal(1)),
    "gms": ("g", Decimal(1)),
    "grams": ("g", Decimal(1)),
    "kg": ("g", Decimal(1000)),
    "kgs": ("g", Decimal(1000)),
    "ml": ("ml", Decimal(1)),
    "l": ("ml", Decimal(1000)),
    "ltr": ("ml", Decimal(1000)),
    "lt": ("ml", Decimal(1000)),
    "litre": ("ml", Decimal(1000)),
    "liter": ("ml", Decimal(1000)),
    "pcs": ("pcs", Decimal(1)),
    "pc": ("pcs", Decimal(1)),
    "s": ("pcs", Decimal(1)),
    "pack": ("pcs", Decimal(1)),
    "nos": ("pcs", Decimal(1)),
}
# "400g", "400 g", "1.5L", "2x100g" (multipacks read as the total).
_SIZE = re.compile(
    r"(?:(\d+)\s*[x×]\s*)?(\d+(?:\.\d+)?)\s*(" + "|".join(sorted(_UNITS, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"[a-z0-9]+")
# Words that say nothing about which product it is.
_STOP = {
    "the", "and", "with", "of", "for", "new", "pack", "packet", "pkt", "bottle", "btl", "tin",
    "jar", "box", "bag", "pouch", "sachet", "brand", "pcs", "pc", "nos", "each", "free",
}
# What the product comes in. Not a matching word, but two titles that each name a
# different one (Box vs Pouch, Packet vs Tin) are different products.
_PACK_KINDS = {
    "box": "box", "pouch": "pouch", "packet": "packet", "pkt": "packet", "tin": "tin",
    "bottle": "bottle", "btl": "bottle", "jar": "jar", "bag": "bag", "sachet": "sachet",
    "can": "can", "bib": "bag", "tube": "tube", "refill": "refill",
}
# Brands that start with an everyday word: the brand is the first two words
# ("Baby Cheramy", not "Baby"; "Little Lion", "Sun Crush", "Mr Pop").
_GENERIC_LEAD = {"baby", "little", "mr", "my", "sun", "the", "new", "lion", "red", "white"}
_BARCODE = re.compile(r"^\d{8}$|^\d{12,14}$")


@dataclass(frozen=True)
class Parsed:
    words: frozenset[str]
    size_value: Decimal | None
    size_unit: str | None
    packs: frozenset[str] = frozenset()
    # The first word of the title: the brand on almost every shelf product
    # ("Munchee", "Elephant", "Anchor"), and `text` the title with no spaces, to find
    # a brand written as one word or two ("Sun Crush" / "Suncrush").
    brand: str | None = None
    text: str = ""


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def parse_size(text: str) -> tuple[Decimal | None, str | None]:
    """The last size in the title wins ("Tin 245G" beats a "2 in 1" earlier on)."""
    found = None
    for m in _SIZE.finditer(text or ""):
        found = m
    if found is None:
        return None, None
    count, amount, unit = found.groups()
    base, factor = _UNITS[unit.lower()]
    value = Decimal(amount) * factor * (int(count) if count else 1)
    if value <= 0:
        return None, None
    return value.quantize(Decimal("0.001")), base


def parse(name: str, pack: str | None = None) -> Parsed:
    size_value, size_unit = parse_size(f"{name} {pack or ''}")
    text = _SIZE.sub(" ", _ascii(name))
    raw = _WORD.findall(text)
    words = frozenset(w for w in raw if w not in _STOP and not (w.isdigit() and len(w) < 3))
    packs = frozenset(_PACK_KINDS[w] for w in raw if w in _PACK_KINDS)
    named = [w for w in raw if not w.isdigit() and len(w) > 1]
    brand = named[0] if named else None
    if brand in _GENERIC_LEAD and len(named) > 1:
        brand = named[0] + named[1]
    return Parsed(words=words, size_value=size_value, size_unit=size_unit, packs=packs,
                  brand=brand, text="".join(raw))


def barcode_of(code: str | None) -> str | None:
    """An EAN-8 / UPC-A / EAN-13 / GTIN-14 with a valid check digit, else None."""
    code = (code or "").strip()
    if not _BARCODE.match(code):
        return None
    digits = [int(c) for c in code]
    check = digits.pop()
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return code if (10 - total % 10) % 10 == check else None
