import io
import json
import os
import re
import threading
import zipfile
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app import db
from app.cj_client import CJClient, CJAPIError, summarize_variants, product_url_for
from app.db import DBError
from app.gemini_client import (
    generate_instagram_content,
    generate_collection_content,
    build_instagram_prompt,
    build_collection_prompt,
    estimate_market_price,
    GeminiAPIError,
)
from app.image_export import export_image, export_media, ConversionError, UnknownCanvasError
from app.instagram_specs import check_image_for_instagram, check_video_for_instagram
from app.pinterest_specs import check_image_for_pinterest
from app.picker import run_scan, load_last_scan, _score_product, season_for_category, CATEGORIES

PLATFORMS = ("instagram", "pinterest")

load_dotenv()

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

app = FastAPI(title="Mirthovia Product Scanner")

CJ_API_KEY = os.getenv("CJ_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
DESTINATION_COUNTRIES = [c.strip() for c in os.getenv("DESTINATION_COUNTRIES", "IN").split(",") if c.strip()]
CUTENESS_KEYWORDS = [k.strip().lower() for k in os.getenv("CUTENESS_KEYWORDS", "cute,kawaii,pastel").split(",") if k.strip()]
USD_TO_INR = float(os.getenv("USD_TO_INR", "88.0"))


def _with_inr(product: dict, rate: float) -> dict:
    """Returns a copy of a scored product dict with _inr companion fields for
    every USD figure, computed at render time (not cached) so changing
    USD_TO_INR in .env takes effect without re-running the whole scan."""
    p = dict(product)
    p["cost_inr"] = round(p["cost"] * rate)
    p["retail_inr"] = round(p["retail"] * rate)
    lo, hi = p["shipping_range"]
    p["shipping_range_inr"] = (round(lo * rate), round(hi * rate))
    worst = min(p["per_country"].values(), key=lambda v: v["margin_pct"])
    p["worst_case_margin_usd"] = worst["margin"]
    p["worst_case_margin_inr"] = round(worst["margin"] * rate)
    # fall back to recomputing if this is a cached scan from before the
    # breakeven field existed, rather than crashing the whole dashboard
    breakeven = p.get("breakeven", round(p["cost"] + hi, 2))
    p["breakeven"] = breakeven
    p["breakeven_inr"] = round(breakeven * rate)
    return p

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MAX_LOG_ENTRIES = 200


def _load_fetch_log() -> list:
    try:
        return db.load_fetch_log()
    except DBError:
        return []


def _log_fetch_attempt(link: str, pid: str | None, status: str, name: str | None = None, error: str | None = None):
    """One row per distinct product/link, most-recent-first - a retry
    updates its existing row in place (and moves it to the top) instead of
    piling up duplicate rows for the same product every time you retry it."""
    key = pid or link
    db.log_fetch_attempt(key, {
        "link": link,
        "pid": pid,
        "status": status,
        "name": name,
        "error": error,
        "fetched_at": datetime.now().isoformat(),
    })


def _extract_pid(link: str) -> str | None:
    """Accepts a raw CJ pid or a product page URL like
    .../product/some-slug-p-1779495048112394240.html - pulls the pid out of
    either. Most CJ pids are a long numeric string, but some products use a
    GUID-style id instead (confirmed live) - a bare token with no slashes
    or spaces is trusted directly rather than requiring digits-only, so
    those still work, while a multi-word search phrase (which always has
    spaces) still correctly falls through to None."""
    link = link.strip()
    if link.isdigit():
        return link
    m = re.search(r"-p-(\d{10,})\.html", link)
    if m:
        return m.group(1)
    m = re.search(r"(\d{10,})", link)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9-]{8,}", link):
        return link
    return None


def _load_posted() -> dict:
    try:
        posted = db.load_posted()
    except DBError:
        return {}
    # Older entries only had a single "posted_at" (back when there was one
    # platform) - treat that as "posted to Instagram" rather than losing it.
    for entry in posted.values():
        if "platforms" not in entry:
            entry["platforms"] = {"instagram": entry.get("posted_at"), "pinterest": None}
    return posted


def _set_posted(pid: str, name: str, platform: str, posted: bool):
    if platform not in PLATFORMS:
        raise ValueError(f"Unknown platform: {platform}")
    db.set_posted(pid, name, platform, posted)


