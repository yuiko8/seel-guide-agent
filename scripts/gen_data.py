"""Generate the simulated knowledge base (deterministic, seed=7).

Everything here is fictional: merchants, prices, policies, customs numbers and
protection terms are simulated for an internship sandbox and must not be read as
real-world rules or as any real company's terms.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "data"
rng = random.Random(7)

MERCHANTS = {
    "nordlys": {"name": "Nordlys Audio", "category": "electronics"},
    "maple": {"name": "Maple & Thread", "category": "apparel"},
    "kiko": {"name": "Kiko Home", "category": "home"},
    "vela": {"name": "Vela Beauty", "category": "beauty"},
    "trailhead": {"name": "Trailhead Gear", "category": "outdoor"},
}

ALL_COUNTRIES = ["US", "CA", "GB", "DE", "FR", "AU", "JP", "BR", "MX"]

# (subcategory, title templates, price range, weight range, tags, battery, liquid)
TEMPLATES = {
    "nordlys": [
        ("headphones", ["{a} Noise-Cancelling Over-Ear Headphones", "{a} Wireless ANC Headphones"], (59, 349), (0.25, 0.4), ["noise-cancelling", "wireless", "gift", "travel"], "contained", False),
        ("earbuds", ["{a} True Wireless Earbuds", "{a} ANC Earbuds Pro"], (29, 229), (0.05, 0.1), ["wireless", "gift", "sport", "noise-cancelling"], "contained", False),
        ("speaker", ["{a} Portable Bluetooth Speaker", "{a} Waterproof Mini Speaker"], (25, 199), (0.3, 1.2), ["wireless", "outdoor", "gift"], "contained", False),
        ("power-bank", ["{a} 20000mAh Power Bank", "{a} MagSafe Battery Pack"], (19, 89), (0.2, 0.45), ["travel", "charging"], "standalone", False),
        ("turntable", ["{a} Belt-Drive Turntable"], (149, 499), (4.0, 7.0), ["vinyl", "gift", "home"], "none", False),
    ],
    "maple": [
        ("sweater", ["{a} Merino Crewneck Sweater", "{a} Cashmere-Blend Cardigan"], (49, 189), (0.3, 0.6), ["gift", "winter"], "none", False),
        ("jacket", ["{a} Packable Down Jacket", "{a} Waxed Cotton Jacket"], (89, 349), (0.5, 1.4), ["winter", "travel"], "none", False),
        ("scarf", ["{a} Wool Scarf", "{a} Silk Square Scarf"], (25, 120), (0.1, 0.2), ["gift", "winter"], "none", False),
        ("bag", ["{a} Leather Tote Bag", "{a} Canvas Weekender Bag", "{a} Leather Crossbody Bag"], (79, 450), (0.6, 1.5), ["gift", "travel", "luxury"], "none", False),
        ("final-sale-tee", ["{a} Organic Cotton Tee (Final Sale)"], (15, 35), (0.15, 0.2), ["basics"], "none", False),
    ],
    "kiko": [
        ("candle", ["{a} Soy Candle Set", "{a} Scented Candle Trio"], (19, 69), (0.6, 1.5), ["gift", "home"], "none", False),
        ("mug", ["{a} Stoneware Mug Set", "{a} Hand-Glazed Ceramic Mug"], (15, 59), (0.4, 1.6), ["gift", "home", "fragile"], "none", False),
        ("lamp", ["{a} Rechargeable Table Lamp", "{a} Linen Shade Floor Lamp"], (39, 259), (0.8, 6.0), ["home", "gift"], "contained", False),
        ("throw", ["{a} Chunky Knit Throw Blanket"], (49, 159), (1.0, 2.0), ["gift", "home", "winter"], "none", False),
    ],
    "vela": [
        ("perfume", ["{a} Eau de Parfum 50ml", "{a} Eau de Toilette 100ml"], (39, 189), (0.2, 0.4), ["gift", "luxury"], "none", True),
        ("skincare", ["{a} Hydrating Serum 30ml", "{a} Night Repair Cream 50ml"], (19, 129), (0.1, 0.3), ["gift", "self-care"], "none", True),
        ("gift-set", ["{a} Mini Skincare Gift Set"], (29, 99), (0.3, 0.6), ["gift", "self-care"], "none", True),
    ],
    "trailhead": [
        ("headlamp", ["{a} Rechargeable Headlamp"], (19, 79), (0.08, 0.15), ["outdoor", "camping"], "contained", False),
        ("tent", ["{a} 2-Person Backpacking Tent"], (149, 499), (1.2, 2.5), ["outdoor", "camping"], "none", False),
        ("bottle", ["{a} Insulated Water Bottle 1L"], (19, 49), (0.4, 0.6), ["outdoor", "gift", "sport"], "none", False),
        ("stove", ["{a} Camping Gas Stove"], (39, 129), (0.3, 1.0), ["outdoor", "camping"], "none", False),
        ("smartwatch", ["{a} GPS Outdoor Smartwatch"], (149, 449), (0.05, 0.1), ["outdoor", "sport", "gift", "wireless"], "contained", False),
    ],
}

ADJ = ["Aurora", "Fjord", "Ember", "Solace", "Drift", "Halo", "Cove", "Nimbus", "Atlas",
       "Juniper", "Lumen", "Basalt", "Willow", "Tidal", "Quill", "Summit", "Sable", "Onyx"]


def gen_catalog() -> list[dict]:
    items = []
    for mid, templates in TEMPLATES.items():
        n = 0
        for sub, titles, (pmin, pmax), (wmin, wmax), tags, battery, liquid in templates:
            for _ in range(rng.randint(10, 16)):
                n += 1
                a = rng.choice(ADJ)
                title = rng.choice(titles).format(a=a)
                price = round(rng.uniform(pmin, pmax) // 1 + 0.99, 2)
                ships_to = list(ALL_COUNTRIES)
                # Some merchants do not ship everywhere.
                if mid == "vela":
                    ships_to = [c for c in ships_to if c not in ("BR",)]
                if mid == "kiko" and rng.random() < 0.4:
                    ships_to = [c for c in ships_to if c not in ("AU", "JP", "BR")]
                if rng.random() < 0.1:
                    ships_to = [c for c in ships_to if c not in ("MX", "BR")]
                items.append({
                    "sku": f"{mid[:3].upper()}-{n:03d}",
                    "merchant_id": mid,
                    "title": title,
                    "category": MERCHANTS[mid]["category"],
                    "subcategory": sub,
                    "price_usd": price,
                    "weight_kg": round(rng.uniform(wmin, wmax), 2),
                    "tags": sorted(set(rng.sample(tags, k=min(len(tags), rng.randint(1, len(tags)))))),
                    "battery": battery,  # none | contained | standalone
                    "liquid": liquid,
                    "final_sale": sub == "final-sale-tee",
                    "ships_to": ships_to,
                    "stock": rng.choice([0, 3, 12, 40, 120, 120, 120]),
                    "rating": round(rng.uniform(3.4, 4.9), 1),
                    "reviews": rng.randint(3, 2400),
                })
    return items


SHIPPING_RULES = {
    "_note": "SIMULATED cross-border rules for a sandbox. Not real customs guidance.",
    "origin": "US",
    "countries": {
        "US": {"name": "United States", "standard_days": [3, 6], "express_days": [1, 3], "duty_free_threshold_usd": 0, "tax_rate": 0.0, "tax_name": "sales tax (merchant collected)", "ddp_available": True, "peak_delay_risk": "low"},
        "CA": {"name": "Canada", "standard_days": [6, 12], "express_days": [3, 5], "duty_free_threshold_usd": 110, "tax_rate": 0.05, "tax_name": "GST", "ddp_available": True, "peak_delay_risk": "medium"},
        "GB": {"name": "United Kingdom", "standard_days": [7, 12], "express_days": [3, 5], "duty_free_threshold_usd": 170, "tax_rate": 0.20, "tax_name": "VAT", "ddp_available": True, "peak_delay_risk": "medium"},
        "DE": {"name": "Germany", "standard_days": [7, 14], "express_days": [3, 6], "duty_free_threshold_usd": 160, "tax_rate": 0.19, "tax_name": "VAT", "ddp_available": True, "peak_delay_risk": "medium"},
        "FR": {"name": "France", "standard_days": [7, 14], "express_days": [3, 6], "duty_free_threshold_usd": 160, "tax_rate": 0.20, "tax_name": "VAT", "ddp_available": True, "peak_delay_risk": "medium"},
        "AU": {"name": "Australia", "standard_days": [8, 16], "express_days": [4, 7], "duty_free_threshold_usd": 650, "tax_rate": 0.10, "tax_name": "GST", "ddp_available": True, "peak_delay_risk": "high"},
        "JP": {"name": "Japan", "standard_days": [6, 12], "express_days": [3, 5], "duty_free_threshold_usd": 65, "tax_rate": 0.10, "tax_name": "consumption tax", "ddp_available": True, "peak_delay_risk": "medium"},
        "BR": {"name": "Brazil", "standard_days": [15, 35], "express_days": [7, 12], "duty_free_threshold_usd": 0, "tax_rate": 0.60, "tax_name": "import tax", "ddp_available": False, "peak_delay_risk": "high"},
        "MX": {"name": "Mexico", "standard_days": [8, 18], "express_days": [4, 7], "duty_free_threshold_usd": 50, "tax_rate": 0.16, "tax_name": "IVA", "ddp_available": False, "peak_delay_risk": "high"},
    },
    "duty_rates_by_category": {"electronics": 0.0, "apparel": 0.12, "home": 0.05, "beauty": 0.04, "outdoor": 0.06},
    "unsupported_destinations": {
        "RU": "Our carriers have suspended service to Russia; orders cannot be shipped there.",
        "KP": "Shipping to North Korea is prohibited.",
        "IR": "Shipping to Iran is prohibited.",
    },
    "restrictions": [
        {"id": "R1", "match": {"battery": "standalone"}, "countries": ["AU", "JP", "BR"], "rule": "Standalone lithium batteries (power banks, battery packs) cannot be shipped by air to this destination."},
        {"id": "R2", "match": {"liquid": True}, "countries": ["BR"], "rule": "Liquid cosmetics and fragrances cannot be imported to this destination by consumer parcel."},
        {"id": "R3", "match": {"subcategory": "stove"}, "countries": ["AU", "JP", "GB", "DE", "FR", "CA", "BR", "MX"], "rule": "Gas appliances are restricted for international parcels; ships to the US only."},
    ],
    "peak_season": {"months": [11, 12], "extra_days": [2, 5]},
}

POLICIES = {
    "nordlys": """# Nordlys Audio — Returns & Refunds Policy (simulated)

