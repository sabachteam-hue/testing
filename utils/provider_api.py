import asyncio
import os
import re
import time
import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from database.models import Provider


class ProviderApiError(RuntimeError):
    pass


# Per-attempt timeout: many "guess the endpoint" attempts are tried in a row
# (see _catalog_attempts / _purchase_urls). If each one is allowed the full
# 30s default, a provider that simply doesn't respond (instead of returning a
# fast 404) makes a single sync/order-place request run for minutes — well
# past Railway's/browsers' own timeouts, which is what shows up as "page just
# keeps loading forever". Cap the per-attempt wait, and also cap the total
# wall-clock time spent looping through attempts, so we always fail fast with
# a clear error instead of hanging — but keep both caps generous enough that a
# provider which is merely slow (not dead) still has time to answer, since a
# too-tight cap turns a working-but-slow provider into a permanent sync failure.
_ATTEMPT_TIMEOUT = min(float(os.getenv("API_TIMEOUT", "30")), 20.0)
_TOTAL_BUDGET_SECONDS = float(os.getenv("API_TOTAL_BUDGET", "55"))

# Some upstream panels put their API behind a firewall/CDN that blocks or
# silently drops requests coming from cloud/datacenter IP ranges (Railway's
# outbound IPs included) while allowing normal residential/browser traffic
# through untouched — this project already hit the exact same thing with
# Binance (see utils/payment_verify.py, BINANCE_HTTP_PROXY) and fixed it by
# routing those requests through a residential proxy instead. Reuse that same
# proxy here (if configured) for provider API calls, since it's a generic
# HTTP(S) proxy and not Binance-specific — a provider whose sync/order calls
# hang indefinitely from Railway but work fine from a browser is showing the
# same symptom. A dedicated PROVIDER_HTTP_PROXY can be set instead if a
# different proxy should be used just for provider calls.
_PROVIDER_HTTP_PROXY = (
    os.getenv("PROVIDER_HTTP_PROXY", "").strip()
    or os.getenv("BINANCE_HTTP_PROXY", "").strip()
    or None
)


def _http_client(**kwargs) -> httpx.AsyncClient:
    if _PROVIDER_HTTP_PROXY:
        kwargs["proxy"] = _PROVIDER_HTTP_PROXY
    return httpx.AsyncClient(**kwargs)


def _deadline() -> float:
    return time.monotonic() + _TOTAL_BUDGET_SECONDS


def _deadline_passed(deadline: float) -> bool:
    return time.monotonic() > deadline


# canboso.com rate-limits per source IP across ALL of its endpoints (catalog,
# balance, purchase alike) — every call from this process shares our one
# outbound IP, so two customers checking out a few seconds apart can still
# trip the limiter even though each individual call is already down to a
# single request. Force a minimum gap between any two canboso requests,
# process-wide, so concurrent orders queue briefly instead of racing.
_CANBOSO_MIN_INTERVAL = float(os.getenv("CANBOSO_MIN_INTERVAL", "1.5"))
_canboso_lock = asyncio.Lock()
_canboso_last_call = 0.0


async def _canboso_throttle() -> None:
    global _canboso_last_call
    async with _canboso_lock:
        wait = _canboso_last_call + _CANBOSO_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _canboso_last_call = time.monotonic()


async def _post(provider: Provider, payload: dict[str, Any]) -> dict[str, Any]:
    if not provider.api_url or not provider.api_key:
        raise ProviderApiError("Provider API URL or key is missing")
    timeout = float(os.getenv("API_TIMEOUT", "30"))
    data = {"key": provider.api_key.strip().strip('"').strip("'"), **payload}
    async with _http_client(timeout=timeout) as client:
        response = await client.post(_normalize_url(provider.api_url), data=data)
        response.raise_for_status()
        body = _parse_json_response(response)
        if isinstance(body, dict) and body.get("error"):
            raise ProviderApiError(str(body["error"]))
        return body


async def fetch_services(provider: Provider) -> list[dict[str, Any]]:
    body = await _fetch_catalog(provider)
    items = _extract_items(body)
    if not items:
        raise ProviderApiError(
            "Provider returned JSON but no service list was found. "
            "Use the exact products/services API endpoint, not the Swagger/docs page."
        )
    return [_normalize_service_item(item) for item in items]


async def _fetch_catalog(provider: Provider) -> Any:
    if not provider.api_url:
        raise ProviderApiError("Provider API URL is missing")

    headers = _auth_headers(provider)
    if "telegram-buyer" in (provider.api_url or "").lower():
        await _canboso_throttle()
    async with _http_client(timeout=_ATTEMPT_TIMEOUT, headers=headers) as client:
        # Strip any key/api_key/token/access_token that may already be baked into
        # the saved API URL (e.g. an admin pasted the full "?key=..." link into the
        # API URL field). Otherwise our own attempts below add a second copy of the
        # same param, producing a URL like "?key=xxx&key=xxx" that many providers
        # reject or mishandle.
        url = _strip_auth_query_params(_normalize_url(provider.api_url))
        attempts = _catalog_attempts(provider)
        errors: list[str] = []
        deadline = _deadline()
        for method, kwargs in attempts:
            if _deadline_passed(deadline):
                errors.append("stopped early: provider is too slow to respond (time budget exceeded)")
                break
            try:
                response = await client.request(method, url, **kwargs)
                if response.status_code >= 400:
                    errors.append(_response_summary(response, method))
                    continue
                body = _parse_json_response(response)
                err = _provider_error_message(body)
                if err:
                    errors.append(f"{method}: provider error: {err}")
                    continue
                return body
            except (httpx.HTTPError, ProviderApiError) as exc:
                errors.append(f"{method}: {exc}")

        raise ProviderApiError("Could not fetch provider services. " + " | ".join(errors[:4]))


def _normalize_url(url: str) -> str:
    clean_url = url.strip().strip('"').strip("'")
    if not clean_url.startswith(("http://", "https://")):
        clean_url = f"https://{clean_url}"
    return clean_url


_AUTH_QUERY_PARAM_NAMES = {"key", "api_key", "apikey", "token", "access_token"}