def _set_added_to_store(pid: str, name: str, added: bool):
    db.set_added_to_store(pid, name, added)


def _set_rejected(pid: str, name: str, rejected: bool):
    db.set_rejected(pid, name, rejected)


_client: CJClient | None = None


def get_client() -> CJClient:
    global _client
    if _client is None:
        _client = CJClient(api_key=CJ_API_KEY)
    return _client


# --- Background scan job ---------------------------------------------------

SCAN_STATE = {"status": "idle", "logs": [], "error": None, "data": load_last_scan()}
SCAN_LOCK = threading.Lock()


def _scan_log(msg: str):
    with SCAN_LOCK:
        SCAN_STATE["logs"].append(msg)
    print(msg)


def _run_scan_job(categories: list[dict] | None = None):
    with SCAN_LOCK:
        SCAN_STATE["status"] = "running"
        SCAN_STATE["logs"] = []
        SCAN_STATE["error"] = None
    try:
        data = run_scan(get_client(), CUTENESS_KEYWORDS, DESTINATION_COUNTRIES, log=_scan_log, categories=categories)
        with SCAN_LOCK:
            SCAN_STATE["status"] = "done"
            SCAN_STATE["data"] = data
        _scan_log("Scan complete.")
    except Exception as e:
        # Catch-all, not just (CJAPIError, ValueError): an unexpected bug here
        # must still flip status away from "running", or the dashboard is
        # stuck showing "running" forever with no way to tell it actually died.
        import traceback

        with SCAN_LOCK:
            SCAN_STATE["status"] = "error"
            SCAN_STATE["error"] = str(e)
        _scan_log(f"ERROR: {e}")
        _scan_log(traceback.format_exc())


def _validate_custom_categories(raw: list) -> list[dict]:
    cleaned = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        keywords = [k.strip() for k in (entry.get("keywords") or []) if isinstance(k, str) and k.strip()]
        if not name or not keywords:
            continue
        cat = {"name": name, "keywords": keywords}
        if entry.get("season") in ("winter", "summer", "all"):
            cat["season"] = entry["season"]
        if isinstance(entry.get("saturation_threshold"), (int, float)):
            cat["saturation_threshold"] = int(entry["saturation_threshold"])
        if isinstance(entry.get("require_cute"), bool):
            cat["require_cute"] = entry["require_cute"]
        cleaned.append(cat)
    return cleaned


@app.get("/scan/categories")
def scan_categories():
    """The scan's default category/keyword/season list (from
    scan_categories.json), so the dashboard's "customize this scan" form
    has real defaults to pre-fill instead of starting from nothing."""
    return {"categories": CATEGORIES}


@app.post("/scan/run")
async def start_scan(request: Request):
    with SCAN_LOCK:
        if SCAN_STATE["status"] == "running":
            return JSONResponse({"ok": False, "message": "A scan is already running."}, status_code=409)

    categories = None
    try:
        body = await request.json()
    except Exception:
        body = None
    if isinstance(body, dict) and isinstance(body.get("categories"), list):
        categories = _validate_custom_categories(body["categories"])
        if not categories:
            return JSONResponse({"ok": False, "message": "No valid categories - each needs a name and at least one keyword."}, status_code=400)

    threading.Thread(target=_run_scan_job, args=(categories,), daemon=True).start()
    return {"ok": True}


@app.get("/scan/status")
def scan_status():
    with SCAN_LOCK:
        return {
            "status": SCAN_STATE["status"],
            "logs": SCAN_STATE["logs"][-200:],
            "error": SCAN_STATE["error"],
            "has_results": SCAN_STATE["data"] is not None,
        }


@app.get("/search-cj")
def search_cj(q: str):
    """Raw CJ product search by name, bypassing the scan's relevance/
    cuteness/off-brand/saturation filters entirely - for when the
    automated pipeline's filters exclude something you can see is actually
    fine, or you just want to browse what's on CJ for a term. Results here
    are added individually via /add-link (same pid-or-link acceptance),
    not scored/saved until you pick one."""
    q = q.strip()
    if not q:
        return JSONResponse({"error": "Enter a search term."}, status_code=400)
    try:
        raw = get_client().search_products(q, size=50)
    except CJAPIError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    results = [
        {
            "pid": str(p.get("id")),
            "name": p.get("nameEn"),
            "image": p.get("bigImage"),
            "cost": p.get("sellPrice"),
            "listed_num": p.get("listedNum"),
        }
        for p in raw
    ]
    try:
        db.log_search_results(q, results)
    except DBError:
        pass  # logging is best-effort - never block showing results over it
    return {"results": results}


