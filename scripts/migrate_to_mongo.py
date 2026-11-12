"""
One-time migration: copies whatever's still in data/*.json and
data/product_cache/*.json into MongoDB Atlas, so switching storage backends
doesn't lose the real data you already have (your manually-fetched products,
fetch history, product cache). Safe to run more than once - every write here
is an upsert, so re-running just re-syncs the same data.

Usage (after setting MONGODB_URI in .env):
    python3 scripts/migrate_to_mongo.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from app import db  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def migrate_posted():
    f = DATA_DIR / "posted_products.json"
    if not f.exists():
        return 0
    data = json.loads(f.read_text())
    n = 0
    for pid, entry in data.items():
        platforms = entry.get("platforms") or {"instagram": entry.get("posted_at"), "pinterest": None}
        db.get_db().posted_products.replace_one(
            {"_id": str(pid)}, {"_id": str(pid), "name": entry.get("name"), "platforms": platforms}, upsert=True
        )
        n += 1
    return n


def migrate_manual_products():
    f = DATA_DIR / "manual_products.json"
    if not f.exists():
        return 0
    data = json.loads(f.read_text())
    for pid, product in data.items():
        db.upsert_product(pid, product, source="manual")
    return len(data)


def migrate_fetch_log():
    f = DATA_DIR / "manual_fetch_log.json"
    if not f.exists():
        return 0
    entries = json.loads(f.read_text())
    for e in entries:
        key = e.get("pid") or e.get("link")
        db.log_fetch_attempt(key, e)
    return len(entries)


def migrate_product_cache():
    d = DATA_DIR / "product_cache"
    if not d.exists():
        return 0
    n = 0
    for f in d.glob("*.json"):
        pid = f.stem
        cached = json.loads(f.read_text())
        db.save_product_cache(pid, cached)
        n += 1
    return n


def migrate_last_scan():
    f = DATA_DIR / "last_scan.json"
    if not f.exists():
        return False
    data = json.loads(f.read_text())
    if not data.get("top_overall") and not data.get("categories"):
        return False  # empty/wiped scan result - nothing worth keeping
    db.save_last_scan(data)
    return True


if __name__ == "__main__":
    print(f"posted_products:     {migrate_posted()} migrated")
    print(f"manual_products:     {migrate_manual_products()} migrated")
    print(f"manual_fetch_log:    {migrate_fetch_log()} migrated")
    print(f"product_cache:       {migrate_product_cache()} migrated")
    print(f"last_scan:           {'migrated' if migrate_last_scan() else 'skipped (empty)'}")
    print("Done.")