## Return window
Unused items in original packaging can be returned within **30 days** of delivery.

## Return shipping
Nordlys pays return shipping for **US** orders. **International customers pay return shipping** and any duties are not refundable.

## Non-returnable items
Opened earbuds cannot be returned for hygiene reasons unless defective.

## Defects and warranty
All electronics carry a **1-year limited warranty**. Defective items are replaced free of charge, including international shipping.

## Refund timing
Refunds are issued to the original payment method within **5–7 business days** after the return is received and inspected.
""",
    "maple": """# Maple & Thread — Returns Policy (simulated)

## Return window
Items can be returned within **45 days** of delivery if unworn with tags attached.

## Return shipping
Free return label for US, CA and GB. Other countries: a **$12 return label fee** is deducted from the refund.

## Final sale
Items marked **Final Sale** cannot be returned or exchanged.

## Exchanges
One free size exchange per order (US, CA, GB only).

## Refund timing
Refunds are processed within **10 business days** of receiving the return.
""",
    "kiko": """# Kiko Home — Returns Policy (simulated)

## Return window
Returns accepted within **14 days** of delivery.

## Return shipping
The customer pays return shipping for all orders. Large items (over 5 kg) incur a **$25 restocking fee**.

## Damaged on arrival
Report damaged or broken items within **48 hours** with photos; Kiko sends a replacement. Reports after 48 hours are not accepted.