@app.post("/add-link")
def add_link(link: str = Form(...), return_to: str = Form("/")):
    """Fetches one product directly from a pasted CJ link or raw pid, scores
    it with real cost/shipping data (same scoring as a scan), and stores it
    so /product/{pid} works for it - for grabbing photos/video to post
    without waiting on or depending on a full category scan. Every attempt
    (success or failure) is logged to /fetched so a failed fetch isn't just
    a one-off error message you lose - it's a row you can retry."""
    pid = _extract_pid(link)
    if not pid:
        _log_fetch_attempt(link, None, "error", error="Couldn't find a product ID in that link")
        return RedirectResponse(f"{return_to}?add_error=Couldn%27t+find+a+product+ID+in+that+link", status_code=303)

    existing = _find_product(pid)
    if existing:
        _log_fetch_attempt(link, pid, "ok", name=existing.get("name"))
        return RedirectResponse(f"/product/{pid}", status_code=303)

    try:
        client = get_client()
        detail = _cached_product_data(pid)["detail"]
        name = detail.get("productNameEn") or detail.get("productName") or pid
        raw = {
            "id": pid,
            "nameEn": name,
            "bigImage": detail.get("bigImage"),
            "isVideo": bool(detail.get("productVideo")),
            "sellPrice": detail.get("sellPrice"),
            "listedNum": detail.get("listedNum"),
        }
        scored = _score_product(client, raw, "Manually Added", DESTINATION_COUNTRIES, log=print)
    except (CJAPIError, DBError) as e:
        _log_fetch_attempt(link, pid, "error", error=str(e))
        return RedirectResponse(f"{return_to}?add_error={e}", status_code=303)

    if not scored:
        error = "Couldn't get a usable variant or shipping quote for that product"
        _log_fetch_attempt(link, pid, "error", name=name, error=error)
        return RedirectResponse(f"{return_to}?add_error={error.replace(' ', '+')}", status_code=303)

    db.upsert_product(pid, scored, source="manual")
    _log_fetch_attempt(link, pid, "ok", name=scored.get("name"))
    return RedirectResponse(f"/product/{pid}", status_code=303)


@app.get("/fetched", response_class=HTMLResponse)
def fetched_log(request: Request):
    try:
        already_added = {str(p["pid"]) for p in db.get_all_products()}
        not_added = [r for r in db.get_search_log() if str(r.get("_id")) not in already_added]
        for r in not_added:
            r["product_url"] = product_url_for(r["_id"], r.get("name") or r["_id"])
    except DBError:
        not_added = []
    try:
        scan_runs = db.get_scan_runs()
    except DBError:
        scan_runs = []
    return templates.TemplateResponse(
        "fetched.html",
        {"request": request, "entries": _load_fetch_log(), "not_added": not_added, "scan_runs": scan_runs},
    )


