"""Deterministic commerce tools. These own the *facts*: price, stock, delivery
window, taxes/duties and shipping restrictions. The LLM never invents these."""
from __future__ import annotations

import datetime as dt
import math

from pydantic import BaseModel, Field, field_validator

from . import kb

CATEGORIES = ["electronics", "apparel", "home", "beauty", "outdoor"]

# keyword -> (category, subcategory or None)
KEYWORD_MAP = {
    "headphone": ("electronics", "headphones"), "headphones": ("electronics", "headphones"),
    "earbud": ("electronics", "earbuds"), "earbuds": ("electronics", "earbuds"), "earphones": ("electronics", "earbuds"),
    "speaker": ("electronics", "speaker"), "power bank": ("electronics", "power-bank"), "powerbank": ("electronics", "power-bank"),
    "battery pack": ("electronics", "power-bank"), "charger": ("electronics", "power-bank"),
    "turntable": ("electronics", "turntable"), "record player": ("electronics", "turntable"),
    "sweater": ("apparel", "sweater"), "cardigan": ("apparel", "sweater"), "jacket": ("apparel", "jacket"), "coat": ("apparel", "jacket"),
    "scarf": ("apparel", "scarf"), "bag": ("apparel", "bag"), "tote": ("apparel", "bag"), "handbag": ("apparel", "bag"), "purse": ("apparel", "bag"),
    "t-shirt": ("apparel", "final-sale-tee"), "tee": ("apparel", "final-sale-tee"),
    "candle": ("home", "candle"), "mug": ("home", "mug"), "lamp": ("home", "lamp"), "blanket": ("home", "throw"), "throw": ("home", "throw"),
    "perfume": ("beauty", "perfume"), "fragrance": ("beauty", "perfume"), "cologne": ("beauty", "perfume"),
    "serum": ("beauty", "skincare"), "skincare": ("beauty", "skincare"), "cream": ("beauty", "skincare"), "moisturizer": ("beauty", "skincare"),
    "headlamp": ("outdoor", "headlamp"), "tent": ("outdoor", "tent"), "bottle": ("outdoor", "bottle"),
    "stove": ("outdoor", "stove"), "smartwatch": ("outdoor", "smartwatch"), "watch": ("outdoor", "smartwatch"),
}
TAG_WORDS = {"noise-cancelling", "noise cancelling", "anc", "wireless", "travel", "gift", "outdoor", "camping", "sport",
             "winter", "luxury", "self-care", "home", "waterproof", "vinyl"}


class SearchInput(BaseModel):
    budget_max: float | None = Field(default=None, gt=0, le=100000)
    budget_min: float | None = Field(default=None, ge=0)
    country: str | None = None
    category: str | None = None
    keywords: list[str] = Field(default_factory=list)
    limit: int = Field(default=3, ge=1, le=10)

    @field_validator("category")
    @classmethod
    def _cat(cls, v):
        if v is not None and v not in CATEGORIES:
            return None
        return v


def search_products(budget_max=None, budget_min=None, country=None, category=None, keywords=None, limit=3) -> dict:
    keywords = [k.lower() for k in (keywords or [])]
    subcats, cats = set(), set([category] if category else [])
    tags = set()
    for k in keywords:
        if k in KEYWORD_MAP:
            c, s = KEYWORD_MAP[k]
            cats.add(c)
            if s:
                subcats.add(s)
        kk = "noise-cancelling" if k in ("noise cancelling", "anc") else k
        if kk in TAG_WORDS:
            tags.add(kk)

    excluded = {"over_budget": 0, "not_shipping": 0, "out_of_stock": 0, "restricted": 0}
    restricted_examples: list[dict] = []
    scored = []
    for p in kb.catalog():
        if cats and p["category"] not in cats:
            continue
        if subcats and p["subcategory"] not in subcats:
            continue
        if budget_max is not None and p["price_usd"] > budget_max:
            excluded["over_budget"] += 1
            continue
        if budget_min is not None and p["price_usd"] < budget_min:
            continue
        if p["stock"] <= 0:
            excluded["out_of_stock"] += 1
            continue
        if country:
            r = check_restrictions(p["sku"], country)
            if not r["allowed"]:
                excluded["restricted" if r["reason_code"] != "merchant_not_shipping" else "not_shipping"] += 1
                if len(restricted_examples) < 2:
                    restricted_examples.append({"sku": p["sku"], "reason": r["reason"]})
                continue
        tag_hits = len(tags & set(p["tags"]))
        quality = p["rating"] + math.log10(1 + p["reviews"]) / 2
        budget_fit = 0.0
        if budget_max:
            budget_fit = 1 - abs((budget_max * 0.8) - p["price_usd"]) / budget_max  # prefer ~80% of budget
        score = 2.0 * tag_hits + quality + budget_fit
        scored.append((score, p))
    scored.sort(key=lambda x: (-x[0], x[1]["sku"]))
    results = [_product_view(p) for _, p in scored[:limit]]
    return {"results": results, "total_matches": len(scored), "excluded": excluded,
            "restricted_examples": restricted_examples,
            "applied_filters": {"budget_max": budget_max, "budget_min": budget_min, "country": country,
                                "categories": sorted(cats), "subcategories": sorted(subcats), "tags": sorted(tags)}}


