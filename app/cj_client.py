"""
Thin wrapper around the CJdropshipping open API (api2.0).

Auth, product search (/product/listV2), variant lookup
(/product/variant/query) and freight calc are all confirmed against a real,
previously-tested run (see /home/vqcodes/Documents/Sanjna/check-products/cj_tool.py
and its saved cj_results.json) - not guessed from docs alone. Order
create/list/confirm/cancel are built from CJ's official docs pages but have
not been exercised against a live account yet, so treat those as "should be
right" rather than "proven."

CJ allows ~1 call/sec and getAccessToken only once per 5 minutes; a token is
valid ~15 days. We rate-limit every call and cache the token to data/token.json.
"""
import json
import re
import time
from pathlib import Path

import requests

BASE_URL = "https://developers.cjdropshipping.com/api2.0/v1"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TOKEN_CACHE = DATA_DIR / "token.json"

RATE_LIMIT_SECONDS = 1.1
REQUEST_TIMEOUT = 45
MAX_RETRIES = 3

BUNDLE_PATTERN = re.compile(r"\b(\d+)\s*(pcs?|packs?)\b", re.IGNORECASE)
SEASONAL_BLOCKLIST = [
    "halloween", "christmas", "xmas", "santa", "valentine", "easter",
    "thanksgiving", "holiday", "snowman", "pumpkin", "elf", "reindeer",
    "new year", "advent",
]


class CJAPIError(Exception):
    pass


def to_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        if isinstance(value, str) and "--" in value:
            try:
                return float(value.split("--")[0].strip())
            except ValueError:
                pass
        return default


def is_seasonal(name: str) -> bool:
    lname = (name or "").lower()
    return any(term in lname for term in SEASONAL_BLOCKLIST)


def is_bundle_variant(variant: dict) -> bool:
    text = f"{variant.get('variantKey') or ''} {variant.get('variantSku') or ''} {variant.get('variantNameEn') or ''}"
    match = BUNDLE_PATTERN.search(text)
    return bool(match) and int(match.group(1)) >= 2


