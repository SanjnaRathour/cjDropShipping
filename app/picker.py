"""
Scans Mirthovia-relevant categories on CJ, filters for cute/on-brand +
non-seasonal, pulls a representative variant + real shipping cost to every
destination country, and ranks by worst-case margin % (profitable
everywhere you sell, not just one market).

Architecture mirrors /home/vqcodes/Documents/Sanjna/check-products/cj_tool.py
(a previously tested tool against the real CJ API) - same auth/search/variant/
freight calls, extended with: multi-country worst-case margin, a cuteness
keyword filter on top of the relevance/seasonal filters, and a verified
product_url_for() link per product.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from app.cj_client import (
    CJClient,
    CJAPIError,
    is_seasonal,
    pick_representative_variant,
    product_url_for,
    to_float,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SCAN_RESULT_FILE = DATA_DIR / "last_scan.json"

# Mirthovia-relevant categories. target_retail is a realistic fixed market
# price for that product type - using landed_cost x markup for everything
# would make every product's margin come out roughly identical regardless of
# what it actually sells for, which defeats the point of ranking by margin.
CATEGORIES = [
    {"name": "Hair Accessories", "keywords": ["cute hair clip", "pearl hair claw clip"], "target_retail": 12.99},
    {"name": "Phone Accessories", "keywords": ["cute phone charm", "kawaii phone case"], "target_retail": 14.99},
    {"name": "Stationery & Desk", "keywords": ["kawaii stationery", "cute desk organizer"], "target_retail": 9.99},
    {"name": "Keychains & Charms", "keywords": ["cute keychain", "plush bag charm"], "target_retail": 8.99},
    {"name": "Jewelry", "keywords": ["cute ring set", "kawaii earrings"], "target_retail": 11.99},
]

TOP_PER_CATEGORY = 6
TOP_OVERALL = 12
FALLBACK_MARKUP = 3.5


def is_relevant(name: str, keyword: str) -> bool:
    return keyword.strip().lower() in (name or "").lower()


def is_cute(name: str, cuteness_keywords: list[str]) -> bool:
    if not cuteness_keywords:
        return True
    lname = (name or "").lower()
    return any(marker in lname for marker in cuteness_keywords)


def _cheapest_shipping(client: CJClient, vid: str, destination_country: str):
    try:
        options = client.calculate_freight("CN", destination_country, [{"vid": vid, "quantity": 1}])
    except CJAPIError:
        return None, None
    if not options:
        return None, None
    options.sort(key=lambda o: to_float(o.get("logisticPrice"), 1e9))
    cheapest = options[0]
    return to_float(cheapest.get("logisticPrice")), cheapest.get("logisticName")


def _score_product(
    client: CJClient,
    p: dict,
    category_name: str,
    target_retail: float,
    destination_countries: list[str],
    markup: float,
    log,
) -> dict | None:
    pid = p.get("id")
    name = p.get("nameEn") or ""
    variants = client.get_variants(pid)
    variant = pick_representative_variant(variants)
    if not variant:
        log(f"    [skip] no usable variant: {name[:60]}")
        return None

    vid = variant.get("vid")
    cost = to_float(variant.get("variantSellPrice"), to_float(p.get("sellPrice")))

    per_country = {}
    for country in destination_countries:
        shipping, carrier = _cheapest_shipping(client, vid, country)
        if shipping is None:
            continue
        per_country[country] = {"shipping_cost": round(shipping, 2), "carrier": carrier}

    if not per_country:
        log(f"    [skip] no shipping quote for any destination: {name[:60]}")
        return None

    # price against a realistic fixed market price for the category; only
    # fall back to a flat markup if the landed cost is already close to/over it
    worst_shipping = max(v["shipping_cost"] for v in per_country.values())
    landed_worst = round(cost + worst_shipping, 2)
    if target_retail > landed_worst:
        retail = round(target_retail, 2)
        retail_source = "category target price"
    else:
        retail = round(landed_worst * markup, 2)
        retail_source = f"{markup}x markup"

    for country, v in per_country.items():
        margin = round(retail - cost - v["shipping_cost"], 2)
        v["margin"] = margin
        v["margin_pct"] = round((margin / retail) * 100, 1) if retail else 0.0

    worst = min(per_country.values(), key=lambda v: v["margin_pct"])
    best = max(per_country.values(), key=lambda v: v["margin_pct"])

    return {
        "pid": pid,
        "vid": vid,
        "name": name,
        "category": category_name,
        "image": p.get("bigImage"),
        "product_url": product_url_for(pid, name),
        "sku": variant.get("variantSku") or p.get("sku"),
        "cost": round(cost, 2),
        "retail": retail,
        "retail_source": retail_source,
        "per_country": per_country,
        "worst_case_margin_pct": worst["margin_pct"],
        "best_case_margin_pct": best["margin_pct"],
        "shipping_range": (
            round(min(v["shipping_cost"] for v in per_country.values()), 2),
            round(max(v["shipping_cost"] for v in per_country.values()), 2),
        ),
        "listed_num": int(p.get("listedNum") or 0),
    }


def run_scan(
    client: CJClient,
    cuteness_keywords: list[str],
    destination_countries: list[str],
    markup: float = FALLBACK_MARKUP,
    log=print,
) -> dict:
    categories_out = []
    all_products = []

    for cat in CATEGORIES:
        log(f"=== {cat['name']} ===")
        seen_ids = set()
        merged = []
        per_keyword_limit = max(2, -(-TOP_PER_CATEGORY // len(cat["keywords"])))

        for kw in cat["keywords"]:
            log(f"  searching '{kw}'...")
            try:
                raw = client.search_products(kw, size=30)
            except CJAPIError as e:
                log(f"  [warn] search failed for '{kw}': {e}")
                continue

            filtered = [
                p for p in raw
                if not is_seasonal(p.get("nameEn"))
                and is_relevant(p.get("nameEn"), kw)
                and is_cute(p.get("nameEn"), cuteness_keywords)
            ]
            log(f"    {len(raw)} returned, {len(filtered)} passed seasonal+relevance+cuteness filters")
            filtered.sort(key=lambda p: int(p.get("listedNum") or 0), reverse=True)

            added = 0
            for p in filtered:
                if added >= per_keyword_limit:
                    break
                pid = p.get("id")
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
                merged.append(p)
                added += 1

        log(f"  scoring {len(merged)} candidates (variant + shipping lookups, ~1/sec)...")
        enriched = []
        for p in merged:
            scored = _score_product(client, p, cat["name"], cat["target_retail"], destination_countries, markup, log)
            if scored:
                enriched.append(scored)
                log(
                    f"    OK: {scored['name'][:50]:<50} cost=${scored['cost']:.2f} "
                    f"worst-case margin={scored['worst_case_margin_pct']}%"
                )

        enriched.sort(key=lambda r: (r["worst_case_margin_pct"], r["listed_num"]), reverse=True)
        top = enriched[:TOP_PER_CATEGORY]
        categories_out.append({"name": cat["name"], "products": top})
        all_products.extend(top)

    all_products.sort(key=lambda r: (r["worst_case_margin_pct"], r["listed_num"]), reverse=True)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "categories": categories_out,
        "top_overall": all_products[:TOP_OVERALL],
    }
    DATA_DIR.mkdir(exist_ok=True)
    SCAN_RESULT_FILE.write_text(json.dumps(result, indent=2))
    return result


def load_last_scan() -> dict | None:
    if SCAN_RESULT_FILE.exists():
        return json.loads(SCAN_RESULT_FILE.read_text())
    return None