## Non-returnable items
Candles that have been burned cannot be returned.

## Refund timing
Refunds within **7 business days** after inspection.
""",
    "vela": """# Vela Beauty — Returns Policy (simulated)

## Return window
Unopened products can be returned within **30 days** of delivery.

## Opened products
Opened skincare and fragrance **cannot be returned** for hygiene reasons, unless the customer had an allergic reaction reported within 14 days.

## Return shipping
Free returns in the US. International customers pay return shipping.

## Refund timing
Refunds within **5 business days** of receipt.
""",
    "trailhead": """# Trailhead Gear — Returns Policy (simulated)

## Return window
Returns within **60 days** of delivery, including lightly used gear.

## Return shipping
Free return shipping for US orders; international customers pay return shipping.

## Warranty
Tents and smartwatches carry a **2-year warranty** against manufacturing defects.

## Refund timing
Refunds within **7 business days** after the return is received.
""",
}

PROTECTION_TERMS = """# Post-Purchase Protection — Product Terms (SIMULATED SANDBOX)

These terms describe two simulated add-on protections offered at checkout in this sandbox. They are fictional and are not the terms of any real company.

## Return Assurance
- Lets the shopper return the item **for any reason within 30 days of delivery**, even if the merchant's own policy would refuse it (for example **final sale** items or **opened** products), or the merchant's window has ended.
- The protection provider refunds the **item price**. The shopper ships the item back with a prepaid label provided by the provider.
- **Not covered:** items damaged by the shopper, items not returned within 14 days of filing, duties and taxes already paid at import, gift cards, perishable goods.
- Return Assurance must be added **at checkout**; it cannot be added after the order is placed.