@app.get("/fetched/scan/{run_id}", response_class=HTMLResponse)
def scan_run_detail(request: Request, run_id: str):
    run = db.get_scan_run(run_id)
    if not run:
        return templates.TemplateResponse(
            "scan_run.html",
            {"request": request, "run_id": run_id, "run": None},
        )
    posted = _load_posted()
    for cat in run.get("categories", []):
        for p in cat["products"]:
            p = _with_inr(p, USD_TO_INR)
            entry = posted.get(str(p["pid"]), {})
            p["rejected"] = entry.get("rejected")
            p["added_to_store"] = entry.get("added_to_store")
    return templates.TemplateResponse(
        "scan_run.html",
        {"request": request, "run_id": run_id, "run": run},
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with SCAN_LOCK:
        data = SCAN_STATE["data"]
        status = SCAN_STATE["status"]
        error = SCAN_STATE["error"]
        logs = list(SCAN_STATE["logs"][-200:])

    # SCAN_STATE["data"] is only ever set once at server startup (from
    # load_last_scan()) or when a scan finishes - if MongoDB happened to be
    # briefly unreachable at that exact startup moment (confirmed this
    # happens, e.g. an Atlas SSL handshake hiccup), data stays None forever
    # and the dashboard shows "No scan results yet" even though real data
    # is sitting in Mongo. Retry the load here instead of only once ever.
    if data is None and status != "running":
        data = load_last_scan()
        if data is not None:
            with SCAN_LOCK:
                SCAN_STATE["data"] = data

    generated_at = data.get("generated_at") if data else None

    posted = _load_posted()
    try:
        first_seen_dates = db.get_first_seen_dates()
    except DBError:
        first_seen_dates = {}

    def _enrich(p):
        p = _with_inr(p, USD_TO_INR)
        entry = posted.get(str(p["pid"]), {})
        platforms = entry.get("platforms", {})
        p["posted_instagram"] = platforms.get("instagram")
        p["posted_pinterest"] = platforms.get("pinterest")
        p["added_to_store"] = entry.get("added_to_store")
        p["rejected"] = entry.get("rejected")
        p["season"] = season_for_category(p["category"])
        p["found_at"] = first_seen_dates.get(str(p["pid"]))
        return p

    # "All products" is everything ever found, from every source, combined
    # - not just the latest scan's top-10-per-category trim. A default
    # scan, a customized scan, a search-and-add, and a pasted link all go
    # through the same upsert_product() call, so get_all_products() (one
    # deduplicated entry per pid, its latest snapshot) already covers every
    # one of them without needing separate per-source merging.
    try:
        all_products = [_enrich(p) for p in db.get_all_products()]
    except DBError:
        all_products = []

    categories_present = sorted({p["category"] for p in all_products})
    scan_dates_present = sorted(
        {p["found_at"][:10] for p in all_products if p.get("found_at")}, reverse=True
    )

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "generated_at": generated_at,
            "all_products": all_products,
            "scan_dates_present": scan_dates_present,
            "categories_present": categories_present,
            "status": status,
            "error": error,
            "initial_logs_json": json.dumps(logs),
            "usd_to_inr": USD_TO_INR,
            "add_error": request.query_params.get("add_error"),
        },
    )


@app.post("/posted/{pid}")
def mark_posted(
    pid: str,
    name: str = Form(...),
    platform: str = Form(...),
    posted: str = Form("true"),
    redirect_to: str = Form("/"),
):
    try:
        _set_posted(pid, name, platform, posted == "true")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except DBError as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return RedirectResponse(redirect_to, status_code=303)


@app.post("/added-to-store/{pid}")
def added_to_store(pid: str, name: str = Form(...), added: str = Form("true"), redirect_to: str = Form("/")):
    try:
        _set_added_to_store(pid, name, added == "true")
    except DBError as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return RedirectResponse(redirect_to, status_code=303)


@app.post("/reject/{pid}")
def reject_product(pid: str, name: str = Form(...), rejected: str = Form("true"), redirect_to: str = Form("/")):
    try:
        _set_rejected(pid, name, rejected == "true")
    except DBError as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return RedirectResponse(redirect_to, status_code=303)


