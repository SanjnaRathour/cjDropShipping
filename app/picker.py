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
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from app import db
from app.cj_client import (
    CJClient,
    CJAPIError,
    is_seasonal,
    pick_representative_variant,
    product_url_for,
    to_float,
)
from app.db import DBError

# Mirthovia-relevant categories. Keywords are PLAIN category terms, not
# "cute X" / "kawaii X" - CJ product titles rarely contain the aesthetic word
# AND the category word as one contiguous phrase, so prefixing the search
# term with "cute"/"kawaii" made the relevance filter reject almost
# everything. The aesthetic is enforced separately by is_cute() below, using
# CUTENESS_KEYWORDS from .env - that's the only place "cute" should be checked.
#
# target_retail is a realistic fixed market price for that product type -
# using landed_cost x markup for everything would make every product's
# margin come out roughly identical regardless of what it actually sells
# for, which defeats the point of ranking by margin.
CATEGORIES = [
    {"name": "Hair Accessories", "keywords": ["hair clip", "hair tie", "hair band", "scrunchie"], "target_retail": 12.99},
    {"name": "Phone Accessories", "keywords": ["phone charm", "phone case"], "target_retail": 14.99},
    {"name": "Stationery & Desk", "keywords": ["stationery", "desk organizer"], "target_retail": 9.99},
    {"name": "Keychains & Charms", "keywords": ["keychain", "bag charm"], "target_retail": 8.99},
    # Clothing/jewelry run far more sellers-per-listing industry-wide on CJ
    # than small accessories do (confirmed live: 0/100 "dress"/"bracelet"/
    # "necklace" results pass the default 300 cutoff) - a higher threshold
    # here isn't "letting in more competition," it's the actual realistic
    # floor for these categories.
    {"name": "Jewelry", "keywords": ["ring", "earrings", "bracelet", "necklace"], "target_retail": 11.99, "saturation_threshold": 1500},
    {"name": "Dresses", "keywords": ["dress"], "target_retail": 29.99, "saturation_threshold": 1500},
    # Clothing titles almost never contain an explicit "cute/kawaii/pastel"
    # word even when the item clearly is (confirmed live: only 1/100
    # "blouse" results passed the cuteness filter) - style here is visual,
    # not a title keyword, so this category skips that check.
    {"name": "Tops & Blouses", "keywords": ["crop top", "blouse"], "target_retail": 19.99, "saturation_threshold": 1500, "require_cute": False},
    {"name": "Skirts", "keywords": ["skirt"], "target_retail": 22.99, "saturation_threshold": 1500},
    # CJ's search for a compound phrase like "christmas hair clip" mostly
    # ignores "christmas" and just returns generic hair clips (confirmed
    # live: 0/100 titles even contained the word) - search broad festive
    # terms instead and require the title separately mention an on-brand
    # accessory type, so results are both genuinely Christmas AND on-brand
    # (not the random holiday home-decor CJ's search otherwise surfaces).
    {
        "name": "Christmas & Festive",
        "keywords": ["christmas", "santa", "snowman", "snowflake"],
        "target_retail": 11.99,
        "allow_seasonal": True,
        "must_contain_any": ["hair clip", "hairpin", "earrings", "necklace", "bracelet", "ring", "keychain", "phone case", "charm"],
    },
    {"name": "Bags & Purses", "keywords": ["mini bag", "crossbody bag"], "target_retail": 15.99, "saturation_threshold": 1500, "require_cute": False},
    {"name": "Sweaters & Cardigans", "keywords": ["sweater", "cardigan"], "target_retail": 24.99, "saturation_threshold": 1500, "require_cute": False},
    {"name": "Sunglasses", "keywords": ["sunglasses"], "target_retail": 12.99, "saturation_threshold": 1500},
    {"name": "Socks & Legwear", "keywords": ["socks"], "target_retail": 9.99, "saturation_threshold": 1500},
]

TOP_PER_CATEGORY = 10
TOP_OVERALL = 30

# Category -> season, for the dashboard's season filter - not a separate
# scan, just a lookup by the category name already stored on every product.
CATEGORY_SEASON = {
    "Sweaters & Cardigans": "winter",
    "Socks & Legwear": "winter",
    "Christmas & Festive": "winter",
    "Dresses": "summer",
    "Skirts": "summer",
    "Sunglasses": "summer",
}


def season_for_category(category: str) -> str:
    return CATEGORY_SEASON.get(category, "all")

# listedNum = how many other sellers already carry this exact product on CJ.
# A real test found a product listed by 7,673 sellers - that's not "proven
# demand," it's "impossible to differentiate from." Anything past
# SATURATED_THRESHOLD gets excluded before we even spend a scoring API call
# on it, and the remaining competition level is shown on every product so
# you can judge it yourself rather than finding out after the fact.
SATURATED_THRESHOLD = 300


def competition_label(listed_num: int) -> str:
    if listed_num < 15:
        return "Fresh"
    if listed_num < 60:
        return "Growing"
    if listed_num < SATURATED_THRESHOLD:
        return "Established"
    return "Saturated"