## Delivery Protection
- Covers parcels that are **lost in transit, stolen after delivery, or arrive damaged**.
- Resolution: a reshipment or refund of the item price and original shipping.
- Claims must be filed within **30 days** of the expected delivery date. Damage claims require photos.
- **Not covered:** delays alone (unless the parcel is over 20 days past the latest estimated date, then it is treated as lost), wrong address entered by the shopper, items seized by customs because they are restricted at the destination.

## Claim outcomes
Every claim is reviewed. **Approval is not guaranteed** until the review is complete; the assistant must never promise that a claim will be approved or that a refund will be issued.

## Pricing
The price of protection is calculated per order from the item category, price, destination and season. Typical prices are 1%–6% of the item price, minimum $0.99.
"""

FAQ = """# Shopping Assistant FAQ (simulated)

## What does DDP mean?
DDP (Delivered Duty Paid) means duties and taxes are collected at checkout, so there is nothing to pay on delivery. Where DDP is not available, the courier collects duties and taxes from the recipient on delivery.

## Do I pay duties on gifts?
Yes. In this sandbox, gifts are treated like any other purchase for customs purposes.

## Can I change my delivery address after ordering?
Address changes are possible only before the order is shipped. Contact support as soon as possible.

## How do I track my order?
A tracking link is emailed when the order ships. International tracking may pause for 2–5 days during customs clearance.

## What happens if my parcel is held at customs?
Customs may hold parcels for inspection. If the item is restricted at the destination it may be returned to sender or destroyed, and this is not covered by Delivery Protection.
"""


def main() -> None:
    ROOT.mkdir(exist_ok=True)
    (ROOT / "policies").mkdir(exist_ok=True)
    catalog = gen_catalog()
    (ROOT / "catalog.json").write_text(json.dumps(catalog, indent=1))
    (ROOT / "merchants.json").write_text(json.dumps(MERCHANTS, indent=1))
    (ROOT / "shipping_rules.json").write_text(json.dumps(SHIPPING_RULES, indent=1))
    for mid, text in POLICIES.items():
        (ROOT / "policies" / f"{mid}.md").write_text(text)
    (ROOT / "protection_terms.md").write_text(PROTECTION_TERMS)
    (ROOT / "faq.md").write_text(FAQ)
    print(f"catalog: {len(catalog)} SKUs")


if __name__ == "__main__":
    main()