def _strip_auth_query_params(url: str) -> str:
    """Drop key/api_key/token/access_token from an already-normalized URL's query
    string, keeping any other query params the admin may have intentionally set."""
    from urllib.parse import parse_qsl, urlencode

    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in _AUTH_QUERY_PARAM_NAMES]
    new_query = urlencode(kept)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def _catalog_attempts(provider: Provider) -> list[tuple[str, dict[str, Any]]]:
    # canboso.com documented API (https://canboso.com/api/swagger): the only
    # valid catalog shape is GET /api/v2/telegram-buyer/products?key=xxx.
    # Don't run it through the generic guesswork loop below — canboso rate-
    # limits (HTTP 429) after just a handful of requests in quick succession,
    # and the generic loop fires 9-13 attempts per sync (plain GET, then GET
    # with key/api_key/token/access_token, then action=products/services
    # variants, then POST variants). Hitting that many requests on every sync
    # burns through canboso's rate limit fast. Since we already know the exact
    # correct format from their Swagger docs, go straight to it — one request.
    if "telegram-buyer" in (provider.api_url or "").lower() and provider.api_key:
        api_key = provider.api_key.strip().strip('"').strip("'")
        return [("GET", {"params": {"key": api_key}})]

    attempts: list[tuple[str, dict[str, Any]]] = [("GET", {})]
    if provider.api_key:
        api_key = provider.api_key.strip().strip('"').strip("'")
        # Try the plain "just the key" shape first (e.g. GET /products?key=xxx) —
        # this is the most common single-key panel format and, on slow providers,
        # must be reached before the total time budget runs out. The less common
        # action=products / alternate-param-name / POST variants are kept as
        # fallbacks after it.
        attempts.extend(
            [
                ("GET", {"params": {"key": api_key}}),
                ("GET", {"params": {"api_key": api_key}}),
                ("GET", {"params": {"token": api_key}}),
                ("GET", {"params": {"access_token": api_key}}),
            ]
        )
    attempts.extend(
        [
            ("GET", {"params": {"action": "products"}}),
            ("GET", {"params": {"action": "services"}}),
        ]
    )
    if provider.api_key:
        api_key = provider.api_key.strip().strip('"').strip("'")
        attempts.extend(
            [
                ("GET", {"params": {"action": "products", "key": api_key}}),
                ("GET", {"params": {"action": "products", "api_key": api_key}}),
                ("GET", {"params": {"action": "products", "token": api_key}}),
                ("GET", {"params": {"action": "products", "access_token": api_key}}),
                ("POST", {"data": {"key": api_key, "action": "services"}}),
                ("POST", {"data": {"api_key": api_key, "action": "services"}}),
                ("POST", {"data": {"access_token": api_key, "action": "services"}}),
                ("POST", {"json": {"api_key": api_key, "action": "services"}}),
                ("POST", {"json": {"access_token": api_key, "action": "services"}}),
            ]
        )
    return attempts