@app.get("/download-image")
def download_image(url: str, filename: str = "mirthovia-product.jpg"):
    """Proxies a CJ product image back with a Content-Disposition header so
    the browser actually downloads it instead of just opening it (plain
    cross-origin <a download> links are unreliable for this)."""
    import requests as _requests

    r = _requests.get(url, stream=True, timeout=30)
    return StreamingResponse(
        r.iter_content(chunk_size=8192),
        media_type=r.headers.get("Content-Type", "image/jpeg"),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/export-image")
def export_image_route(url: str, platform: str, canvas: str, filename: str = "mirthovia-export.jpg"):
    """Actually crops + resizes the source photo to the platform's exact
    canvas size (not just a pass-through like /download-image) - e.g. a
    square CJ product photo center-cropped and resized to Pinterest's
    1000x1500 Standard Pin, ready to post as-is."""
    import requests as _requests

    r = _requests.get(url, timeout=30)
    r.raise_for_status()
    try:
        out_bytes = export_image(r.content, platform, canvas)
    except UnknownCanvasError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    return StreamingResponse(
        io.BytesIO(out_bytes),
        media_type="image/jpeg",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


@app.get("/convert", response_class=HTMLResponse)
def convert_page(request: Request):
    """A standalone Instagram/Pinterest resizer for any file you have on
    hand - not tied to a CJ product. Images go through the same blurred-
    letterbox export as a product photo; GIFs/videos go through ffmpeg
    instead (export_image only keeps a GIF's first frame and can't touch
    video at all)."""
    return templates.TemplateResponse("convert.html", {"request": request})


@app.post("/convert-media")
async def convert_media_route(
    file: UploadFile = File(...),
    platform: str = Form(...),
    canvas: str = Form(...),
):
    content = await file.read()
    if not content:
        return JSONResponse({"error": "That file came through empty."}, status_code=400)

    ext = os.path.splitext(file.filename or "")[1].lower() or ".jpg"
    try:
        if ext in IMAGE_EXTS:
            out_bytes = export_image(content, platform, canvas)
            out_ext = ".jpg"
        else:
            out_bytes, out_ext = export_media(content, ext, platform, canvas)
    except UnknownCanvasError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except ConversionError as e:
        return JSONResponse({"error": f"Conversion failed: {e}"}, status_code=500)

    media_type = "image/gif" if out_ext == ".gif" else "video/mp4" if out_ext == ".mp4" else "image/jpeg"
    return StreamingResponse(
        io.BytesIO(out_bytes),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="mirthovia-export{out_ext}"'},
    )


@app.get("/collection-download")
def collection_download_route(pids: list[str] = Query(...), variant: str = "original"):
    """Each selected product's main photo as its own file in one zip - for
    posting them together as an Instagram carousel (or building your own
    collage in Canva) without merging them into a single image yourself."""
    import requests as _requests

    if variant not in DOWNLOAD_VARIANTS:
        return JSONResponse({"error": f"Unknown variant: {variant}"}, status_code=400)
    platform_canvas = DOWNLOAD_VARIANTS[variant]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, pid in enumerate(pids, start=1):
            product = _find_product(pid)
            if not product or not product.get("image"):
                continue
            try:
                r = _requests.get(product["image"], timeout=30)
                r.raise_for_status()
                content = r.content
            except Exception:
                continue

            if platform_canvas:
                try:
                    content = export_image(content, *platform_canvas)
                    ext = ".jpg"
                except Exception:
                    continue  # skip a photo that fails to export rather than fail the whole zip
            else:
                ext = ".png" if product["image"].lower().endswith(".png") else ".jpg"

            zf.writestr(f"{i:02d}-{_safe_filename(product['name'])}{ext}", content)

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="mirthovia-collection-photos.zip"'},
    )


@app.post("/collection-caption")
def collection_caption_route(pids: list[str] = Form(...)):
    """One combined Gemini caption for posting the selected products
    together (as an Instagram carousel), instead of one caption each."""
    names = [p["name"] for pid in pids if (p := _find_product(pid))]
    if not names:
        return JSONResponse({"error": "None of the selected products were found."}, status_code=404)
    try:
        return generate_collection_content(api_key=GEMINI_API_KEY, product_names=names)
    except (GeminiAPIError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/collection-caption-prompt")
def collection_caption_prompt_route(pids: list[str] = Form(...)):
    """Same prompt as /collection-caption, without calling Gemini at all -
    for pasting into ChatGPT or any other AI when Gemini itself is down."""
    names = [p["name"] for pid in pids if (p := _find_product(pid))]
    if not names:
        return JSONResponse({"error": "None of the selected products were found."}, status_code=404)
    return {"prompt": build_collection_prompt(names)}


DOWNLOAD_VARIANTS = {
    "original": None,
    "instagram-square": ("instagram", "square"),
    "instagram-portrait": ("instagram", "portrait"),
    "pinterest-standard": ("pinterest", "standard"),
    "pinterest-square": ("pinterest", "square"),
}


@app.get("/download-all/{pid}")
def download_all_images(
    pid: str,
    variant: str = "original",
    include_video: bool = False,
    indices: list[str] = Query(default=[]),
):
    """One zip, built from whichever photos were checked on the page (all by
    default), exported to whichever platform size was picked."""
    import requests as _requests

    if variant not in DOWNLOAD_VARIANTS:
        return JSONResponse({"error": f"Unknown variant: {variant}"}, status_code=400)
    platform_canvas = DOWNLOAD_VARIANTS[variant]

    product = _find_product(pid)
    name = product["name"] if product else pid
    fallback_image = product.get("image") if product else None
    images, video_url, error = _fetch_media(pid, fallback_image)

    if not images:
        return JSONResponse({"error": error or "No images found for this product."}, status_code=404)

    if indices:
        wanted = {int(i) for i in indices if i.isdigit()}
        images = [img for i, img in enumerate(images, start=1) if i in wanted]
        if not images:
            return JSONResponse({"error": "No photos selected."}, status_code=400)

    safe_name = _safe_filename(name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, url in enumerate(images, start=1):
            try:
                r = _requests.get(url, timeout=30)
                r.raise_for_status()
                content = r.content
            except Exception:
                continue

            if platform_canvas:
                try:
                    content = export_image(content, *platform_canvas)
                    ext = ".jpg"
                except Exception:
                    continue  # skip a photo that fails to export rather than fail the whole zip
            else:
                lower = url.lower()
                ext = ".png" if lower.endswith(".png") else ".webp" if lower.endswith(".webp") else ".jpg"

            zf.writestr(f"{safe_name}-{i:02d}{ext}", content)

        if include_video and video_url:
            try:
                r = _requests.get(video_url, timeout=60)
                r.raise_for_status()
                lower = video_url.lower()
                ext = ".mov" if lower.endswith(".mov") else ".mp4"
                zf.writestr(f"{safe_name}-video{ext}", r.content)
            except Exception:
                pass  # photos in the zip still matter even if the video fetch fails

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}-photos.zip"'},
    )


def _first_truthy(*values):
    for v in values:
        if v:
            return v
    return None


def _find_product(pid: str) -> dict | None:
    """The `products` collection is the single canonical, deduplicated store
    for every product ever captured (scan or manual fetch) - check it first.
    Only fall back to the in-memory last-scan data if the DB itself is
    unreachable, so a Mongo outage doesn't take down product pages you'd
    otherwise still be able to see from this session's scan."""
    try:
        found = db.get_product(pid)
        if found:
            return found
    except DBError:
        pass

    with SCAN_LOCK:
        data = SCAN_STATE["data"]
    if data:
        for p in data.get("top_overall", []):
            if str(p["pid"]) == str(pid):
                return p
        for cat in data.get("categories", []):
            for p in cat["products"]:
                if str(p["pid"]) == str(pid):
                    return p
    return None


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9\-_]+", "_", name or "").strip("_")
    return cleaned[:60] or "product"