def _has_word(text: str, word: str) -> bool:
    # Whole-word match, not plain substring - a naive `word in text` check
    # would let "ring" match inside "earring", "top" match inside "laptop",
    # "cat" match inside "category", "band" match inside "husband", etc.
    # Allows an optional trailing "s"/"es" so plain plurals ("dress"/"dresses",
    # "star"/"stars") still match without needing both forms listed.
    return re.search(r"(?<!\w)" + re.escape(word) + r"(?:es|s)?(?!\w)", text) is not None


def is_relevant(name: str, keyword: str) -> bool:
    # Require every word of the keyword to appear somewhere in the name (not
    # necessarily contiguous/in order) - a strict exact-phrase match was
    # rejecting nearly all real CJ titles, which rarely repeat a multi-word
    # search term verbatim (e.g. "Pearl Flower Hair Clip" doesn't contain the
    # literal substring "hair claw clip" even though it's clearly relevant).
    lname = (name or "").lower()
    return all(_has_word(lname, word) for word in keyword.strip().lower().split())


def is_cute(name: str, cuteness_keywords: list[str]) -> bool:
    if not cuteness_keywords:
        return True
    lname = (name or "").lower()
    return any(_has_word(lname, marker) for marker in cuteness_keywords)


# Clothing titles are noisier than small accessories - a single cuteness
# keyword match (e.g. "pink", "flower") isn't a reliable enough signal on its
# own, so these words hard-disqualify a product regardless of what else
# matched (e.g. "Sexy Slim Oblique Shoulder Skirt" matched a cuteness keyword
# but is clearly off-brand for Mirthovia).
OFF_BRAND_KEYWORDS = ["sexy", "lingerie", "bodycon", "see-through", "sheer", "fetish", "erotic"]


def is_off_brand(name: str) -> bool:
    lname = (name or "").lower()
    return any(_has_word(lname, word) for word in OFF_BRAND_KEYWORDS)


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

    # WED2C bills shipping to the buyer separately by default (confirmed:
    # its own "Estimated Earning" = price - cost, shipping not subtracted) -
    # so shipping is not a seller cost and must not reduce margin/breakeven.
    # It only decides which market-price floor to use below.
    worst_shipping = max(v["shipping_cost"] for v in per_country.values())
    landed_worst = round(cost + worst_shipping, 2)

    listed_num = int(p.get("listedNum") or 0)
    competition = competition_label(listed_num)
    is_saturated = competition == "Saturated"

    # A flat category target price assumes a normal market - once hundreds
    # of sellers are reselling the exact same CJ listing (confirmed live:
    # a $0.28-cost item the category target priced at $11.99, when other
    # sellers already have it at $1.50), that assumption breaks. Fall back
    # to the cost-based formula instead, same as when there's no category
    # target at all, so the suggestion scales with THIS item's real cost.
    if target_retail > landed_worst and not is_saturated:
        retail = round(target_retail, 2)
        retail_source = "category target price"
    else:
        retail = round(landed_worst * markup, 2)
        retail_source = f"{markup}x markup"
        max_retail = round(cost / 0.4, 2)  # cap at 60% margin - only the
        if retail > max_retail:            # markup fallback can run away
            retail = max_retail             # like this; a category target
            retail_source += ", capped at 60% margin"  # price is already realistic
        if is_saturated:
            retail_source += " (category price skipped - saturated)"

    for country, v in per_country.items():
        margin = round(retail - cost, 2)
        v["margin"] = margin
        v["margin_pct"] = round((margin / retail) * 100, 1) if retail else 0.0

    worst = min(per_country.values(), key=lambda v: v["margin_pct"])
    best = max(per_country.values(), key=lambda v: v["margin_pct"])

    if is_saturated:
        # Still worth surfacing the raw number on the price label itself,
        # not just the competition badge, since hundreds of other sellers
        # reselling the exact same listing means even this cost-based
        # number should be checked against what they're actually charging.
        retail_source += f" — ⚠ {listed_num} sellers already list this exact item, verify the real price before using"

    return {
        "pid": pid,
        "vid": vid,
        "name": name,
        "category": category_name,
        "image": p.get("bigImage"),
        "has_video": bool(p.get("isVideo")),
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
        # Minimum price that still breaks even - just cost, since shipping
        # is billed to the buyer separately, not absorbed by you.
        "breakeven": round(cost, 2),
        "listed_num": listed_num,
        "competition": competition,
    }


