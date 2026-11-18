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
    set_posted(pid, name, platform, posted=True)


def set_posted(pid: str, name: str, platform: str, posted: bool):
    db = get_db()
    db.posted_products.update_one(
        {"_id": str(pid)},
        {"$set": {"name": name, f"platforms.{platform}": datetime.now().isoformat() if posted else None}},
        upsert=True,
    )


def mark_added_to_store(pid: str, name: str):
    set_added_to_store(pid, name, added=True)


def set_added_to_store(pid: str, name: str, added: bool):
    db = get_db()
    db.posted_products.update_one(
        {"_id": str(pid)},
        {"$set": {"name": name, "added_to_store": datetime.now().isoformat() if added else None}},
        upsert=True,
    )


def set_rejected(pid: str, name: str, rejected: bool):
    db = get_db()
    db.posted_products.update_one(
        {"_id": str(pid)},
        {"$set": {"name": name, "rejected": datetime.now().isoformat() if rejected else None}},
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


def get_first_seen_dates() -> dict:
    """pid -> first_seen_at, so the dashboard can show which scan run first
    turned up a given product instead of just one blanket 'last scan' date."""
    docs = get_db().products.find({}, {"first_seen_at": 1})
    return {d["_id"]: d["first_seen_at"] for d in docs}


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


# --- CJ search results (not scored/added, just a record of what was seen) ---

def log_search_results(query: str, results: list[dict]):
    """Every raw CJ search hit gets saved here, even ones never clicked
    "+ Add" - so nothing you searched is lost, without it cluttering "All
    products" (which only ever reads the scored `products` collection,
    populated by /add-link - searching alone never writes there)."""
    if not results:
        return
    db = get_db()
    now = datetime.now().isoformat()
    for r in results:
        pid = r.get("pid")
        if not pid:
            continue
        db.search_log.update_one(
            {"_id": str(pid)},
            {"$set": {**r, "last_query": query, "last_seen_at": now}},
            upsert=True,
        )


def get_search_log() -> list:
    return list(get_db().search_log.find().sort("last_seen_at", -1))


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


# --- per-run scan history (going forward only - older scans were never
# tagged with a run id, so they only live in `products`/`last_scan`) -------

def save_scan_run(run_id: str, data: dict):
    db = get_db()
    db.scan_runs.replace_one({"_id": run_id}, {**data, "_id": run_id}, upsert=True)


def get_scan_runs() -> list:
    """Summary only (no product lists) - for listing past runs without
    pulling every product in every one of them into memory just to show a
    list of timestamps."""
    docs = get_db().scan_runs.find(
        {}, {"started_at": 1, "finished_at": 1, "categories.name": 1, "categories.products.pid": 1}
    ).sort("started_at", -1)
    out = []
    for d in docs:
        out.append({
            "run_id": d["_id"],
            "started_at": d.get("started_at"),
            "finished_at": d.get("finished_at"),
            "categories": [
                {"name": c["name"], "count": len(c.get("products", []))}
                for c in d.get("categories", [])
            ],
            "total": sum(len(c.get("products", [])) for c in d.get("categories", [])),
        })
    return out


def get_scan_run(run_id: str) -> dict | None:
    doc = get_db().scan_runs.find_one({"_id": run_id})
    if doc:
        doc.pop("_id", None)
    return doc
