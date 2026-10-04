"""Rule-based ingredient-line parsing and unit conversion.

Splits "2 1/2 cups finely chopped onion (about 2)" into quantity, unit and a
cleaned name without any model call. Only the cleaned *name* is sent to the
worker model for canonicalization, and only once per distinct name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

UNICODE_FRACTIONS = {
    "½": 0.5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": 0.25, "¾": 0.75, "⅕": 0.2, "⅖": 0.4,
    "⅗": 0.6, "⅘": 0.8, "⅙": 1 / 6, "⅚": 5 / 6, "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
}

# unit alias -> (canonical unit, kind, factor to base) ; base is g for mass, ml for volume
UNITS: dict[str, tuple[str, str, float]] = {}
for names, unit, kind, factor in [
    (("g", "gram", "grams", "gr"), "g", "mass", 1),
    (("kg", "kilogram", "kilograms", "kilo", "kilos"), "kg", "mass", 1000),
    (("mg",), "mg", "mass", 0.001),
    (("oz", "ounce", "ounces"), "oz", "mass", 28.35),
    (("lb", "lbs", "pound", "pounds"), "lb", "mass", 453.6),
    (("ml", "milliliter", "milliliters", "millilitre", "millilitres"), "ml", "volume", 1),
    (("l", "liter", "liters", "litre", "litres"), "l", "volume", 1000),
    (("tsp", "teaspoon", "teaspoons"), "tsp", "volume", 4.93),
    (("tbsp", "tablespoon", "tablespoons", "tbs", "tbl", "tb"), "tbsp", "volume", 14.79),
    (("cup", "cups"), "cup", "volume", 240),
    (("fl oz", "fluid ounce", "fluid ounces"), "fl oz", "volume", 29.57),
    (("pint", "pints", "pt"), "pint", "volume", 473),
    (("quart", "quarts", "qt"), "quart", "volume", 946),
    (("pinch", "pinches", "dash", "dashes"), "pinch", "volume", 0.3),
    (("clove", "cloves"), "clove", "each", 1),
    (("can", "cans", "tin", "tins"), "can", "each", 1),
    (("bunch", "bunches"), "bunch", "each", 1),
    (("stalk", "stalks", "stick", "sticks"), "stalk", "each", 1),
    (("slice", "slices"), "slice", "each", 1),
    (("piece", "pieces", "pc", "pcs"), "piece", "each", 1),
    (("handful", "handfuls"), "handful", "each", 1),
    (("sprig", "sprigs"), "sprig", "each", 1),
    (("package", "packages", "pkg", "packet", "packets", "pack", "packs"), "package", "each", 1),
    (("head", "heads"), "head", "each", 1),
    (("large", "medium", "small"), "", "each", 1),
]:
    for n in names:
        UNITS[n] = (unit, kind, factor)

# Typical grams for "each"-type units that don't depend much on the ingredient.
GENERIC_EACH_G = {"clove": 5, "can": 400, "bunch": 60, "stalk": 40, "slice": 25, "handful": 30,
                  "sprig": 1, "package": 250, "pinch": 0.3}

PREP_WORDS = re.compile(
    r"\b(finely|roughly|coarsely|thinly|freshly|chopped|minced|diced|sliced|grated|crushed|peeled|"
    r"divided|softened|melted|beaten|julienned|cubed|halved|quartered|shredded|trimmed|rinsed|drained|"
    r"packed|heaping|level|optional|to taste|for serving|for garnish|room temperature|boneless|skinless|"
    r"about|approximately|plus more|or more|fresh|dried|ground|large|medium|small)\b",
    re.I,
)

_NUM = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d*\.\d+|\d+)"
_FRAC = "".join(UNICODE_FRACTIONS)
_QTY = re.compile(rf"^\s*(?P<q1>\d+\s*[{_FRAC}]|[{_FRAC}]|{_NUM})"
                  rf"(?:\s*(?:-|–|to)\s*(?P<q2>{_NUM}))?\s*")


def _to_float(token: str) -> float:
    token = token.strip()
    for ch, val in UNICODE_FRACTIONS.items():
        if ch in token:
            whole = token.replace(ch, "").strip()
            return (float(whole) if whole else 0) + val
    if " " in token:  # "2 1/2"
        whole, frac = token.split()
        return float(whole) + _to_float(frac)
    if "/" in token:
        num, den = token.split("/")
        return float(num) / float(den)
    return float(token)


@dataclass
class ParsedLine:
    raw: str
    quantity: float | None
    unit: str  # canonical unit, "" for bare counts
    unit_kind: str  # "mass" | "volume" | "each"
    name: str  # cleaned ingredient name, lowercase

    def base_amount(self) -> tuple[float | None, str]:
        """Amount in g (mass), ml (volume) or count (each)."""
        if self.quantity is None:
            return None, self.unit_kind
        factor = UNITS.get(self.unit, ("", self.unit_kind, 1))[2] if self.unit else 1
        return self.quantity * factor, self.unit_kind


def parse_line(raw: str) -> ParsedLine:
    text = raw.strip()
    text = re.sub(r"\([^)]*\)", " ", text)  # drop parentheticals
    text = text.split(",")[0]  # drop trailing prep notes ("onion, diced")
    qty: float | None = None
    m = _QTY.match(text)
    if m:
        q1 = _to_float(m.group("q1"))
        q2 = _to_float(m.group("q2")) if m.group("q2") else None
        qty = (q1 + q2) / 2 if q2 else q1
        text = text[m.end():]
    unit, kind = "", "each"
    low = text.lower().lstrip()
    for alias in sorted(UNITS, key=len, reverse=True):
        if re.match(rf"{re.escape(alias)}\.?(\s|$)", low):
            unit, kind, _ = UNITS[alias]
            low = low[len(alias):].lstrip(". ")
            break
    else:
        low = low.strip()
    name = re.sub(r"^\s*of\s+", "", low)
    name = PREP_WORDS.sub(" ", name)
    name = re.sub(r"[^a-z0-9&' -]", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" -")
    return ParsedLine(raw=raw, quantity=qty, unit=unit, unit_kind=kind, name=name)


def to_grams(line: ParsedLine, each_g: float | None, density_g_per_ml: float | None) -> float | None:
    """Best-effort grams for a parsed line, given per-ingredient facts."""
    amount, kind = line.base_amount()
    if amount is None:
        return None
    if kind == "mass":
        return amount
    if kind == "volume":
        return amount * (density_g_per_ml or 1.0)
    if line.unit in GENERIC_EACH_G:
        return amount * GENERIC_EACH_G[line.unit]
    return amount * each_g if each_g else None