def run_scan(
    client: CJClient,
    cuteness_keywords: list[str],
    destination_countries: list[str],
    markup: float,
    log=print,
) -> dict:
    categories_out = []
    all_products = []

    # Check the database BEFORE spending 20-40 minutes on rate-limited CJ
    # calls across every category - a scan that can't save its result at
    # the end is pointless, and that failure needs to surface in seconds,
    # not after all the real work is already done and thrown away.
    try:
        known_pids = {str(p["pid"]) for p in db.get_all_products()}
    except DBError as e:
        raise DBError(f"Can't reach the database - aborting before wasting API calls: {e}") from e

    for cat in CATEGORIES:
        log(f"=== {cat['name']} ===")
        seen_ids = set()
        merged = []
        per_keyword_limit = max(2, -(-TOP_PER_CATEGORY // len(cat["keywords"])))

        for kw in cat["keywords"]:
            log(f"  searching '{kw}'...")
            try:
                raw = client.search_products(kw, size=100)  # CJ's max page size per the docs
            except CJAPIError as e:
                log(f"  [warn] search failed for '{kw}': {e}")
                continue

            cat_threshold = cat.get("saturation_threshold", SATURATED_THRESHOLD)
            must_contain_any = cat.get("must_contain_any")
            filtered = [
                p for p in raw
                if (cat.get("allow_seasonal") or not is_seasonal(p.get("nameEn")))
                and not is_off_brand(p.get("nameEn"))
                and is_relevant(p.get("nameEn"), kw)
                and (cat.get("require_cute", True) is False or is_cute(p.get("nameEn"), cuteness_keywords))
                and int(p.get("listedNum") or 0) < cat_threshold
                and (not must_contain_any or any(_has_word((p.get("nameEn") or "").lower(), w) for w in must_contain_any))
            ]
            log(f"    {len(raw)} returned, {len(filtered)} passed seasonal+off-brand+relevance+cuteness+competition filters")
            # Prefer products not already captured in a past scan/fetch, so
            # a daily re-scan surfaces new finds instead of the same top
            # results every time; within that, same demand-sweet-spot sort
            # as before (not brand new/unproven, not oversaturated).
            sweet_spot = cat_threshold / 2
            filtered.sort(key=lambda p: (
                str(p.get("id")) in known_pids,
                abs(int(p.get("listedNum") or 0) - sweet_spot),
            ))

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

        enriched.sort(key=lambda r: (-r["worst_case_margin_pct"], r["listed_num"]))
        top = enriched[:TOP_PER_CATEGORY]
        categories_out.append({"name": cat["name"], "products": top})
        all_products.extend(top)

    all_products.sort(key=lambda r: (-r["worst_case_margin_pct"], r["listed_num"]))

    if not all_products:
        # Zero products across every category almost always means CJ's
        # search endpoint itself is rate-limited/down for now (confirmed by
        # testing: other endpoints like product detail still worked fine
        # while search returned 0 for every keyword) - NOT a real "nothing
        # found" result. Raise instead of silently overwriting good cached
        # results from a previous successful scan with an empty one.
        raise CJAPIError(
            "Scan found 0 products in every category - this almost always means CJ's "
            "search endpoint is rate-limited right now (check if other calls like "
            "product detail still work), not that nothing matched. Previous scan "
            "results were kept, not overwritten. Try again later."
        )

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "categories": categories_out,
        "top_overall": all_products[:TOP_OVERALL],
    }

    # 20-40 minutes of rate-limited CJ calls just produced this - write it
    # to disk BEFORE touching Mongo, so a save failure (even a transient
    # one, like Atlas still propagating a new IP allowlist entry) never
    # throws away completed work. Retry the real save a couple times
    # before giving up, since that kind of failure is often transient.
    backup_file = Path(__file__).resolve().parent.parent / "data" / "last_scan_backup.json"
    backup_file.parent.mkdir(exist_ok=True)
    backup_file.write_text(json.dumps(result))

    last_err = None
    for attempt in range(3):
        try:
            db.save_last_scan(result)
            for p in all_products:
                db.upsert_product(p["pid"], p, source="scan")
            break
        except DBError as e:
            last_err = e
            log(f"  [warn] DB save failed (attempt {attempt + 1}/3): {e}")
            time.sleep(5)
    else:
        raise DBError(
            f"Scan finished but couldn't save to the database after 3 tries: {last_err}. "
            f"The result was NOT lost - it's saved locally at {backup_file}, retry the save "
            f"once the database is reachable again instead of re-running the whole scan."
        )

    backup_file.unlink(missing_ok=True)  # saved for real - the local copy was just insurance
    return result


def recover_backup() -> bool:
    """Pushes data/last_scan_backup.json (written by run_scan when the Mongo
    save itself failed) into the database, without re-scanning. Returns
    False if there's no backup waiting."""
    backup_file = Path(__file__).resolve().parent.parent / "data" / "last_scan_backup.json"
    if not backup_file.exists():
        return False
    result = json.loads(backup_file.read_text())
    db.save_last_scan(result)
    for cat in result.get("categories", []):
        for p in cat["products"]:
            db.upsert_product(p["pid"], p, source="scan")
    backup_file.unlink()
    return True


def load_last_scan() -> dict | None:
    try:
        return db.get_last_scan()
    except DBError:
        # Mongo isn't configured/reachable yet - this is called at import
        # time (main.py's SCAN_STATE init), so it must not crash the whole
        # app before the user's even had a chance to set MONGODB_URI.
        return None