def _parse_json_response(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderApiError(_response_summary(response, response.request.method)) from exc


def _response_summary(response: httpx.Response, method: str) -> str:
    content_type = response.headers.get("content-type", "unknown")
    preview = response.text.strip().replace("\n", " ")[:180]
    if not preview:
        preview = "<empty response>"
    return f"{method}: status {response.status_code}, content-type {content_type}, body starts with: {preview}"


def _auth_headers(provider: Provider) -> dict[str, str]:
    # Some providers front their API with a firewall/CDN (Cloudflare etc.) that
    # blocks or silently hangs requests carrying a generic client "User-Agent"
    # (httpx's default is "python-httpx/x.y") — the exact same request works
    # fine from a browser (e.g. testing the endpoint in Swagger UI) purely
    # because the browser sends a normal-looking User-Agent. Send a real
    # browser-style User-Agent so our server-to-server calls aren't fingerprinted
    # as a bot and dropped before ever reaching the provider's application code.
    headers = {
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    if not provider.api_key:
        return headers
    api_key = provider.api_key.strip().strip('"').strip("'")
    headers.update({"Authorization": f"Bearer {api_key}", "X-API-Key": api_key})
    return headers


def _extract_items(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    if not isinstance(body, dict):
        return []
    for key in ("products", "services", "data", "result", "items"):
        value = body.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        # Some APIs wrap the list: {"data": {"products": [...]}}
        if isinstance(value, dict):
            for inner_key in ("products", "services", "items", "result"):
                inner = value.get(inner_key)
                if isinstance(inner, list):
                    return [item for item in inner if isinstance(item, dict)]
    return []


def _response_is_order_list(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    if isinstance(body.get("orders"), list):
        return True
    data = body.get("data")
    return isinstance(data, dict) and isinstance(data.get("orders"), list)


def _extract_matching_order(body: Any, provider_order_id: str) -> dict[str, Any] | None:
    """Shop Bot / list-style APIs may only expose GET /orders -> {orders:[...]}.

    Pull out the matching order row by order_group / orderCode / id and return it
    as a normalized detail payload so delivery polling can complete the order.
    """
    if not isinstance(body, dict):
        return None
    rows = body.get("orders")
    if not isinstance(rows, list):
        rows = body.get("data", {}).get("orders") if isinstance(body.get("data"), dict) else None
    if not isinstance(rows, list):
        return None
    needle = str(provider_order_id).strip()
    for row in rows:
        if not isinstance(row, dict):
            continue
        candidates = [
            row.get("order_group"),
            row.get("orderGroup"),
            row.get("orderCode"),
            row.get("order_code"),
            row.get("orderId"),
            row.get("order_id"),
            row.get("external_order_id"),
            row.get("id"),
            row.get("_id"),
        ]
        if any(str(val).strip() == needle for val in candidates if val not in (None, "")):
            merged = dict(body)
            merged["order"] = row
            for key, value in row.items():
                if key not in merged or merged.get(key) in (None, "", []):
                    merged[key] = value
            return merged
    return None


def _pick_number(item: dict[str, Any], keys: tuple[str, ...], default: float = 0.0) -> float | None:
    """First present numeric field. Returns None if no key matched (so callers can try more sources)."""
    for key in keys:
        if key not in item:
            continue
        raw = item.get(key)
        if raw is None or raw == "":
            continue
        # Nested currency maps: {"usd": 1.2, "usdt": 1.2, "vnd": 30000}
        if isinstance(raw, dict):
            nested = _pick_number(
                raw,
                ("usd", "usdt", "USD", "USDT", "price", "amount", "value", "vnd", "VND"),
            )
            if nested is not None:
                return nested
            continue
        try:
            return float(str(raw).replace("$", "").replace(",", "").strip())
        except (TypeError, ValueError):
            continue
    return None


def _pick_positive_number(item: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    """Like _pick_number, but skip 0 / negative so empty sale_price does not block real price."""
    for key in keys:
        if key not in item:
            continue
        raw = item.get(key)
        if raw is None or raw == "":
            continue
        if isinstance(raw, dict):
            nested = _pick_positive_number(
                raw,
                ("usd", "usdt", "USD", "USDT", "price", "amount", "value", "vnd", "VND"),
            )
            if nested is not None:
                return nested
            continue
        try:
            value = float(str(raw).replace("$", "").replace(",", "").strip())
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


# Reseller / special API pricing must win over public retail price.
_RESELLER_COST_KEYS = (
    "reseller_price",
    "resellerPrice",
    "special_price",
    "specialPrice",
    "wholesale_price",
    "wholesalePrice",
    "your_price",
    "yourPrice",
    "api_price",
    "apiPrice",
    "partner_price",
    "partnerPrice",
    "custom_price",
    "customPrice",
    "buy_price",
    "buyPrice",
    "unit_cost",
    "unitCost",
    "cost_price",
    "costPrice",
)

# Active store sale / promo price on the provider shop.
_SALE_COST_KEYS = (
    "sale_price",
    "salePrice",
    "discounted_price",
    "discountedPrice",
    "promo_price",
    "promoPrice",
    "current_price",
    "currentPrice",
    "final_price",
    "finalPrice",
    "active_price",
    "activePrice",
    "offer_price",
    "offerPrice",
)

_SALE_FLAG_KEYS = (
    "on_sale",
    "onSale",
    "is_on_sale",
    "isOnSale",
    "sale_active",
    "saleActive",
    "has_sale",
    "hasSale",
    "is_discounted",
    "isDiscounted",
)

_SALE_OBJECT_KEYS = ("sale", "promotion", "promo", "discount", "offer")

_REGULAR_PRICE_KEYS = (
    "regular_price",
    "regularPrice",
    "retail_price",
    "retailPrice",
    "public_price",
    "publicPrice",
    "original_price",
    "originalPrice",
    "list_price",
    "listPrice",
    "msrp",
    "base_price",
    "basePrice",
)

_FALLBACK_COST_KEYS = (
    "price_usd",
    "price_usdt",
    "usd_price",
    "usdt_price",
    "priceUsd",
    "priceUsdt",
    "rate",
    "cost",
    "price",
    "amount",
    "value",
    "display_price",
    "displayPrice",
    "selling_price",
    "sellingPrice",
    "walletPricing",
    "walletPrice",
    "price_vnd",
    "vnd_price",
    "priceVnd",
)

_NESTED_PRICE_LIST_KEYS = ("variants", "prices", "options", "skus", "plans", "packages")


def _pick_product_cost(item: dict[str, Any]) -> float:
    """Pick the reseller's cost from a provider product row.

    Prefer store sale price when the provider shop has an active promotion,
    then reseller/special API rates, then any current price lower than regular.

    Important: treat 0 as "missing". Many Shop Bot APIs (Starboy, etc.) always
    include sale_price/special_price: 0 when unused — that must not block the
    real price / price_usd / nested pricing fields.
    """
    pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
    sources: list[dict[str, Any]] = [item]
    if pricing:
        sources.append(pricing)
    for key in ("walletPricing", "walletPrice", "priceInfo", "price_info"):
        nested = item.get(key)
        if isinstance(nested, dict):
            sources.append(nested)

    for source in sources:
        cost = _pick_positive_number(source, _SALE_COST_KEYS)
        if cost is not None:
            return cost

    for key in _SALE_OBJECT_KEYS:
        nested = item.get(key)
        if isinstance(nested, dict):
            cost = _pick_positive_number(nested, _SALE_COST_KEYS + ("price", "amount", "value"))
            if cost is not None:
                return cost

    for source in sources:
        cost = _pick_positive_number(source, _RESELLER_COST_KEYS)
        if cost is not None:
            return cost

    on_sale = any(
        source.get(flag) is True for source in sources for flag in _SALE_FLAG_KEYS if flag in source
    )
    if on_sale:
        for source in sources:
            current = _pick_positive_number(source, ("price", "amount", "sell_price", "sellPrice"))
            if current is not None:
                return current

    for source in sources:
        regular = _pick_positive_number(source, _REGULAR_PRICE_KEYS)
        special = _pick_positive_number(source, ("price", "amount", "sell_price", "sellPrice"))
        if regular is not None and special is not None and special <= regular:
            return special

    for source in sources:
        cost = _pick_positive_number(source, _FALLBACK_COST_KEYS)
        if cost is not None:
            return cost
        cost = _pick_positive_number(source, _REGULAR_PRICE_KEYS)
        if cost is not None:
            return cost

    if pricing:
        cost = _pick_positive_number(pricing, ("usd", "usdt", "USD", "USDT", "vnd", "VND", "price", "amount"))
        if cost is not None:
            return cost

    # Some catalogs put unit price only on the first variant/plan row.
    for list_key in _NESTED_PRICE_LIST_KEYS:
        rows = item.get(list_key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                cost = _pick_product_cost(row)
                if cost > 0:
                    return cost

    return 0.0


def _normalize_service_item(item: dict[str, Any]) -> dict[str, Any]:
    product_id = item.get("service") or item.get("id") or item.get("_id") or item.get("productId") or item.get("product_id")
    name = (
        item.get("name")
        or item.get("product_name")
        or item.get("product_name_raw")
        or item.get("productName")
        or item.get("title")
        or str(product_id)
    )
    category = item.get("category") or item.get("slotProductType") or item.get("type") or "Imported"

    cost = _pick_product_cost(item)

    stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
    available = _pick_number(
        item,
        (
            "stock_available",
            "stockAvailable",
            "available_stock",
            "availableStock",
            "available",
            "stock",
            "qty",
            "quantity",
            "in_stock",
            "inStock",
        ),
    )
    if available is None and stats:
        available = _pick_number(stats, ("available", "stock", "stock_available", "qty"))
    if available is None:
        available = 0.0

    sold = _pick_number(item, ("sold", "sold_count", "total_sold"), None)
    if sold is None and stats:
        sold = _pick_number(stats, ("sold", "sold_count"), 0.0)
    if sold is None:
        sold = 0.0

    description = (
        item.get("description")
        or item.get("description_raw")
        or item.get("product_name_raw")
        or item.get("product_name")
        or name
    )
    min_qty = item.get("min") or item.get("min_qty") or item.get("minimum") or item.get("min_order") or 1
    max_qty = (
        item.get("max")
        or item.get("max_qty")
        or item.get("maximum")
        or item.get("max_order")
        or max(int_or_default(available, 1), 1)
    )

    return {
        "service": str(product_id),
        "id": str(product_id),
        "name": str(name),
        "description": str(description),
        "category": str(category),
        "rate": float(cost),
        "cost": float(cost),
        "available": int_or_default(available, 0),
        "sold": int_or_default(sold, 0),
        "min": int_or_default(min_qty, 1),
        "max": int_or_default(max_qty, 10000),
        "raw": item,
    }


def float_or_default(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def int_or_default(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return default


def _provider_error_message(body: Any) -> str | None:
    """Return provider error text when the JSON payload indicates failure."""
    if not isinstance(body, dict):
        return None
    if body.get("ok") is False:
        return str(body.get("error") or body.get("message") or body.get("detail") or "ok=false")
    if body.get("success") is False:
        return str(body.get("error") or body.get("message") or body.get("detail") or "success=false")
    if body.get("error"):
        return str(body["error"])
    # FastAPI-style auth errors sometimes arrive as {"detail": "..."} with HTTP 200.
    detail = body.get("detail")
    if isinstance(detail, str) and detail.strip():
        lower = detail.lower()
        if any(token in lower for token in ("invalid", "missing", "unauthorized", "forbidden", "token", "api key")):
            return detail
    message = body.get("message")
    if isinstance(message, str) and message.strip():
        lower = message.lower()
        if any(
            token in lower
            for token in (
                "insufficient",
                "not enough",
                "out of stock",
                "sold out",
                "unauthorized",
                "forbidden",
                "invalid api",
                "invalid key",
                "invalid token",
            )
        ):
            return message
    return None


def _is_api_discovery_doc(body: Any) -> bool:
    """SafwanTiger (and similar) return ok:true docs from POST /api — not an order."""
    if not isinstance(body, dict):
        return False
    endpoints = body.get("endpoints")
    if isinstance(endpoints, dict) and (
        "order_body" in body or "order" in endpoints or "legacy_order" in endpoints
    ):
        return True
    name = str(body.get("name") or "").lower()
    return "reseller api" in name and isinstance(endpoints, dict)


def _looks_like_catalog_response(body: Any) -> bool:
    """Product/service list — HTTP 200 but not a placed order."""
    if not isinstance(body, dict):
        return False
    for key in ("products", "services"):
        rows = body.get(key)
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return True
    data = body.get("data")
    if isinstance(data, dict):
        for key in ("products", "services"):
            rows = data.get(key)
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                return True
    return False


def _has_strong_purchase_ref(body: Any) -> bool:
    """True when JSON includes a real order reference (not a bare product id)."""
    from utils.provider_delivery import provider_delivery_sources

    if isinstance(body, (int, float)):
        return True
    if isinstance(body, str) and body.strip():
        return True
    if not isinstance(body, dict):
        return False
    strong_keys = (
        "order_group",
        "orderGroup",
        "orderCode",
        "order_code",
        "orderId",
        "order_id",
        "request_id",
        "external_order_id",
        "provider_order_id",
    )
    for source in provider_delivery_sources(body):
        order_value = source.get("order")
        if order_value not in (None, "") and not isinstance(order_value, (dict, list)):
            return True
        for key in strong_keys:
            if source.get(key) not in (None, ""):
                return True
    return False


def _looks_like_purchase_response(body: Any) -> bool:
    """Reject fake HTTP 200s that are not an actual buy (no balance cut / no delivery).

    After SafwanTiger /order was preferred first, some Shop Bot panels (tunvn…)
    answered POST /order or GET /orders with ok/list JSON. place_order treated
    that as success → SMF status=processing, provider wallet untouched.
    """
    from utils.provider_delivery import (
        extract_provider_delivery_items,
        extract_provider_status,
        provider_status_is_completed,
    )

    if _is_api_discovery_doc(body) or _response_is_order_list(body) or _looks_like_catalog_response(body):
        return False
    if isinstance(body, (int, float)):
        return True
    if isinstance(body, str) and body.strip():
        # Bare "Completed" / numeric id — SMM panels.
        return True
    if not isinstance(body, dict):
        return False
    if extract_provider_delivery_items(body):
        return True
    if _has_strong_purchase_ref(body):
        return True
    status = extract_provider_status(body)
    if provider_status_is_completed(status) and (
        body.get("data") not in (None, "", [])
        or body.get("result") not in (None, "", [])
        or body.get("order") not in (None, "", [])
    ):
        return True
    # {"ok": true} / {"success": true} / {"message": "..."} alone is NOT a buy.
    return False


def _is_supabase_reseller_api(api_url: str | None) -> bool:
    if not api_url:
        return False
    url = _normalize_url(api_url).lower()
    return "reseller-api" in url or ("/functions/v1/" in url and "action=" in url)


def _reseller_api_base(api_url: str) -> str:
    url = _normalize_url(api_url)
    return url.split("?", 1)[0].rstrip("/")


async def _place_reseller_order(
    provider: Provider,
    service_id: str,
    quantity: int,
    external_order_id: str | None = None,
) -> dict[str, Any]:
    """Supabase Edge Function reseller API (Rexovaan style).

    POST {base}?action=order
    Body: {"product_id", "quantity", "external_order_id"}
    Auth: Authorization: Bearer API_KEY
    """
    base = _reseller_api_base(provider.api_url)
    url = f"{base}?action=order"
    payload: dict[str, Any] = {
        "product_id": int(service_id) if str(service_id).isdigit() else service_id,
        "quantity": quantity,
    }
    if external_order_id:
        payload["external_order_id"] = external_order_id

    timeout = float(os.getenv("API_TIMEOUT", "30"))
    headers = _auth_headers(provider)
    headers["Content-Type"] = "application/json"
    async with _http_client(timeout=timeout, headers=headers) as client:
        response = await client.post(url, json=payload)
        if response.status_code >= 400:
            raise ProviderApiError(_response_summary(response, "POST"))
        body = _parse_json_response(response)
        err = _provider_error_message(body)
        if err:
            raise ProviderApiError(err)
        return body if isinstance(body, dict) else {"result": body}


async def _get_reseller_order_details(provider: Provider, provider_order_id: str) -> dict[str, Any]:
    base = _reseller_api_base(provider.api_url)
    timeout = float(os.getenv("API_TIMEOUT", "30"))
    headers = _auth_headers(provider)
    errors: list[str] = []
    attempts: list[tuple[str, str, dict[str, Any]]] = [
        (f"{base}?action=order_status", "GET", {"params": {"order_id": provider_order_id}}),
        (f"{base}?action=order_status", "GET", {"params": {"external_order_id": provider_order_id}}),
        (f"{base}?action=order_status", "GET", {"params": {"id": provider_order_id}}),
        (f"{base}?action=order", "GET", {"params": {"order_id": provider_order_id}}),
        (f"{base}?action=order", "GET", {"params": {"external_order_id": provider_order_id}}),
        (f"{base}?action=orders", "GET", {"params": {"external_order_id": provider_order_id}}),
        (f"{base}?action=orders", "GET", {}),
        (f"{base}?action=order_status", "POST", {"json": {"order_id": provider_order_id}}),
        (f"{base}?action=order_status", "POST", {"json": {"external_order_id": provider_order_id}}),
    ]
    async with _http_client(timeout=timeout, headers=headers) as client:
        for url, method, kwargs in attempts:
            try:
                response = await client.request(method, url, **kwargs)
                if response.status_code >= 400:
                    errors.append(_response_summary(response, method))
                    continue
                body = _parse_json_response(response)
                matched = _extract_matching_order(body, provider_order_id)
                if matched is not None:
                    body = matched
                elif _response_is_order_list(body):
                    errors.append(f"{method}: no matching order in list response")
                    continue
                err = _provider_error_message(body)
                if err:
                    errors.append(f"{method}: provider error: {err}")
                    continue
                return body if isinstance(body, dict) else {"result": body}
            except (httpx.HTTPError, ProviderApiError) as exc:
                errors.append(f"{method}: {exc}")
    raise ProviderApiError("Could not fetch reseller order details. " + " | ".join(errors[:5]))


async def place_order(
    provider: Provider,
    service_id: str,
    link: str,
    quantity: int,
    external_order_id: str | None = None,
) -> dict[str, Any]:
    if not provider.api_url or not provider.api_key:
        raise ProviderApiError("Provider API URL or key is missing")

    if _is_supabase_reseller_api(provider.api_url):
        return await _place_reseller_order(provider, service_id, quantity, external_order_id)

    # canboso.com (telegram-buyer/purchase): same rate-limit problem as the
    # catalog/balance fetches above, but worse — the generic loop below fires
    # ~20 payload-shape guesses (json + form, times ~15 field-name variants)
    # at the SAME url in a couple of seconds. canboso's nginx rate limiter
    # (scope "nginx_ip") trips almost immediately, so every single order was
    # failing with 429 RATE_LIMITED regardless of which payload shape was
    # actually correct. Send one well-formed request instead; retry once,
    # honoring their retryAfter, only if that first attempt is rate-limited.
    if "telegram-buyer" in (provider.api_url or "").lower():
        return await _place_canboso_order(provider, service_id, quantity, external_order_id)

    headers = _auth_headers(provider)
    errors: list[str] = []
    deadline = _deadline()
    async with _http_client(timeout=_ATTEMPT_TIMEOUT, headers=headers) as client:
        for url in _purchase_urls(provider.api_url):
            if _deadline_passed(deadline):
                errors.append("stopped early: provider is too slow to respond (time budget exceeded)")
                break
            for method, kwargs in _purchase_attempts(
                provider, service_id, link, quantity, external_order_id=external_order_id
            ):
                if _deadline_passed(deadline):
                    break
                try:
                    response = await client.request(method, url, **kwargs)
                    if response.status_code >= 400:
                        errors.append(f"{url} " + _response_summary(response, method))
                        continue
                    body = _parse_json_response(response)
                    if _is_api_discovery_doc(body):
                        errors.append(
                            f"{method} {url}: API discovery document (not an order) — try /order"
                        )
                        continue
                    if _response_is_order_list(body) or _looks_like_catalog_response(body):
                        errors.append(
                            f"{method} {url}: catalog/order-list response (not a placed buy)"
                        )
                        continue
                    err = _provider_error_message(body)
                    if err:
                        errors.append(f"{method} {url}: provider error: {err}")
                        continue
                    if not _looks_like_purchase_response(body):
                        errors.append(
                            f"{method} {url}: HTTP OK but no order id/delivery "
                            f"(not a real buy) body={str(body)[:180]}"
                        )
                        continue
                    return body if isinstance(body, dict) else {"result": body}
                except (httpx.HTTPError, ProviderApiError) as exc:
                    errors.append(f"{method} {url}: {exc}")

    try:
        return await _post(
            provider,
            {
                "action": "add",
                "service": service_id,
                "link": link,
                "quantity": quantity,
            },
        )
    except Exception as exc:
        errors.append(f"generic POST: {exc}")
    raise ProviderApiError("Could not place provider order. " + " | ".join(errors[:5]))


async def get_order_details(provider: Provider, provider_order_id: str) -> dict[str, Any]:
    if not provider.api_url or not provider_order_id:
        raise ProviderApiError("Provider API URL or order ID is missing")

    if _is_supabase_reseller_api(provider.api_url):
        return await _get_reseller_order_details(provider, provider_order_id)

    headers = _auth_headers(provider)
    errors: list[str] = []
    deadline = _deadline()
    async with _http_client(timeout=_ATTEMPT_TIMEOUT, headers=headers) as client:
        for url, method, kwargs in _order_detail_attempts(provider, provider_order_id):
            if _deadline_passed(deadline):
                errors.append("stopped early: provider is too slow to respond (time budget exceeded)")
                break
            try:
                response = await client.request(method, url, **kwargs)
                if response.status_code >= 400:
                    errors.append(_response_summary(response, method))
                    continue
                body = _parse_json_response(response)
                matched = _extract_matching_order(body, provider_order_id)
                if matched is not None:
                    body = matched
                elif _response_is_order_list(body):
                    # Full order history without a matching row — try next endpoint.
                    errors.append(f"{method}: no matching order in list response")
                    continue
                err = _provider_error_message(body)
                if err:
                    errors.append(f"{method}: provider error: {err}")
                    continue
                return body if isinstance(body, dict) else {"result": body}
            except (httpx.HTTPError, ProviderApiError) as exc:
                errors.append(f"{method}: {exc}")
    raise ProviderApiError("Could not fetch provider order details. " + " | ".join(errors[:5]))


async def _place_canboso_order(
    provider: Provider,
    service_id: str,
    quantity: int,
    external_order_id: str | None,
) -> dict[str, Any]:
    """canboso.com (https://canboso.com/api/swagger): POST {base}/purchase.

    Single request, not the generic guesswork loop — see place_order for why.
    """
    base = _api_base_url(provider.api_url)
    query = _url_query(provider.api_url)
    url = _with_original_query(f"{base}/purchase", query)
    product_id: Any = int(service_id) if str(service_id).isdigit() else service_id
    ref = (external_order_id or "").strip()
    payload: dict[str, Any] = {"product_id": product_id, "quantity": quantity}
    if ref:
        payload["external_order_id"] = ref
        payload["request_id"] = ref

    headers = _auth_headers(provider)
    idem_key = _idempotency_key(external_order_id)
    headers["Idempotency-Key"] = idem_key

    async def _attempt() -> httpx.Response:
        await _canboso_throttle()
        async with _http_client(timeout=_ATTEMPT_TIMEOUT, headers=headers) as client:
            return await client.post(url, json=payload)

    response = await _attempt()
    attempt_num = 1
    while response.status_code == 429 and attempt_num < 3:
        retry_after = 1.5 * attempt_num
        try:
            body = _parse_json_response(response)
            if isinstance(body, dict):
                retry_after = float(
                    ((body.get("rateLimit") or {}).get("retryAfter")) or retry_after
                )
        except Exception:  # noqa: BLE001 - fall back to default backoff
            pass
        await asyncio.sleep(min(retry_after, 6.0))
        response = await _attempt()
        attempt_num += 1

    if response.status_code >= 400:
        raise ProviderApiError(_response_summary(response, "POST"))
    body = _parse_json_response(response)
    err = _provider_error_message(body)
    if err:
        raise ProviderApiError(err)
    if not _looks_like_purchase_response(body):
        raise ProviderApiError(
            f"POST {url}: HTTP OK but no order id/delivery in response body={str(body)[:180]}"
        )
    return body if isinstance(body, dict) else {"result": body}


def _idempotency_key(external_order_id: str | None) -> str:
    """Build a canboso-compliant Idempotency-Key (8-128 chars).

    Deterministic per external_order_id so retrying the same order reuses the
    same key instead of canboso seeing it as a brand-new purchase.
    """
    ref = (external_order_id or "").strip()
    key = f"order-{ref}" if ref else f"order-{uuid.uuid4()}"
    key = re.sub(r"[^A-Za-z0-9_.-]", "-", key)[:128]
    if len(key) < 8:
        key = key.ljust(8, "0")
    return key


def _purchase_url(api_url: str) -> str:
    """Back-compat: primary purchase/order URL."""
    return _purchase_urls(api_url)[0]


def _purchase_urls(api_url: str) -> list[str]:
    """Candidate order endpoints.

    SafwanTiger Shop: POST /api/order  (also legacy POST /api?action=order)
    Shop Bot API (tunvnmmo style): POST /api/buy
    MMOStore: POST /api/v1/orders
    Older panels: /purchase
    """
    query = _url_query(api_url)
    # Build every candidate on the query-stripped URL, then re-append the
    # original query string once at the very end - never let it sit mid-string
    # (see _api_base_url for why that breaks routing on panels that put an
    # auth token in the configured URL's query string).
    split = urlsplit(_normalize_url(api_url))
    url = urlunsplit((split.scheme, split.netloc, split.path.rstrip("/"), "", ""))
    base = _api_base_url(api_url).rstrip("/")
    urls: list[str] = []
    lower = url.lower()

    # canboso.com documented API (https://canboso.com/api/swagger):
    # POST /api/v2/telegram-buyer/purchase — hit this exact endpoint first so
    # we don't waste the time budget on /buy (which 404s on this panel).
    if "telegram-buyer" in lower and base:
        urls.append(f"{base}/purchase")

    # Shop Bot panels (/products or /buy) must hit /buy before /order — POST /order
    # often returns a fake HTTP 200 that never charges the provider wallet.
    shop_bot = any(token in lower for token in ("/buy", "/products", "tunvn", "shopbot", "shop-bot"))

    if base:
        if shop_bot:
            urls.extend(
                [
                    f"{base}/buy",
                    f"{base}/order",
                    f"{base}?action=order",
                    f"{base}/purchase",
                    f"{base}/orders",
                ]
            )
        else:
            urls.extend(
                [
                    f"{base}/order",
                    f"{base}?action=order",
                    f"{base}/buy",
                    f"{base}/orders",
                    f"{base}/purchase",
                ]
            )

    if "/products" in url:
        urls.append(url.replace("/products", "/buy", 1))
        urls.append(url.replace("/products", "/order", 1))
        urls.append(url.replace("/products", "/orders", 1))
        urls.append(url.replace("/products", "/purchase", 1))
    if url.rstrip("/").endswith("/services"):
        svc_base = url.rstrip("/")[: -len("/services")]
        urls.append(svc_base + "/buy")
        urls.append(svc_base + "/order")
        urls.append(svc_base + "/orders")
        urls.append(svc_base + "/purchase")

    # Bare /api last — often returns a discovery doc (rejected in place_order).
    if url not in urls:
        urls.append(url)

    seen: set[str] = set()
    out: list[str] = []
    for candidate in urls:
        if candidate not in seen:
            seen.add(candidate)
            out.append(_with_original_query(candidate, query))
    return out


def _api_base_url(api_url: str) -> str:
    # Split off the query string FIRST. Some panels bake their auth token
    # straight into the configured URL (e.g. ".../products?key=tgb_xxx").
    # Doing endswith()/suffix-stripping on the raw string (old behavior) then
    # silently fails whenever a query string trails the path, and every
    # candidate built from it (e.g. "<base>/buy") ends up appending "/buy"
    # AFTER the query string instead of before it - which the server sees as
    # an unrouted "/products" POST (query strings aren't part of the path).
    split = urlsplit(_normalize_url(api_url))
    path = split.path.rstrip("/")
    for suffix in ("/products", "/services", "/purchase", "/buy", "/orders", "/order"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((split.scheme, split.netloc, path, "", "")).rstrip("/")


def _url_query(api_url: str) -> str:
    """The original query string (e.g. 'key=tgb_xxx') on a configured provider
    URL, when the admin's URL bakes the auth token in there instead of it being
    sent separately. Must be re-appended to the END of every candidate purchase/
    order-detail URL we build, never left attached mid-string."""
    return urlsplit(_normalize_url(api_url)).query


def _with_original_query(url: str, query: str) -> str:
    if not query:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{query}"


def _order_detail_attempts(provider: Provider, provider_order_id: str) -> list[tuple[str, str, dict[str, Any]]]:
    api_key = provider.api_key.strip().strip('"').strip("'") if provider.api_key else ""
    base_url = _api_base_url(provider.api_url)
    query = _url_query(provider.api_url)
    auth_params = {"access_token": api_key} if api_key else {}
    attempts = [
        (f"{base_url}/orders/group/{provider_order_id}", "GET", {}),
        (f"{base_url}/orders/{provider_order_id}", "GET", {}),
        (f"{base_url}/order/{provider_order_id}", "GET", {}),
        (f"{base_url}/buy/{provider_order_id}", "GET", {}),
        (f"{base_url}/order-details/{provider_order_id}", "GET", {}),
        (f"{base_url}/order-detail/{provider_order_id}", "GET", {}),
        (f"{base_url}/purchase/{provider_order_id}", "GET", {}),
        (f"{base_url}/purchases/{provider_order_id}", "GET", {}),
        (f"{base_url}/orders", "GET", {"params": {"orderCode": provider_order_id, **auth_params}}),
        (f"{base_url}/orders", "GET", {"params": {"order_group": provider_order_id, **auth_params}}),
        (f"{base_url}/orders", "GET", {"params": {"orderGroup": provider_order_id, **auth_params}}),
        (f"{base_url}/orders", "GET", {"params": {**auth_params, "limit": 100}}),
        (f"{base_url}/orders", "GET", {"params": auth_params}),
        (f"{base_url}/order", "GET", {"params": {"orderCode": provider_order_id}}),
        (f"{base_url}/order-details", "GET", {"params": {"orderCode": provider_order_id}}),
        (f"{base_url}/purchase", "GET", {"params": {"orderCode": provider_order_id}}),
        (f"{base_url}/purchases", "GET", {"params": {"orderCode": provider_order_id}}),
        (f"{base_url}/purchase", "GET", {"params": {"orderId": provider_order_id}}),
        (f"{base_url}/purchase", "GET", {"params": {"access_token": api_key, "orderCode": provider_order_id}}),
        (f"{base_url}/orders", "GET", {"params": {"access_token": api_key, "orderCode": provider_order_id}}),
        (f"{base_url}/purchase", "POST", {"json": {"orderCode": provider_order_id}}),
        (f"{base_url}/purchase", "POST", {"data": {"orderCode": provider_order_id}}),
        (f"{base_url}/purchase", "POST", {"json": {"api_key": api_key, "orderCode": provider_order_id}}),
        (f"{base_url}/purchase", "POST", {"data": {"api_key": api_key, "orderCode": provider_order_id}}),
        (f"{base_url}/purchase", "POST", {"json": {"access_token": api_key, "orderCode": provider_order_id}}),
        (f"{base_url}/purchase", "POST", {"data": {"access_token": api_key, "orderCode": provider_order_id}}),
    ]
    # Panels whose auth token is baked into the configured URL's query string
    # (e.g. ".../products?key=tgb_xxx") need that query re-appended to every
    # candidate URL — see _api_base_url for why it's stripped above.
    if query:
        attempts = [(_with_original_query(url, query), method, kwargs) for url, method, kwargs in attempts]
    return attempts



def _purchase_attempts(
    provider: Provider,
    service_id: str,
    link: str,
    quantity: int,
    external_order_id: str | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    api_key = provider.api_key.strip().strip('"').strip("'") if provider.api_key else ""
    product_id: Any = int(service_id) if str(service_id).isdigit() else service_id
    ref = (external_order_id or "").strip()

    # SafwanTiger docs: { product_id, quantity, request_id } — put these first.
    payloads: list[dict[str, Any]] = []
    if ref:
        payloads.extend(
            [
                {"product_id": product_id, "quantity": quantity, "request_id": ref},
                {
                    "product_id": product_id,
                    "quantity": quantity,
                    "request_id": ref,
                    "external_order_id": ref,
                },
                {"product_id": service_id, "quantity": quantity, "request_id": ref},
                {"product_id": product_id, "quantity": quantity, "external_order_id": ref},
            ]
        )
    payloads.extend(
        [
            {"product_id": product_id, "quantity": quantity},
            # Shop Bot (tunvn / USD): currency on /buy for wallet cut.
            {"product_id": product_id, "quantity": quantity, "currency": "usdt"},
            {"product_id": service_id, "quantity": quantity, "currency": "usdt"},
            {"product_id": product_id, "quantity": quantity, "currency": "usd"},
            {"product_id": service_id, "quantity": quantity, "currency": "vnd"},
            {"product_id": product_id, "quantity": quantity, "currency": "vnd"},
            {"product_id": service_id, "quantity": quantity},
            {"product_id": service_id, "qty": quantity, "currency": "usdt"},
            {"product_id": service_id, "qty": quantity, "currency": "vnd"},
            {"product_id": service_id, "qty": quantity, "currency": "usd"},
            {"product_id": service_id, "qty": quantity},
            {"productId": service_id, "quantity": quantity},
            {"id": service_id, "quantity": quantity},
            {"product": service_id, "qty": quantity},
            {"access_token": api_key, "productId": service_id, "quantity": quantity},
            {"key": api_key, "action": "add", "service": service_id, "link": link, "quantity": quantity},
        ]
    )
    attempts: list[tuple[str, dict[str, Any]]] = []
    for payload in payloads:
        attempts.append(("POST", {"json": payload}))
        # Form body only for legacy panels — skip for request_id JSON shape to cut noise.
        if "request_id" not in payload and "external_order_id" not in payload:
            attempts.append(("POST", {"data": payload}))
    # No GET "purchase" attempts — GET /products|/orders often returns HTTP 200
    # catalogs that were wrongly treated as placed orders.
    return attempts


def _normalize_status_response(body: Any) -> dict[str, Any]:
    """SMM panels may return a bare status string instead of JSON object."""
    if isinstance(body, dict):
        return body
    if isinstance(body, str):
        text = body.strip()
        if text:
            return {"status": text}
    if body not in (None, ""):
        return {"status": str(body)}
    return {}


async def get_status(provider: Provider, provider_order_id: str) -> dict[str, Any]:
    if _is_supabase_reseller_api(provider.api_url):
        return await _get_reseller_order_details(provider, provider_order_id)
    try:
        return await get_order_details(provider, provider_order_id)
    except ProviderApiError:
        body = await _post(provider, {"action": "status", "order": provider_order_id})
        return _normalize_status_response(body)


# ---------------------------------------------------------------------------
# Provider account / wallet (balance + username for admin Providers table)
# ---------------------------------------------------------------------------

_BALANCE_KEYS = (
    "balance",
    "wallet",
    "wallet_balance",
    "usdt_balance",
    "balance_usdt",
    "balance_usd",
    "credit",
    "credits",
    "credit_balance",
    "current_balance",
    "remaining",
    "remaining_balance",
    "available_balance",
    "funds",
    "amount",
    "money",
    "points",
    "point",
    "usdt",
    "usd",
    "so_du",
)
_USERNAME_KEYS = (
    "username",
    "bot_username",
    "user_name",
    "telegram_username",
    "telegram",
    "bot",
    "name",
)


def _parse_money(value: Any) -> float | None:
    """Parse balance amounts like 12.5, '12.5', '$12.50', '12,50 USDT'."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        nested = _pick_number(value, ("usdt", "usd", "balance", "amount", "available", "value"), default=0.0)
        return float(nested) if nested is not None else None
    text = str(value).strip()
    if not text:
        return None
    # Keep first number with optional decimal (handles $ / USDT / commas).
    match = re.search(r"-?\d+(?:[.,]\d+)?", text.replace(",", ""))
    if not match:
        # Retry with European decimal comma: 12,50
        match = re.search(r"-?\d+,\d+", text)
        if not match:
            return None
        try:
            return float(match.group(0).replace(",", "."))
        except ValueError:
            return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _balance_attempts(provider: Provider) -> list[tuple[str, dict[str, Any]]]:
    """Small set of common wallet endpoints — keep short so sync stays fast."""
    # Plain GET first: Balance URL may already be ...?action=balance (Rexovaan).
    attempts: list[tuple[str, dict[str, Any]]] = [
        ("GET", {}),
        ("GET", {"params": {"action": "balance"}}),
        ("GET", {"params": {"action": "account"}}),
    ]
    if not provider.api_key:
        return attempts
    api_key = provider.api_key.strip().strip('"').strip("'")
    attempts.extend(
        [
            ("GET", {"params": {"action": "balance", "key": api_key}}),
            ("GET", {"params": {"action": "balance", "api_key": api_key}}),
            ("GET", {"params": {"action": "balance", "access_token": api_key}}),
            ("GET", {"params": {"action": "account", "key": api_key}}),
            ("POST", {"data": {"key": api_key, "action": "balance"}}),
            ("POST", {"json": {"api_key": api_key, "action": "balance"}}),
            ("POST", {"json": {"access_token": api_key, "action": "balance"}}),
        ]
    )
    return attempts


def _extract_balance(body: Any) -> float | None:
    """Walk JSON for a numeric wallet/balance field (case-insensitive, nested)."""
    direct = _parse_money(body)
    if isinstance(body, (int, float, str)) and direct is not None:
        return direct
    if not isinstance(body, dict):
        return None

    # Exact known keys first (case-insensitive).
    lowered = {str(k).lower(): v for k, v in body.items()}
    for key in _BALANCE_KEYS:
        if key not in lowered:
            continue
        found = _parse_money(lowered.get(key))
        if found is not None:
            return found

    for wrap in ("data", "user", "account", "wallet", "result", "profile", "payload", "info", "response"):
        inner = lowered.get(wrap)
        if isinstance(inner, dict):
            found = _extract_balance(inner)
            if found is not None:
                return found
        else:
            found = _parse_money(inner)
            if found is not None and wrap in {"data", "result", "wallet", "balance", "payload"}:
                return found

    # Deep scan: any key name that looks like a wallet field.
    return _deep_balance_scan(body)


def _deep_balance_scan(obj: Any, depth: int = 0) -> float | None:
    if depth > 5 or obj is None:
        return None
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_l = str(key).lower()
            if any(token in key_l for token in ("balance", "credit", "wallet", "fund", "usdt", "usd")):
                # Avoid product stock "available" / pricing noise.
                if key_l in {"available", "price", "rate", "cost", "amount_sold"}:
                    continue
                found = _parse_money(value)
                if found is not None:
                    return found
                if isinstance(value, dict):
                    found = _deep_balance_scan(value, depth + 1)
                    if found is not None:
                        return found
            elif isinstance(value, (dict, list)):
                found = _deep_balance_scan(value, depth + 1)
                if found is not None:
                    return found
    elif isinstance(obj, list):
        for item in obj[:20]:
            found = _deep_balance_scan(item, depth + 1)
            if found is not None:
                return found
    return None


def _balance_body_preview(body: Any) -> str:
    """Short debug preview when balance can't be parsed."""
    if isinstance(body, dict):
        keys = ",".join(list(body.keys())[:12])
        return f"keys=[{keys}] body={str(body)[:180]}"
    return str(body)[:200]


def _extract_username(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in _USERNAME_KEYS:
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            text = val.strip()
            if key in {"telegram", "bot"} and not text.startswith("@"):
                if "t.me/" in text:
                    return "@" + text.split("t.me/")[-1].split("/")[0].split("?")[0]
                return f"@{text.lstrip('@')}"
            return text
    for wrap in ("data", "user", "account", "profile", "result"):
        inner = body.get(wrap)
        if isinstance(inner, dict):
            found = _extract_username(inner)
            if found:
                return found
    return None


def _balance_candidate_urls(api_url: str | None, balance_url: str | None = None) -> list[str]:
    """Build balance endpoint candidates for shop bots / reseller panels.

    SafwanTiger / Dodi / similar: GET {api_base}/balance
    Tunvn / mailreader reseller: GET {base}?action=balance  (prefer before /balance path)
    MMOStore-style: GET {api_base}/balance or /account
    Custom Balance URL (admin): tried first as-is.
    """
    urls: list[str] = []
    if balance_url and balance_url.strip():
        urls.append(_normalize_url(balance_url.strip()))

    source = (balance_url or api_url or "").strip()
    if not source:
        return urls

    raw = _normalize_url(source)
    path_only = raw.split("?", 1)[0].rstrip("/")
    lower = raw.lower()
    is_reseller_query = (
        "reseller" in lower
        or "action=" in lower
        or "reseller-api" in lower
        or "/functions/v1/" in lower
    )

    # Strip catalog suffixes so …/api/products → …/api
    base = path_only
    for suffix in ("/products", "/services", "/purchase", "/buy", "/orders", "/order"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break

    # Reseller / supabase: action=balance first (mailreader + Rexovaan).
    if is_reseller_query and path_only:
        urls.append(f"{path_only}?action=balance")

    if base:
        # Shop bots (SafwanTiger docs): GET /api/balance
        if not is_reseller_query:
            urls.append(f"{base}/balance")
            urls.append(f"{base}?action=balance")
        else:
            # Still try path form after query form.
            urls.append(f"{base}?action=balance")
            urls.append(f"{base}/balance")
        urls.extend(
            [
                f"{base}/account",
                f"{base}/me",
                f"{base}/wallet",
                f"{base}/profile",
            ]
        )
        if base.endswith("/api"):
            urls.append(f"{base}/v1/balance")

    if "/products" in path_only:
        urls.append(path_only.replace("/products", "/balance", 1))
        urls.append(path_only.replace("/products", "/account", 1))

    seen: set[str] = set()
    out: list[str] = []
    for candidate in urls:
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def _balance_auth_header_variants(provider: Provider) -> list[dict[str, str]]:
    """Try a few auth header styles — some panels reject combined Bearer+X-API-Key."""
    base = {"Accept": "application/json"}
    if not provider.api_key:
        return [base]
    api_key = provider.api_key.strip().strip('"').strip("'")
    return [
        {**base, "Authorization": f"Bearer {api_key}", "X-API-Key": api_key},
        {**base, "Authorization": f"Bearer {api_key}"},
        {**base, "X-API-Key": api_key},
        {**base, "x-api-key": api_key},
        {**base, "Authorization": f"Bearer {api_key}", "apikey": api_key},
    ]


async def _try_balance_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """GET one balance URL. Raises ProviderApiError if not a usable wallet payload."""
    response = await client.get(url, headers=headers, params=params or None)
    if response.status_code >= 400:
        raise ProviderApiError(_response_summary(response, "GET"))
    body = _parse_json_response(response)
    if _is_api_discovery_doc(body):
        raise ProviderApiError("discovery doc (not balance)")
    err = _provider_error_message(body)
    if err:
        raise ProviderApiError(err)
    balance = _extract_balance(body)
    username = _extract_username(body)
    if balance is None and username is None:
        raise ProviderApiError(f"no balance field ({_balance_body_preview(body)})")
    return {"balance": balance, "username": username}


async def _try_balance_post(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = await client.post(url, headers=headers, json=json_body or {})
    if response.status_code >= 400:
        raise ProviderApiError(_response_summary(response, "POST"))
    body = _parse_json_response(response)
    if _is_api_discovery_doc(body):
        raise ProviderApiError("discovery doc (not balance)")
    err = _provider_error_message(body)
    if err:
        raise ProviderApiError(err)
    balance = _extract_balance(body)
    username = _extract_username(body)
    if balance is None and username is None:
        raise ProviderApiError(f"no balance field ({_balance_body_preview(body)})")
    return {"balance": balance, "username": username}


async def _get_reseller_balance(provider: Provider, source_url: str) -> dict[str, Any]:
    """Rexovaan / Supabase / mailreader reseller: GET/POST {base}?action=balance."""
    base = _reseller_api_base(source_url)
    url = f"{base}?action=balance"
    timeout = min(float(os.getenv("API_TIMEOUT", "30")), 12.0)
    errors: list[str] = []
    async with _http_client(timeout=timeout) as client:
        for headers in _balance_auth_header_variants(provider):
            try:
                return await _try_balance_get(client, url, headers=headers)
            except (httpx.HTTPError, ProviderApiError) as exc:
                errors.append(str(exc)[:160])
            try:
                return await _try_balance_post(client, url, headers=headers, json_body={})
            except (httpx.HTTPError, ProviderApiError) as exc:
                errors.append(f"POST {exc}"[:160])
        # Some panels also accept the key in the query string.
        if provider.api_key:
            api_key = provider.api_key.strip().strip('"').strip("'")
            headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
            for params in ({"key": api_key}, {"access_token": api_key}, {"api_key": api_key}):
                try:
                    return await _try_balance_get(client, url, headers=headers, params=params)
                except (httpx.HTTPError, ProviderApiError) as exc:
                    errors.append(str(exc)[:160])
    # Prefer the most informative error (reason first, not a long URL).
    raise ProviderApiError(
        "Reseller balance failed: " + (" | ".join(errors[:3]) if errors else "unknown")
    )


async def fetch_provider_account(provider: Provider) -> dict[str, Any]:
    """Fetch upstream wallet balance + username when the provider API exposes them.

    Uses a short timeout and few attempts so a missing balance endpoint cannot
    slow down (or block) product sync for long.
    """
    api_url = (provider.api_url or "").strip()
    balance_url = (provider.balance_url or "").strip()
    if not api_url and not balance_url:
        raise ProviderApiError("Provider balance URL is missing")

    # canboso.com (telegram-buyer): same rate-limit problem as the catalog fetch
    # above — the generic candidate loop below tries ~6 URLs x 5 header
    # variants (30 requests) every sync, which alone is enough to trip
    # canboso's 429 limiter even after the catalog fetch was capped to 1
    # request. canboso's /products endpoint authenticates with a plain
    # "?key=..." query param, so try the balance endpoint the same way first
    # (2 lightweight attempts) and give up quietly if that's not it — balance
    # is best-effort and must never cost this provider its rate limit budget.
    if "telegram-buyer" in (api_url or balance_url).lower() and provider.api_key:
        api_key = provider.api_key.strip().strip('"').strip("'")
        base = _api_base_url(api_url or balance_url)
        timeout = min(float(os.getenv("API_TIMEOUT", "30")), 12.0)
        errors: list[str] = []
        async with _http_client(timeout=timeout) as client:
            try:
                await _canboso_throttle()
                return await _try_balance_get(client, f"{base}/balance", params={"key": api_key})
            except (httpx.HTTPError, ProviderApiError) as exc:
                errors.append(str(exc)[:160])
            try:
                await _canboso_throttle()
                return await _try_balance_get(
                    client, f"{base}/balance", headers={"Accept": "application/json", "X-API-Key": api_key}
                )
            except (httpx.HTTPError, ProviderApiError) as exc:
                errors.append(str(exc)[:160])
        raise ProviderApiError("Could not read provider balance/account. " + " | ".join(errors[:2]))

    # Rexovaan / mailreader / supabase-style: clean ?action=balance with auth variants.
    if (
        _is_supabase_reseller_api(balance_url)
        or _is_supabase_reseller_api(api_url)
        or "reseller" in (balance_url or api_url).lower()
        or "action=" in (balance_url or api_url).lower()
    ):
        try:
            return await _get_reseller_balance(provider, balance_url or api_url)
        except ProviderApiError:
            pass

    timeout = min(float(os.getenv("API_TIMEOUT", "30")), 15.0)
    errors: list[str] = []
    candidates = _balance_candidate_urls(api_url, balance_url)

    async with _http_client(timeout=timeout) as client:
        for url in candidates:
            for headers in _balance_auth_header_variants(provider):
                try:
                    return await _try_balance_get(client, url, headers=headers)
                except (httpx.HTTPError, ProviderApiError) as exc:
                    # Keep reason first so admin flash message isn't cut mid-URL.
                    errors.append(str(exc)[:180])

        # Legacy probes on the configured Balance/API URL (query-param style).
        probe_url = _normalize_url(balance_url or api_url)
        headers = _auth_headers(provider)
        for method, kwargs in _balance_attempts(provider):
            try:
                response = await client.request(method, probe_url, headers=headers, **kwargs)
                if response.status_code >= 400:
                    errors.append(_response_summary(response, method))
                    continue
                body = _parse_json_response(response)
                if _is_api_discovery_doc(body):
                    errors.append(f"{method}: discovery doc")
                    continue
                err = _provider_error_message(body)
                if err:
                    errors.append(f"{method}: provider error: {err}")
                    continue
                balance = _extract_balance(body)
                username = _extract_username(body)
                if balance is not None or username:
                    return {"balance": balance, "username": username}
                errors.append(f"{method}: JSON ok but no balance field ({str(body)[:80]})")
            except (httpx.HTTPError, ProviderApiError) as exc:
                errors.append(f"{method}: {exc}")

    raise ProviderApiError(
        "Could not read provider balance/account. "
        + (" | ".join(errors[:4]) if errors else "No balance endpoint matched.")
    )