def _cached_product_data(pid: str) -> dict:
    """CJ rate-limits each API endpoint to ~1000 calls/day - don't waste them
    re-fetching a product's detail/variants every time you reopen its page,
    add it to store, or revisit something already posted. One cache miss per
    product fetches both detail + variants together; every call after that
    (forever, unless the DB doc is deleted) reads from Mongo with zero API
    calls. Product photos/variants essentially never change day to day, so
    there's no real downside to caching indefinitely."""
    cached = db.get_product_cache(pid)
    if cached and "detail" in cached:
        return cached

    client = get_client()
    detail = client.get_product_detail(pid)
    variants = client.get_variants(pid)
    cached = {"detail": detail, "variants": variants, "cached_at": datetime.now().isoformat()}
    db.save_product_cache(pid, cached)
    return cached


def _cached_image_checks(pid: str, images: list[str]) -> list[dict]:
    """Checking every photo against Instagram's + Pinterest's format specs
    means downloading each one in full (twice - once per platform) - without
    caching that happens on EVERY page view, which is why reopening a
    product with 15+ photos feels like it's re-fetching from scratch every
    time. Cache the results in the same product_cache doc, keyed to this
    exact image list; only recompute if the gallery itself changed."""
    cached = db.get_product_cache(pid) or {}

    if cached.get("image_checks_for") == images and cached.get("image_checks"):
        return cached["image_checks"]

    checks = [
        {"url": img, "instagram": check_image_for_instagram(img), "pinterest": check_image_for_pinterest(img)}
        for img in images
    ]
    cached["image_checks"] = checks
    cached["image_checks_for"] = images
    db.save_product_cache(pid, cached)
    return checks


