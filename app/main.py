import os
import threading
import uuid
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

_client: CJClient | None = None


def get_client() -> CJClient:
    global _client
    if _client is None:
        _client = CJClient(api_key=CJ_API_KEY)
    return _client


def _first(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


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
    except (CJAPIError, ValueError) as e:
        with SCAN_LOCK:
            SCAN_STATE["status"] = "error"
            SCAN_STATE["error"] = str(e)
        _scan_log(f"ERROR: {e}")


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
    return templates.TemplateResponse("dashboard.html", {"request": request, "data": data, "status": status})


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


# --- Order placement ---------------------------------------------------

@app.get("/order", response_class=HTMLResponse)
def order_form(request: Request, vid: str = "", name: str = ""):
    return templates.TemplateResponse(
        "order.html", {"request": request, "vid": vid, "product_name": name, "result": None, "error": None}
    )


@app.post("/order", response_class=HTMLResponse)
def place_order(
    request: Request,
    vid: str = Form(...),
    product_name: str = Form(""),
    customer_name: str = Form(...),
    mobile: str = Form(...),
    address: str = Form(...),
    city: str = Form(...),
    province: str = Form(...),
    zip_code: str = Form(...),
    country_code: str = Form(...),
    country_name: str = Form(...),
    quantity: int = Form(1),
):
    result = None
    error = None
    try:
        client = get_client()

        options = client.calculate_freight(
            start_country_code="CN",
            end_country_code=country_code,
            products=[{"vid": vid, "quantity": quantity}],
        )
        if not options:
            raise CJAPIError(
                f"No shipping option available from CJ for this product to {country_code}. "
                "Can't place the order without a valid logisticName."
            )
        options.sort(key=lambda o: float(_first(o, "logisticPrice", default=1e9)))
        logistic_name = _first(options[0], "logisticName", "logisticsName")
        if not logistic_name:
            raise CJAPIError("Got a freight quote but no logistics name field in it - check the raw response.")

        order_number = f"MIRTH-{uuid.uuid4().hex[:10].upper()}"
        result = client.create_order(
            order_number=order_number,
            shipping_country_code=country_code,
            shipping_country=country_name,
            shipping_province=province,
            shipping_city=city,
            shipping_customer_name=customer_name,
            shipping_address=address,
            shipping_zip=zip_code,
            shipping_phone=mobile,
            logistic_name=logistic_name,
            products=[{"vid": vid, "quantity": quantity}],
        )
        result["order_number"] = order_number
    except (CJAPIError, ValueError) as e:
        error = str(e)

    return templates.TemplateResponse(
        "order.html",
        {"request": request, "vid": vid, "product_name": product_name, "result": result, "error": error},
    )


# --- Orders list, filters, confirm, cancel -------------------------------

KNOWN_STATUS_FILTERS = ["", "UNSHIPPED", "DELIVERED", "CREATED", "IN_CART", "CANCELLED"]


@app.get("/orders", response_class=HTMLResponse)
def orders_list(request: Request, status: str = "", page: int = 1):
    error = None
    orders = []
    total = 0
    try:
        data = get_client().list_orders(page_num=page, page_size=20, status=status or None)
        orders = data.get("list", []) or []
        total = data.get("total", len(orders))
    except CJAPIError as e:
        error = str(e)

    return templates.TemplateResponse(
        "orders.html",
        {
            "request": request,
            "orders": orders,
            "total": total,
            "page": page,
            "status": status,
            "known_statuses": KNOWN_STATUS_FILTERS,
            "error": error,
            "action_result": request.query_params.get("action_result"),
        },
    )


@app.post("/orders/{order_id}/confirm")
def confirm_order_route(order_id: str, status: str = Form(""), page: int = Form(1)):
    try:
        get_client().confirm_order(order_id)
        msg = f"Order {order_id} confirmed."
    except CJAPIError as e:
        msg = f"Confirm failed for {order_id}: {e}"
    return RedirectResponse(f"/orders?status={status}&page={page}&action_result={msg}", status_code=303)


@app.post("/orders/{order_id}/cancel")
def cancel_order_route(order_id: str, status: str = Form(""), page: int = Form(1)):
    try:
        get_client().cancel_order(order_id)
        msg = f"Order {order_id} cancelled."
    except CJAPIError as e:
        msg = f"Cancel failed for {order_id}: {e}"
    return RedirectResponse(f"/orders?status={status}&page={page}&action_result={msg}", status_code=303)