def pick_representative_variant(variants: list[dict]) -> dict | None:
    """Pick the single-unit variant closest to the MEDIAN price of all
    single-unit variants (bundles excluded) - avoids the "cheapest variant"
    trap, which tends to be an old/legacy SKU, not the one actually sold."""
    if not variants:
        return None
    single_unit = [v for v in variants if not is_bundle_variant(v)]
    pool = single_unit or variants

    prices = sorted(to_float(v.get("variantSellPrice"), 1e9) for v in pool)
    n = len(prices)
    median = prices[n // 2] if n % 2 == 1 else (prices[n // 2 - 1] + prices[n // 2]) / 2
    return min(pool, key=lambda v: abs(to_float(v.get("variantSellPrice"), 1e9) - median))


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "product"


def product_url_for(pid: str, name: str) -> str:
    # Confirmed pattern (verified against a real CJ product page in prior testing):
    # https://www.cjdropshipping.com/product/{slugified-name}-p-{pid}.html
    return f"https://www.cjdropshipping.com/product/{slugify(name)}-p-{pid}.html"


class CJClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("CJ_API_KEY is missing - set it in your .env file")
        self.api_key = api_key
        DATA_DIR.mkdir(exist_ok=True)
        self._token = None
        self._last_call = 0.0
        self._load_cached_token()

    def _load_cached_token(self):
        if TOKEN_CACHE.exists():
            cached = json.loads(TOKEN_CACHE.read_text())
            if cached.get("expires_at", 0) > time.time() + 86400:
                self._token = cached["access_token"]

    def _save_token(self, token: str, expires_in_days: int = 14):
        # CJ's docs say an access token is valid ~15 days - cache for 14 to
        # refresh a day early rather than risk hitting a server-side expiry.
        TOKEN_CACHE.write_text(
            json.dumps({"access_token": token, "expires_at": time.time() + expires_in_days * 86400})
        )

    def _throttle(self):
        wait = RATE_LIMIT_SECONDS - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def _http(self, method: str, url: str, **kwargs) -> dict:
        self._throttle()
        last_err = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
                return resp.json()
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_err = e
                time.sleep(2 * attempt)
        raise CJAPIError(f"{method} {url} failed after {MAX_RETRIES} retries: {last_err}")

    def get_access_token(self, force: bool = False) -> str:
        if self._token and not force:
            return self._token
        data = self._http("POST", f"{BASE_URL}/authentication/getAccessToken", json={"apiKey": self.api_key})
        if not data.get("result"):
            raise CJAPIError(f"getAccessToken failed: {data.get('message', data)}")
        token = data["data"]["accessToken"]
        self._token = token
        self._save_token(token)
        return token

    def _headers(self) -> dict:
        return {"CJ-Access-Token": self.get_access_token(), "Content-Type": "application/json"}

    def _request(self, method: str, path: str, **kwargs) -> dict:
        data = self._http(method, f"{BASE_URL}{path}", headers=self._headers(), **kwargs)
        if not data.get("result"):
            if data.get("code") in (401, 1600000, 1600001):
                self.get_access_token(force=True)
                data = self._http(method, f"{BASE_URL}{path}", headers=self._headers(), **kwargs)
                if data.get("result"):
                    return data
            raise CJAPIError(f"{method} {path} failed: {data.get('message', data)}")
        return data

    def search_products(self, keyword: str, page: int = 1, size: int = 30) -> list[dict]:
        """GET /product/listV2 - confirmed response shape: data.content[].productList[]
        (a list of blocks, each holding a productList array), per fields id,
        nameEn, sellPrice, bigImage, sku, listedNum."""
        params = {"keyWord": keyword, "page": page, "size": size, "orderBy": 1, "sort": "desc"}
        data = self._request("GET", "/product/listV2", params=params)
        products = []
        for block in data.get("data", {}).get("content", []):
            products.extend(block.get("productList", []))
        return products

    def get_variants(self, pid: str) -> list[dict]:
        """GET /product/variant/query?pid=... - returns the variant array
        directly in `data` (not nested), each with vid, variantSku,
        variantKey, variantSellPrice, variantNameEn."""
        data = self._request("GET", "/product/variant/query", params={"pid": pid})
        return data.get("data") or []

    def calculate_freight(self, start_country_code: str, end_country_code: str, products: list[dict]) -> list[dict]:
        """products: [{"vid": "<variant id>", "quantity": 1}]
        Returns the list of available logistics options, each with
        logisticPrice and logisticName (confirmed field names)."""
        body = {
            "startCountryCode": start_country_code,
            "endCountryCode": end_country_code,
            "products": products,
        }
        data = self._request("POST", "/logistic/freightCalculate", json=body)
        return data.get("data", []) or []

    def create_order(
        self,
        order_number: str,
        shipping_country_code: str,
        shipping_country: str,
        shipping_province: str,
        shipping_city: str,
        shipping_customer_name: str,
        shipping_address: str,
        logistic_name: str,
        products: list[dict],
        from_country_code: str = "CN",
        shipping_zip: str | None = None,
        shipping_phone: str | None = None,
    ) -> dict:
        """POST /shopping/order/createOrderV2 (per CJ's docs; not yet
        exercised against a live account - if your account's response shape
        differs, the raised CJAPIError will show CJ's real message).

        logistic_name: must match an option CJ's freightCalculate just
        returned for this exact product+destination - call calculate_freight()
        right before this and reuse its cheapest option's name."""
        body = {
            "orderNumber": order_number,
            "shippingCountryCode": shipping_country_code,
            "shippingCountry": shipping_country,
            "shippingProvince": shipping_province,
            "shippingCity": shipping_city,
            "shippingCustomerName": shipping_customer_name,
            "shippingAddress": shipping_address,
            "logisticName": logistic_name,
            "fromCountryCode": from_country_code,
            "products": products,
        }
        if shipping_zip:
            body["shippingZip"] = shipping_zip
        if shipping_phone:
            body["shippingPhone"] = shipping_phone
        data = self._request("POST", "/shopping/order/createOrderV2", json=body)
        return data.get("data", {})

    def list_orders(self, page_num: int = 1, page_size: int = 20, status: str | None = None) -> dict:
        params = {"pageNum": page_num, "pageSize": page_size}
        if status:
            params["status"] = status
        data = self._request("GET", "/shopping/order/list", params=params)
        return data.get("data", {}) or {}

    def get_order_detail(self, order_id: str) -> dict:
        data = self._request("GET", "/shopping/order/getOrderDetail", params={"orderId": order_id})
        return data.get("data", {}) or {}

    def confirm_order(self, order_id: str) -> dict:
        data = self._request("PATCH", "/shopping/order/confirmOrder", json={"orderId": order_id})
        return data.get("data", {})

    def cancel_order(self, order_id: str) -> dict:
        """Only succeeds while the order is still CREATED or IN_CART."""
        data = self._request("DELETE", "/shopping/order/deleteOrder", params={"orderId": order_id})
        return data.get("data", {})
