import json
import os
import threading
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.cj_client import CJClient, CJAPIError
from app.gemini_client import generate_instagram_caption, GeminiAPIError
from app.picker import run_scan, load_last_scan

load_dotenv()

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

app = FastAPI(title="Mirthovia Product Scanner")

CJ_API_KEY = os.getenv("CJ_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
DESTINATION_COUNTRIES = [c.strip() for c in os.getenv("DESTINATION_COUNTRIES", "IN").split(",") if c.strip()]
MARKUP_MULTIPLIER = float(os.getenv("MARKUP_MULTIPLIER", "3.5"))
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
POSTED_FILE = DATA_DIR / "posted_products.json"


def _load_posted() -> dict:
    if POSTED_FILE.exists():
        return json.loads(POSTED_FILE.read_text())
    return {}


def _mark_posted(pid: str, name: str):
    DATA_DIR.mkdir(exist_ok=True)
    posted = _load_posted()
    posted[str(pid)] = {"name": name, "posted_at": datetime.now().isoformat()}
    POSTED_FILE.write_text(json.dumps(posted, indent=2))


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


def _run_scan_job():
    with SCAN_LOCK:
        SCAN_STATE["status"] = "running"
        SCAN_STATE["logs"] = []
        SCAN_STATE["error"] = None
    try:
        data = run_scan(get_client(), CUTENESS_KEYWORDS, DESTINATION_COUNTRIES, MARKUP_MULTIPLIER, log=_scan_log)
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


@app.post("/scan/run")
def start_scan():
    with SCAN_LOCK:
        if SCAN_STATE["status"] == "running":
            return JSONResponse({"ok": False, "message": "A scan is already running."}, status_code=409)
    threading.Thread(target=_run_scan_job, daemon=True).start()
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


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with SCAN_LOCK:
        data = SCAN_STATE["data"]
        status = SCAN_STATE["status"]
        error = SCAN_STATE["error"]
        logs = list(SCAN_STATE["logs"][-200:])

    today_pick = []
    if data:
        posted = _load_posted()

        def _enrich(p):
            p = _with_inr(p, USD_TO_INR)
            p["posted_at"] = posted.get(str(p["pid"]), {}).get("posted_at")
            return p

        data = dict(data)
        data["top_overall"] = [_enrich(p) for p in data.get("top_overall", [])]
        data["categories"] = [
            {"name": c["name"], "products": [_enrich(p) for p in c["products"]]}
            for c in data.get("categories", [])
        ]
        today_pick = [p for p in data["top_overall"] if not p["posted_at"]][:5]

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "data": data,
            "today_pick": today_pick,
            "status": status,
            "error": error,
            "initial_logs_json": json.dumps(logs),
            "usd_to_inr": USD_TO_INR,
        },
    )


@app.post("/posted/{pid}")
def mark_posted(pid: str, name: str = Form(...)):
    _mark_posted(pid, name)
    return RedirectResponse("/", status_code=303)


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


def _first_truthy(*values):
    for v in values:
        if v:
            return v
    return None


@app.get("/media/{pid}", response_class=HTMLResponse)
def product_media(request: Request, pid: str, name: str = "", fallback_image: str = ""):
    """On-demand full image gallery + video for one product - not fetched
    during the bulk scan (that would add an extra API call per candidate and
    slow the whole scan down), only when you actually want everything for a
    product you're about to post."""
    error = None
    images = []
    video_url = None
    try:
        detail = get_client().get_product_detail(pid)
        images = detail.get("productImageSet") or detail.get("newImgList") or []
        single = _first_truthy(detail.get("bigImage"), detail.get("img"), detail.get("bigimg"), fallback_image)
        if single and single not in images:
            images = [single] + images
        video_url = _first_truthy(detail.get("productVideo"), detail.get("video"))
        if not video_url:
            video_list = detail.get("videoList") or []
            video_url = video_list[0] if video_list else None
    except CJAPIError as e:
        error = str(e)
        if fallback_image:
            images = [fallback_image]

    return templates.TemplateResponse(
        "media.html",
        {"request": request, "name": name, "images": images, "video_url": video_url, "error": error},
    )


@app.get("/caption", response_class=HTMLResponse)
def caption_page(request: Request, name: str, category: str = "", price: float = 0.0):
    caption = None
    error = None
    try:
        caption = generate_instagram_caption(
            api_key=GEMINI_API_KEY,
            product_name=name,
            category=category or "Cute finds",
            retail_price=price,
        )
    except (GeminiAPIError, ValueError) as e:
        error = str(e)
    return templates.TemplateResponse(
        "caption.html", {"request": request, "name": name, "caption": caption, "error": error}
    )