def _fetch_media(pid: str, fallback_image: str | None) -> tuple[list[str], str | None, str | None]:
    """Shared by the detail page and the ZIP download - keeps productImageSet's
    own order (matches CJ's actual product-page gallery order), only falling
    back to a single image if the gallery came back empty."""
    error = None
    images = []
    video_url = None
    try:
        detail = _cached_product_data(pid)["detail"]
        images = detail.get("productImageSet") or detail.get("newImgList") or []
        if not images:
            single = _first_truthy(detail.get("bigImage"), detail.get("img"), detail.get("bigimg"), fallback_image)
            images = [single] if single else []
        video_url = _first_truthy(detail.get("productVideo"), detail.get("video"))
        if not video_url:
            video_list = detail.get("videoList") or []
            video_url = video_list[0] if video_list else None
    except CJAPIError as e:
        error = str(e)
        if fallback_image:
            images = [fallback_image]
    return images, video_url, error


@app.get("/product/{pid}", response_class=HTMLResponse)
def product_detail(request: Request, pid: str):
    """The single consolidated 'everything for this one product' page:
    full image gallery + video (with Instagram format checks on each),
    Gemini-written description/caption/5 hashtags/video script, pricing
    stats, the price checker, and mark-as-posted - replacing the old
    scattered /media + /caption pages."""
    product = _find_product(pid)
    if not product:
        return templates.TemplateResponse(
            "product_detail.html",
            {"request": request, "error": "Product not found in the last scan - it may have expired from cache. Run a new scan.", "product": None},
        )

    product = _with_inr(product, USD_TO_INR)
    posted = _load_posted()
    posted_entry = posted.get(str(product["pid"]), {})
    platforms = posted_entry.get("platforms", {})
    product["posted_instagram"] = platforms.get("instagram")
    product["posted_pinterest"] = platforms.get("pinterest")
    product["added_to_store"] = posted_entry.get("added_to_store")
    product["rejected"] = posted_entry.get("rejected")

    images, video_url, media_error = _fetch_media(pid, product.get("image"))
    image_checks = _cached_image_checks(pid, images)
    video_check = check_video_for_instagram(video_url) if video_url else None
    best_image_index = next((i for i, c in enumerate(image_checks) if c["instagram"]["verdict"] == "good"), None)

    variants_error = None
    variants = []
    try:
        raw_variants = _cached_product_data(pid)["variants"]
        variants = summarize_variants(raw_variants)
        for v in variants:
            v["cost_inr"] = round(v["cost"] * USD_TO_INR)
    except CJAPIError as e:
        variants_error = str(e)

    return templates.TemplateResponse(
        "product_detail.html",
        {
            "request": request,
            "error": None,
            "product": product,
            "media_error": media_error,
            "image_checks": image_checks,
            "best_image_index": best_image_index,
            "video_url": video_url,
            "video_check": video_check,
            "variants": variants,
            "variants_error": variants_error,
        },
    )


@app.post("/product/{pid}/generate-content")
def generate_content(pid: str):
    """Only called when you click the button - Gemini is NOT hit just from
    viewing a product page, so browsing/pricing never depends on it being up."""
    product = _find_product(pid)
    if not product:
        return JSONResponse({"error": "Product not found."}, status_code=404)
    try:
        content = generate_instagram_content(
            api_key=GEMINI_API_KEY,
            product_name=product["name"],
            category=product["category"],
            retail_price=product["retail"],
        )
        return content
    except (GeminiAPIError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/product/{pid}/generate-content-prompt")
def generate_content_prompt(pid: str):
    """Same prompt as /generate-content, without calling Gemini at all -
    for pasting into ChatGPT or any other AI when Gemini itself is down."""
    product = _find_product(pid)
    if not product:
        return JSONResponse({"error": "Product not found."}, status_code=404)
    prompt = build_instagram_prompt(product["name"], product["category"], product["retail"])
    return {"prompt": prompt}


@app.post("/product/{pid}/estimate-price")
def estimate_price(pid: str):
    """Free, no-billing Gemini price estimate - from training knowledge, NOT
    live search (that's billing-gated). Labeled clearly in the UI as an
    estimate to verify, not confirmed market research."""
    product = _find_product(pid)
    if not product:
        return JSONResponse({"error": "Product not found."}, status_code=404)
    try:
        result = estimate_market_price(
            api_key=GEMINI_API_KEY,
            product_name=product["name"],
            category=product["category"],
            cost=product["cost"],
        )
        return result
    except (GeminiAPIError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=502)