def _product_view(p: dict) -> dict:
    m = kb.merchants()[p["merchant_id"]]
    return {"sku": p["sku"], "title": p["title"], "merchant": m["name"], "merchant_id": p["merchant_id"],
            "category": p["category"], "subcategory": p["subcategory"], "price_usd": p["price_usd"],
            "rating": p["rating"], "reviews": p["reviews"], "tags": p["tags"], "final_sale": p["final_sale"],
            "stock": p["stock"]}


class SkuCountryInput(BaseModel):
    sku: str = Field(pattern=r"^[A-Z]{3}-\d{3}$")
    country: str = Field(min_length=2, max_length=2)


def get_product(sku: str) -> dict:
    p = kb.catalog_by_sku().get(sku)
    if not p:
        return {"found": False, "sku": sku}
    return {"found": True, **_product_view(p), "weight_kg": p["weight_kg"], "battery": p["battery"], "liquid": p["liquid"]}


def check_restrictions(sku: str, country: str) -> dict:
    rules = kb.shipping_rules()
    p = kb.catalog_by_sku().get(sku)
    if not p:
        return {"allowed": False, "reason_code": "unknown_sku", "reason": f"Unknown product {sku}."}
    if country in rules["unsupported_destinations"]:
        return {"allowed": False, "reason_code": "destination_unsupported", "rule_id": f"DEST-{country}",
                "reason": rules["unsupported_destinations"][country]}
    if country not in rules["countries"]:
        return {"allowed": False, "reason_code": "destination_unknown", "reason": f"We do not have shipping data for {country}."}
    for r in rules["restrictions"]:
        if country in r["countries"] and all(p.get(k) == v for k, v in r["match"].items()):
            return {"allowed": False, "reason_code": "restricted_item", "rule_id": r["id"], "reason": r["rule"]}
    if country not in p["ships_to"]:
        return {"allowed": False, "reason_code": "merchant_not_shipping",
                "reason": f"{kb.merchants()[p['merchant_id']]['name']} does not ship this item to {kb.country_name(country)}."}
    return {"allowed": True, "reason_code": "ok"}


def standard_window(country: str, today: str | None = None) -> list[int] | None:
    rules = kb.shipping_rules()
    c = rules["countries"].get(country)
    if not c:
        return None
    d = dt.date.fromisoformat(today) if today else dt.date.today()
    lo, hi = c["standard_days"]
    if d.month in rules["peak_season"]["months"]:
        lo += rules["peak_season"]["extra_days"][0]
        hi += rules["peak_season"]["extra_days"][1]
    return [lo, hi]


def estimate_shipping(sku: str, country: str, today: str | None = None) -> dict:
    """Delivery window + landed cost (item + simulated tax + duty)."""
    rules = kb.shipping_rules()
    p = kb.catalog_by_sku()[sku]
    restr = check_restrictions(sku, country)
    if not restr["allowed"]:
        return {"sku": sku, "country": country, "shippable": False, **restr}
    c = rules["countries"][country]
    d = dt.date.fromisoformat(today) if today else dt.date.today()
    peak = d.month in rules["peak_season"]["months"]
    lo, hi = standard_window(country, today)
    shipping_fee = 0.0 if p["price_usd"] >= 75 or country == "US" else round(9.0 + 4.0 * p["weight_kg"], 2)
    price = p["price_usd"]
    tax = duty = 0.0
    if country != "US":
        if price > c["duty_free_threshold_usd"]:
            duty = round(price * rules["duty_rates_by_category"][p["category"]], 2)
        tax = round((price + shipping_fee + duty) * c["tax_rate"], 2)
    return {
        "sku": sku, "country": country, "country_name": c["name"], "shippable": True,
        "standard_days": [lo, hi], "express_days": c["express_days"], "peak_season_applied": peak,
        "delay_risk": "high" if peak and c["peak_delay_risk"] != "low" else c["peak_delay_risk"],
        "shipping_fee_usd": shipping_fee, "tax_usd": tax, "tax_name": c["tax_name"], "duty_usd": duty,
        "ddp": c["ddp_available"],
        "taxes_collected": "at checkout" if c["ddp_available"] else "by the courier on delivery",
        "landed_total_usd": round(price + shipping_fee + tax + duty, 2),
        "source": "shipping_rules.json (simulated)",
    }

