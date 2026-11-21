"""Simulated post-purchase protection pricing (sandbox).

Per-order dynamic pricing in the spirit of risk-based protection products: price is
derived from the item's return risk (category, final sale, the merchant's own policy
strictness) and delivery risk (destination lane, peak season, fragility). Every
factor is returned so the quote is explainable and auditable - the recommendation
logic is rules, not the LLM's opinion.
"""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field

from . import kb
from .commerce import check_restrictions

RA_BASE = {"apparel": 0.040, "beauty": 0.045, "electronics": 0.030, "home": 0.030, "outdoor": 0.025}
MERCHANT_WINDOW_DAYS = {"nordlys": 30, "maple": 45, "kiko": 14, "vela": 30, "trailhead": 60}
LANE_RISK = {"low": 0.0, "medium": 0.005, "high": 0.012}
MIN_PRICE, MAX_RATE = 0.99, 0.06


class QuoteInput(BaseModel):
    sku: str = Field(pattern=r"^[A-Z]{3}-\d{3}$")
    country: str = Field(min_length=2, max_length=2)


def _price(rate: float, item_price: float) -> float:
    return round(max(MIN_PRICE, min(rate, MAX_RATE) * item_price), 2)


def quote_protection(sku: str, country: str, today: str | None = None) -> dict:
    p = kb.catalog_by_sku().get(sku)
    if not p:
        return {"sku": sku, "available": False, "reason": "unknown product"}
    restr = check_restrictions(sku, country)
    if not restr["allowed"]:
        return {"sku": sku, "available": False, "reason": restr["reason"]}
    rules = kb.shipping_rules()
    lane = rules["countries"][country]["peak_delay_risk"]
    d = dt.date.fromisoformat(today) if today else dt.date.today()
    peak = d.month in rules["peak_season"]["months"]
    price = p["price_usd"]

    # ---- Return Assurance ----
    ra_rate, ra_factors = RA_BASE[p["category"]], [f"{p['category']} base rate"]
    if p["final_sale"]:
        ra_rate += 0.010
        ra_factors.append("final sale item")
    if MERCHANT_WINDOW_DAYS[p["merchant_id"]] <= 14:
        ra_rate += 0.010
        ra_factors.append("store has a short return window")
    if p["category"] == "beauty":
        ra_factors.append("store won't take back opened products")
    ra_reasons = []
    if p["final_sale"]:
        ra_reasons.append("This item is final sale, so the store won't accept a return.")
    if p["category"] == "beauty":
        ra_reasons.append("Opened beauty products can't be returned to the store.")
    if MERCHANT_WINDOW_DAYS[p["merchant_id"]] <= 14:
        ra_reasons.append("The store only accepts returns for 14 days.")
    if country != "US" and p["merchant_id"] in ("nordlys", "vela", "trailhead", "kiko"):
        ra_reasons.append("International returns to this store are at your own cost.")

    # ---- Delivery Protection ----
    dp_rate, dp_factors = 0.012, ["base rate"]
    dp_rate += LANE_RISK[lane]
    if LANE_RISK[lane]:
        dp_factors.append(f"{lane} delay risk to {rules['countries'][country]['name']}")
    if peak:
        dp_rate += 0.005
        dp_factors.append("peak season")
    fragile = "fragile" in p["tags"] or p["subcategory"] in ("mug", "turntable", "lamp")
    if fragile:
        dp_rate += 0.010
        dp_factors.append("fragile item")
    dp_reasons = []
    if lane == "high" or (peak and lane != "low"):
        dp_reasons.append("Deliveries on this route are often delayed or lost in peak season.")
    if fragile:
        dp_reasons.append("Fragile items are more likely to arrive damaged.")
    if price >= 200:
        dp_reasons.append("It's a higher-value parcel.")

    low_value = price < 30
    options = [
        {"type": "return_assurance", "name": "Return Assurance", "price_usd": _price(ra_rate, price),
         "rate_pct": round(min(ra_rate, MAX_RATE) * 100, 1), "factors": ra_factors,
         "recommended": bool(ra_reasons) and not low_value, "reasons": ra_reasons,
         "covers": "Return for any reason within 30 days of delivery, even final sale or opened items.",
         "doc": "protection#return-assurance"},
        {"type": "delivery_protection", "name": "Delivery Protection", "price_usd": _price(dp_rate, price),
         "rate_pct": round(min(dp_rate, MAX_RATE) * 100, 1), "factors": dp_factors,
         "recommended": bool(dp_reasons) and not low_value, "reasons": dp_reasons,
         "covers": "Lost in transit, stolen after delivery, or arrived damaged.",
         "doc": "protection#delivery-protection"},
    ]
    return {"sku": sku, "title": p["title"], "item_price_usd": price, "country": country, "available": True,
            "options": options, "must_add_at_checkout": True,
            "low_value_note": "For an item this inexpensive, protection may not be worth it." if low_value else None,
            "source": "protection pricing model (simulated)"}
