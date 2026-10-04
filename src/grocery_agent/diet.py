"""Dietary restrictions as hard rules, checked in code against every recipe.

A recipe breaks a diet when its title or any ingredient line names a forbidden
food, or when a normalized ingredient sits in a forbidden category (meat,
seafood). Lines that say they are a substitute ("vegan butter", "coconut milk",
"gluten-free pasta") are let through. The check is deliberately strict: a false
alarm costs one recipe, a miss puts beef in a vegetarian's week.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MEAT = ["beef", "steak", "pork", "bacon", "ham", "prosciutto", "pancetta", "guanciale", "sausage", "sausages",
        "chorizo", "salami", "pepperoni", "hot dog", "chicken", "turkey", "duck", "lamb", "mutton", "goat", "veal",
        "venison", "bison", "rabbit", "meat", "meatball", "meatballs", "mince", "brisket", "ribs", "oxtail", "hamburger",
        "liver", "lard", "gelatin", "gelatine", "suet", "bone broth", "drippings"]
SEAFOOD = ["fish", "salmon", "tuna", "cod", "tilapia", "halibut", "haddock", "trout", "mackerel", "sardine",
           "sardines", "anchovy", "anchovies", "shrimp", "shrimps", "prawn", "prawns", "crab", "lobster", "scallop",
           "scallops", "clam", "clams", "mussel", "mussels", "oyster", "oysters", "squid", "calamari", "octopus",
           "shrimp paste", "dashi", "bonito", "katsuobushi", "worcestershire", "caviar", "roe", "swordfish", "catfish",
           "monkfish", "whitefish", "shellfish", "crayfish", "crawfish", "seafood"]
DAIRY = ["milk", "butter", "buttermilk", "cheese", "cheddar", "parmesan", "parmigiano", "pecorino", "mozzarella",
         "ricotta", "feta", "paneer", "mascarpone", "gruyere", "cream", "creme fraiche", "half-and-half", "yogurt",
         "yoghurt", "curd", "ghee", "whey", "casein", "kefir", "buttercream"]
OTHER_ANIMAL = ["egg", "eggs", "egg yolk", "egg yolks", "mayonnaise", "mayo", "honey", "aioli"]
GLUTEN = ["wheat", "flour", "bread", "breadcrumbs", "bread crumbs", "panko", "pasta", "spaghetti", "linguine",
          "fettuccine", "penne", "rigatoni", "macaroni", "lasagna", "orzo", "noodle", "noodles", "ramen", "udon",
          "couscous", "barley", "rye", "bulgur", "farro", "semolina", "seitan", "soy sauce", "tortilla",
          "tortillas", "pita", "naan", "baguette", "croutons", "beer", "malt", "gnocchi", "dumpling", "dumplings"]

# Phrases that contain a forbidden word but are fine. Removed from the text before matching.
SAFE_PHRASES = {
    "meat": ["vegetable broth", "vegetable stock", "mushroom broth", "cauliflower steak", "mushroom steak",
             "jackfruit", "coconut meat", "ham hock substitute", "plant-based meat", "meat substitute",
             "liquid smoke", "goat cheese", "goat's cheese", "goats cheese", "goat milk", "duck sauce",
             "celery rib", "celery ribs", "chicken of the woods", "lamb's lettuce", "hamburger bun", "hamburger buns"],
    "seafood": ["vegan fish sauce", "vegetarian oyster sauce", "mushroom oyster sauce", "oyster mushroom",
                "oyster mushrooms", "vegan worcestershire", "vegetarian worcestershire", "kombu dashi",
                "shiitake dashi", "crab apple"],
    "dairy": ["coconut milk", "almond milk", "oat milk", "soy milk", "soya milk", "rice milk", "cashew milk",
              "coconut cream", "cashew cream", "coconut yogurt", "soy yogurt", "peanut butter", "almond butter",
              "cashew butter", "sunflower butter", "nut butter", "seed butter", "cocoa butter", "apple butter",
              "butternut", "butternut squash", "butter beans", "butter bean", "butter lettuce", "cream of tartar",
              "nutritional yeast", "milk thistle", "bean curd"],
    "other_animal": ["eggplant", "eggplants", "egg noodles substitute", "flax egg", "chia egg", "egg replacer",
                     "vegan mayo", "vegan mayonnaise", "maple syrup", "agave"],
    "gluten": ["rice flour", "almond flour", "coconut flour", "chickpea flour", "gram flour", "besan",
               "corn flour", "cornflour", "tapioca flour", "potato flour", "oat flour", "buckwheat flour",
               "cassava flour", "sorghum flour", "rice noodle", "rice noodles", "glass noodles", "rice pasta",
               "corn tortilla", "corn tortillas", "rice paper", "tamari", "buckwheat", "soba 100%",
               "kelp noodles", "zucchini noodles", "pasta sauce"],
}

# A forbidden word right after one of these is a stand-in ("vegan butter", "gluten-free pasta").
QUALIFIERS = {
    "meat": ["vegan", "vegetarian", "plant-based", "plant based", "meatless", "meat-free", "veggie", "imitation",
             "mock", "soy-based", "soy"],
    "seafood": ["vegan", "vegetarian", "plant-based", "plant based", "imitation", "mock", "fish-free"],
    "dairy": ["vegan", "dairy-free", "dairy free", "non-dairy", "nondairy", "plant-based", "plant based",
              "lactose-free"],
    "other_animal": ["vegan", "egg-free", "eggless", "plant-based", "plant based"],
    "gluten": ["gluten-free", "gluten free", "gf"],
}

# Which groups each diet forbids, and which normalized ingredient categories it rules out.
DIETS: dict[str, tuple[list[str], set[str]]] = {
    "vegetarian": (["meat", "seafood"], {"meat", "seafood"}),
    "vegan": (["meat", "seafood", "dairy", "other_animal"], {"meat", "seafood", "dairy"}),
    "pescatarian": (["meat"], {"meat"}),
    "dairy-free": (["dairy"], set()),
    "gluten-free": (["gluten"], set()),
}
GROUP_TERMS = {"meat": MEAT, "seafood": SEAFOOD, "dairy": DAIRY, "other_animal": OTHER_ANIMAL, "gluten": GLUTEN}
ALIASES = {"veg": "vegetarian", "vegetarian": "vegetarian", "lacto-ovo vegetarian": "vegetarian",
           "plant-based": "vegan", "plant based": "vegan", "pescetarian": "pescatarian", "no meat": "vegetarian",
           "no dairy": "dairy-free", "dairy free": "dairy-free", "lactose-free": "dairy-free", "no gluten": "gluten-free",
           "gluten free": "gluten-free", "coeliac": "gluten-free", "celiac": "gluten-free"}


def normalize_diet(name: str) -> str:
    key = name.strip().lower().replace("_", "-")
    key = ALIASES.get(key, key)
    if key not in DIETS:
        raise ValueError(f"unknown diet '{name}'; use one of {sorted(DIETS)} "
                         "(put single foods to avoid under allergies or dislikes)")
    return key


def _pattern(terms: list[str]) -> re.Pattern:
    words = sorted(terms, key=len, reverse=True)
    return re.compile(r"(?<![\w-])(" + "|".join(re.escape(w) for w in words) + r")(?![\w-])")


_GROUP_RE = {g: _pattern(t) for g, t in GROUP_TERMS.items()}
_QUALIFIER_RE = {g: _pattern(t) for g, t in QUALIFIERS.items()}


def _clean(text: str, group: str) -> str:
    text = text.lower().replace("’", "'")
    for phrase in sorted(SAFE_PHRASES.get(group, []), key=len, reverse=True):
        text = text.replace(phrase, " ")
    return text


def _qualified(text: str, start: int, group: str) -> bool:
    """True when one of the two or three words before `start` marks a substitute."""
    before = " ".join(text[:start].split()[-3:])
    return _QUALIFIER_RE[group].search(before) is not None


def forbidden_term(text: str, group: str) -> str | None:
    """The first forbidden word for `group` in `text` that isn't marked as a substitute."""
    text = _clean(text, group)
    for m in _GROUP_RE[group].finditer(text):
        if not _qualified(text, m.start(), group):
            return m.group(1)
    return None


def _substitute(text: str, groups: list[str]) -> bool:
    """The line names a safe phrase or a substitute marker, so its category alone proves nothing."""
    low = text.lower().replace("’", "'")
    return any(_clean(low, g) != low or _QUALIFIER_RE[g].search(low) for g in groups)


@dataclass
class Item:
    raw: str
    category: str | None = None   # normalized category, when known


def diet_violations(diets: list[str], title: str, items: list[Item]) -> list[str]:
    """Plain-language reasons a recipe breaks any of `diets`, e.g. "not vegetarian: beef (1 lb ground beef)"."""
    out = []
    for diet in diets:
        groups, categories = DIETS[diet]
        reason = None
        for group in groups:
            if (term := forbidden_term(title, group)) is not None:
                reason = f"{term} (title)"
                break
        if reason is None:
            for item in items:
                for group in groups:
                    term = forbidden_term(item.raw, group)
                    if term is None and item.category in categories and not _substitute(item.raw, groups):
                        term = item.category  # e.g. a cut the word lists don't name
                    if term is not None:
                        reason = f"{term} ({item.raw.strip()})"
                        break
                if reason:
                    break
        if reason:
            out.append(f"not {diet}: {reason}")
    return out
