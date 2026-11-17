"""MongoDB Atlas-backed storage for all app data. Requires MONGODB_URI in .env."""
import os
from datetime import datetime

from pymongo import MongoClient
from pymongo.errors import PyMongoError

DB_NAME = "mirthovia"

_client: MongoClient | None = None
_db = None


class DBError(Exception):
    pass


def get_db():
    global _client, _db
    if _db is not None:
        return _db

    uri = os.getenv("MONGODB_URI", "")
    if not uri:
        raise DBError(
            "MONGODB_URI is not set in .env - create a free cluster at "
            "mongodb.com/cloud/atlas, get its connection string, and add it "
            "as MONGODB_URI in .env."
        )
    try:
        _client = MongoClient(uri, serverSelectionTimeoutMS=8000)
        _db = _client[DB_NAME]
        _client.admin.command("ping")
    except PyMongoError as e:
        _client = None
        _db = None
        raise DBError(f"Couldn't connect to MongoDB: {e}") from e
    return _db


# --- posted (per-platform posting status) -----------------------------------

def load_posted() -> dict:
    db = get_db()
    out = {}
    for doc in db.posted_products.find():
        pid = doc.pop("_id")
        out[pid] = doc
    return out


def mark_posted(pid: str, name: str, platform: str):
    db = get_db()
    db.posted_products.update_one(
        {"_id": str(pid)},
        {"$set": {"name": name, f"platforms.{platform}": datetime.now().isoformat()}},
        upsert=True,
    )


# --- products: one deduplicated history per pid, across scan + manual fetch -

def upsert_product(pid: str, snapshot: dict, source: str) -> dict:
    db = get_db()
    pid = str(pid)
    now = datetime.now().isoformat()
    existing = db.products.find_one({"_id": pid})

    if existing is None:
        db.products.insert_one({
            "_id": pid,
            "first_seen": snapshot,
            "first_seen_at": now,
            "latest": snapshot,
            "latest_source": source,
            "history": [{"snapshot": snapshot, "recorded_at": now, "source": source}],
        })
        return snapshot

    if existing.get("latest") == snapshot:
        return existing["latest"]

    db.products.update_one(
        {"_id": pid},
        {
            "$set": {"latest": snapshot, "latest_source": source},
            "$push": {"history": {"snapshot": snapshot, "recorded_at": now, "source": source}},
        },
    )
    return snapshot


def get_product(pid: str) -> dict | None:
    doc = get_db().products.find_one({"_id": str(pid)})
    return doc["latest"] if doc else None


def get_product_history(pid: str) -> dict | None:
    doc = get_db().products.find_one({"_id": str(pid)})
    if not doc:
        return None
    return {"first_seen": doc["first_seen"], "first_seen_at": doc["first_seen_at"], "history": doc["history"]}


def get_all_products() -> list:
    return [d["latest"] for d in get_db().products.find()]


# --- fetch-by-link history ---------------------------------------------------

def load_fetch_log() -> list:
    db = get_db()
    docs = list(db.manual_fetch_log.find().sort("fetched_at", -1))
    for d in docs:
        d.pop("_id", None)
    return docs


def log_fetch_attempt(key: str, entry: dict):
    db = get_db()
    db.manual_fetch_log.replace_one({"_id": key}, {**entry, "_id": key}, upsert=True)


# --- CJ product detail/variants/image-check cache ----------------------------

def get_product_cache(pid: str) -> dict | None:
    db = get_db()
    doc = db.product_cache.find_one({"_id": str(pid)})
    if doc:
        doc.pop("_id", None)
    return doc


def save_product_cache(pid: str, cached: dict):
    # $set, not replace_one - two concurrent requests for a brand-new pid
    # (e.g. opened in two tabs) can both read an empty/partial cache and
    # write back different subsets of fields (detail/variants vs
    # image_checks); replace_one would let whichever writes last erase the
    # other's fields, $set merges them instead.
    db = get_db()
    db.product_cache.update_one({"_id": str(pid)}, {"$set": cached}, upsert=True)


# --- last scan result ---------------------------------------------------------

def get_last_scan() -> dict | None:
    db = get_db()
    doc = db.scan_state.find_one({"_id": "last_scan"})
    if doc:
        doc.pop("_id", None)
    return doc


def save_last_scan(result: dict):
    db = get_db()
    db.scan_state.replace_one({"_id": "last_scan"}, {**result, "_id": "last_scan"}, upsert=True)
