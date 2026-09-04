"""Public catalog + Mini App checkout for the Telegram shop / website.

  GET  /api/web/shop
  GET  /api/web/featured
  GET  /api/web/categories
  GET  /api/web/products?category_id=&q=
  GET  /api/web/products/{sku}
  GET  /api/web/stats
  GET  /api/web/payment-methods
  POST /api/web/signup
  POST /api/web/login
  POST /api/web/checkout
  GET  /api/web/orders/{code}

Prices are the admin sell_price shown in Telegram.
"""

from __future__ import annotations

import html
import os
import re
import time
from datetime import datetime
from urllib.parse import urlparse
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, joinedload, selectinload

from database.models import (
    BotConfig,
    Category,
    Claim,
    GrantedAccount,
    IssueReport,
    Order,
    PaymentMethod,
    ProductSale,
    Service,
    Transaction,
    User,
    get_active_languages,
    get_active_payment_methods,
    get_db,
)
from utils.claims_workflow import (
    create_customer_claim,
    format_claim_payload,
    get_open_claim_for_account,
)
from utils.granted_accounts import (
    calculate_account_refund_estimate,
    format_customer_transaction,
    format_granted_account_payload,
    sync_granted_accounts_for_order,
    sync_user_granted_accounts,
)
from utils.helpers import generate_order_code, get_mini_app_url, get_public_base_url, parse_icon
from utils.rate_limiter import check_rate_limit
from utils.security import (
    hash_password,
    safe_upload_filename,
    validate_claim_evidence_upload,
    verify_password,
)
from utils.stock_display import effective_available_qty
from utils.stock_manager import InsufficientStockError, reserve_stock
from utils.storage import get_upload_dir

router = APIRouter(prefix="/api/web", tags=["web-catalog"])

_HTML_TAG = re.compile(r"<[^>]+>")
_TG_EMOJI = re.compile(r"</?tg-emoji[^>]*>", re.IGNORECASE)
_SLUG_KEEP = re.compile(r"[^a-z0-9]+")
_DEFAULT_TELEGRAM_ORIGINS = (
    "https://web.telegram.org",
    "https://k.telegram.org",
)
_DEV_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)
_SHOP_NAME = "SMF SHOP"
_SHOP_EYEBROW = "Live premium catalog"
_SHOP_HEADLINE = "Premium plans, without the wait."
_SHOP_TAGLINE = "AI tools, streaming, and SaaS accounts — live stock, Telegram prices."
_FEATURED_LIMIT = 4
_LANG_FLAG_ISO = {
    "en": "gb",
    "es": "es",
    "ar": "sa",
    "hi": "in",
    "ru": "ru",
    "vi": "vn",
    "zh": "cn",
    "fa": "ir",
    "id": "id",
    "ko": "kr",
    "ur": "pk",
    "fr": "fr",
    "de": "de",
    "tr": "tr",
    "pt": "pt",
}
_CURRENCY_FLAG_ISO = {
    "USD": "us",
    "PKR": "pk",
    "EUR": "eu",
    "GBP": "gb",
    "INR": "in",
}


def _plain_text(value: str | None) -> str:
    text = _TG_EMOJI.sub("", value or "")
    text = _HTML_TAG.sub("", text)
    return html.unescape(" ".join(text.split())).strip()


def _slugify(name: str | None, fallback: str) -> str:
    slug = _SLUG_KEEP.sub("-", (name or "").lower()).strip("-")
    return slug or fallback


def _origin_of(url: str | None) -> str | None:
    parsed = urlparse((url or "").strip())
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def cors_allow_origins() -> list[str] | str:
    """Origins allowed to call /api/web from the Mini App / Vercel storefront.

    Catalog is public GET-only. Empty CORS_ORIGINS (or *) allows any origin so a
    separately hosted Mini App works without a Railway restart. Set CORS_ORIGINS
    to a comma-separated list to lock it down.
    """
    raw = (os.getenv("CORS_ORIGINS") or "").strip()
    if not raw or raw == "*":
        return "*"
    origins: list[str] = []
    for part in raw.split(","):
        origin = _origin_of(part) or part.strip().rstrip("/")
        if origin:
            origins.append(origin)
    for extra in _DEFAULT_TELEGRAM_ORIGINS + _DEV_ORIGINS:
        if extra not in origins:
            origins.append(extra)
    mini = _origin_of(get_mini_app_url())
    if mini and mini not in origins:
        origins.append(mini)
    return origins


def _public_base(request: Request | None = None) -> str:
    base = get_public_base_url()
    if base:
        return base.rstrip("/")
    if request is not None:
        return str(request.base_url).rstrip("/")
    return ""


def absolute_media_url(path: str | None, request: Request | None = None) -> str | None:
    value = (path or "").strip()
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    if not value.startswith("/"):
        value = f"/{value}"
    base = _public_base(request)
    return f"{base}{value}" if base else value


def _display_emoji(value: str | None, fallback: str) -> str:
    _, display = parse_icon(value, fallback)
    return display or fallback


def _active_sale(service: Service, now: datetime | None = None) -> ProductSale | None:
    now = now or datetime.utcnow()
    for sale in getattr(service, "sales", None) or []:
        if not sale.is_active:
            continue
        if sale.starts_at and sale.starts_at > now:
            continue
        if sale.ends_at and sale.ends_at <= now:
            continue
        return sale
    return None


def _warranty_percent(warranty: str | None) -> int | None:
    if not warranty:
        return None
    match = re.search(r"(\d+)", warranty)
    if not match:
        return 70
    amount = int(match.group(1))
    lowered = warranty.lower()
    if "year" in lowered or amount >= 12:
        return 100
    if amount >= 6:
        return 85
    if amount >= 3:
        return 75
    if amount >= 2:
        return 70
    return 50


def _delivery_type(service: Service) -> str:
    fulfillment = (getattr(service, "fulfillment_type", None) or "auto").lower()
    return "manual" if fulfillment == "manual" else "instant"


def _delivery_note(service: Service) -> str:
    fulfillment = (getattr(service, "fulfillment_type", None) or "auto").lower()
    if fulfillment == "manual":
        return "Manual fulfillment — admin completes this order."
    if fulfillment == "stock":
        return "Account details delivered instantly after payment."
    return "Instant delivery after payment."


def _whatsapp_url(raw: str | None) -> str | None:
    digits = re.sub(r"\D+", "", raw or "")
    return f"https://wa.me/{digits}" if digits else None


def _infer_platform(name: str, description: str) -> str:
    text = f"{name} {description}".lower()
    if any(token in text for token in ("android", "ios", "mobile app", "apk")):
        return "mobile"
    if any(token in text for token in ("windows", "macos", "desktop", "pc only")):
        return "desktop"
    if any(token in text for token in ("web", "browser", "chatgpt", "netflix", "spotify", "canva")):
        return "web"
    return "multi"


def category_payload(category: Category) -> dict:
    return {
        "id": category.id,
        "name": _plain_text(category.name) or category.name,
        "emoji": _display_emoji(category.emoji, "📦"),
        "slug": _slugify(_plain_text(category.name) or category.name, f"cat-{category.id}"),
        "description": _plain_text(category.description) or None,
        "sort_order": int(category.sort_order or 0),
    }


def product_payload(service: Service, request: Request | None = None) -> dict:
    available = effective_available_qty(service)
    in_stock = available > 0
    sale = _active_sale(service)
    sell_price = float(service.sell_price or 0)
    original_price = None
    if sale and sale.original_price is not None:
        original = float(sale.original_price)
        if original > sell_price:
            original_price = original
    warranty = _plain_text(service.warranty) or None
    description = _plain_text(service.description)
    name = _plain_text(service.name) or service.name
    category = service.category
    is_free = sell_price <= 0
    badges: list[str] = []
    if in_stock:
        badges.append("live")
    if original_price is not None:
        badges.append("hot")
    sale_ends_at = None
    if sale and sale.ends_at:
        sale_ends_at = sale.ends_at.isoformat() + "Z"
    return {
        "id": service.id,
        "sku": service.sku,
        "name": name,
        "description": description or None,
        "sell_price": sell_price,
        "original_price": original_price,
        "is_free": is_free,
        "category_id": category.id if category else None,
        "category": (_plain_text(category.name) or category.name) if category else None,
        "emoji": _display_emoji(service.emoji, "🛍️"),
        "image_url": absolute_media_url(service.image_path, request),
        "min_qty": int(service.min_qty or 1),
        "max_qty": int(service.max_qty or 1),
        "stock": available,
        "in_stock": in_stock,
        "stock_label": "In stock" if in_stock else "Out of stock",
        "platform": _infer_platform(name, description),
        "note": _delivery_note(service),
        "warranty_label": warranty,
        "warranty_percent": _warranty_percent(warranty),
        "delivery_type": _delivery_type(service),
        "badges": badges,
        "sale_ends_at": sale_ends_at,
    }


def _active_products_query(db: Session):
    return (
        db.query(Service)
        .options(
            joinedload(Service.category),
            joinedload(Service.stock),
            selectinload(Service.sales),
        )
        .filter(Service.is_active.is_(True), Service.is_deleted.is_(False))
    )


def shop_payload(db: Session) -> dict:
    """Mini App chrome: brand, languages, WhatsApp. No live FX ticker."""
    config = db.query(BotConfig).first()
    languages = [
        {
            "code": lang.code,
            "name": lang.name,
            "flag": lang.flag or "🌐",
            "flag_iso": _LANG_FLAG_ISO.get((lang.code or "").lower(), "xx"),
        }
        for lang in get_active_languages(db)
    ]
    if not languages:
        languages = [{"code": "en", "name": "English", "flag": "🇬🇧", "flag_iso": "gb"}]
    whatsapp = _whatsapp_url(getattr(config, "support_whatsapp", None) if config else None)
    support_url = ((getattr(config, "support_url", None) if config else None) or "").strip() or None
    username = ((getattr(config, "support_username", None) if config else None) or "").strip() or None
    pkr_rate = float(getattr(config, "usd_to_pkr_rate", None) or 280.0) if config else 280.0
    currencies = [
        {
            "code": "USD",
            "symbol": "$",
            "label": "USD ($)",
            "flag": "🇺🇸",
            "flag_iso": _CURRENCY_FLAG_ISO["USD"],
        },
        {
            "code": "PKR",
            "symbol": "Rs.",
            "label": "PKR (Rs.)",
            "flag": "🇵🇰",
            "flag_iso": _CURRENCY_FLAG_ISO["PKR"],
        },
    ]
    return {
        "name": _SHOP_NAME,
        "eyebrow": _SHOP_EYEBROW,
        "headline": _SHOP_HEADLINE,
        "tagline": _SHOP_TAGLINE,
        "whatsapp_url": whatsapp,
        "support_url": support_url,
        "support_username": username,
        "currency": currencies[0],
        "currencies": currencies,
        "pkr_rate": pkr_rate,
        "languages": languages,
    }


def _best_seller_ids(db: Session, limit: int = _FEATURED_LIMIT) -> list[int]:
    rows = (
        db.query(Order.service_id, func.count(Order.id))
        .filter(Order.status == "completed")
        .group_by(Order.service_id)
        .order_by(func.count(Order.id).desc())
        .limit(limit)
        .all()
    )
    ids: list[int] = []
    for row in rows:
        sid = row[0] if isinstance(row, (tuple, list)) else getattr(row, "service_id", None)
        if sid:
            ids.append(int(sid))
    return ids


def build_featured(
    services: list,
    db: Session,
    request: Request | None = None,
    limit: int = _FEATURED_LIMIT,
) -> dict:
    """Three separate Mini App carts: Live, Hot, Best Seller."""
    products = [product_payload(service, request) for service in services]
    live = [item for item in products if item["in_stock"]][:limit]
    hot = [item for item in products if "hot" in (item.get("badges") or [])][:limit]
    by_id = {item["id"]: item for item in products}
    best = [by_id[sid] for sid in _best_seller_ids(db, limit) if sid in by_id]
    if not best:
        best = products[:limit]
    for item in best:
        badges = list(item.get("badges") or [])
        if "best_seller" not in badges:
            badges.append("best_seller")
            item["badges"] = badges
    return {
        "live": live,
        "hot": hot,
        "best_seller": best[:limit],
    }


@router.get("/categories")
def list_categories(db: Session = Depends(get_db)) -> list[dict]:
    rows = (
        db.query(Category)
        .filter(Category.is_active.is_(True))
        .order_by(Category.sort_order.asc(), Category.name.asc())
        .all()
    )
    return [category_payload(row) for row in rows]


@router.get("/products")
def list_products(
    request: Request,
    db: Session = Depends(get_db),
    category_id: int | None = Query(None),
    q: str | None = Query(None),
) -> list[dict]:
    query = _active_products_query(db)
    if category_id is not None:
        query = query.filter(Service.category_id == category_id)
    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        query = query.filter(
            or_(
                Service.name.ilike(like),
                Service.sku.ilike(like),
                Service.description.ilike(like),
                Service.category.has(Category.name.ilike(like)),
            )
        )
    rows = query.order_by(Service.sort_order.asc(), Service.name.asc()).all()
    return [product_payload(row, request) for row in rows]


@router.get("/products/{sku}")
def get_product(sku: str, request: Request, db: Session = Depends(get_db)) -> dict:
    service = (
        _active_products_query(db)
        .filter(Service.sku == sku)
        .first()
    )
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return product_payload(service, request)


@router.get("/shop")
def shop_info(db: Session = Depends(get_db)) -> dict:
    return shop_payload(db)


@router.get("/featured")
def featured_products(request: Request, db: Session = Depends(get_db)) -> dict:
    rows = _active_products_query(db).order_by(Service.sort_order.asc(), Service.name.asc()).all()
    return build_featured(rows, db, request)


@router.get("/stats")
def shop_stats(db: Session = Depends(get_db)) -> dict:
    customers = db.query(func.count(User.id)).scalar() or 0
    orders_completed = (
        db.query(func.count(Order.id)).filter(Order.status == "completed").scalar() or 0
    )
    config = db.query(BotConfig).first()
    rate = float(getattr(config, "usd_to_pkr_rate", None) or 280.0)
    return {
        "customers": int(customers),
        "orders_completed": int(orders_completed),
        "usd_to_pkr_rate": rate,
    }


def payment_method_payload(method: PaymentMethod, request: Request | None = None) -> dict:
    return {
        "id": method.id,
        "name": method.name,
        "code": method.code,
        "method_type": method.method_type,
        "network": method.network,
        "address": method.address,
        "icon": _display_emoji(method.icon, "💳"),
        "image_url": absolute_media_url(method.image_path, request),
        "instructions": _plain_text(method.instructions) or None,
    }


def _web_telegram_id(email: str) -> str:
    return f"web:{email.strip().lower()}"


CUSTOMER_USER_ID_KEY = "customer_user_id"
CUSTOMER_LOGGED_IN_KEY = "customer_logged_in"
CUSTOMER_LAST_ACTIVE_KEY = "customer_last_active"


def _has_session(request: Request) -> bool:
    return "session" in request.scope


def _set_customer_session(request: Request, user_id: int) -> None:
    if not _has_session(request):
        return
    request.session[CUSTOMER_USER_ID_KEY] = int(user_id)
    request.session[CUSTOMER_LOGGED_IN_KEY] = True
    request.session[CUSTOMER_LAST_ACTIVE_KEY] = time.time()


def _clear_customer_session(request: Request) -> None:
    if not _has_session(request):
        return
    request.session.pop(CUSTOMER_USER_ID_KEY, None)
    request.session.pop(CUSTOMER_LOGGED_IN_KEY, None)
    request.session.pop(CUSTOMER_LAST_ACTIVE_KEY, None)


def get_current_customer(request: Request, db: Session = Depends(get_db)) -> User:
    if not _has_session(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in.",
        )
    user_id = request.session.get(CUSTOMER_USER_ID_KEY)
    logged_in = request.session.get(CUSTOMER_LOGGED_IN_KEY)
    if not user_id or not logged_in:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in.",
        )
    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        _clear_customer_session(request)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Customer account not found.",
        )
    if user.is_banned:
        _clear_customer_session(request)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been suspended.",
        )
    request.session[CUSTOMER_LAST_ACTIVE_KEY] = time.time()
    return user


def get_optional_customer(request: Request, db: Session = Depends(get_db)) -> User | None:
    if not _has_session(request):
        return None
    user_id = request.session.get(CUSTOMER_USER_ID_KEY)
    logged_in = request.session.get(CUSTOMER_LOGGED_IN_KEY)
    if not user_id or not logged_in:
        return None
    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user or user.is_banned:
        _clear_customer_session(request)
        return None
    request.session[CUSTOMER_LAST_ACTIVE_KEY] = time.time()
    return user


def _public_user(user: User) -> dict:
    return {
        "id": user.id,
        "name": user.full_name or "",
        "email": user.email or "",
        "wallet_balance": float(user.wallet_usdt or 0.0),
    }


class SignupBody(BaseModel):
    name: str = ""
    email: str
    password: str


class LoginBody(BaseModel):
    email: str
    password: str


class CheckoutItem(BaseModel):
    sku: str
    qty: int = Field(1, ge=1, le=100)


class CheckoutBody(BaseModel):
    email: str
    name: str = ""
    password: str | None = None
    payment_method: str
    items: list[CheckoutItem]


def _get_or_create_web_user(db: Session, email: str, name: str = "", password: str | None = None) -> User:
    clean_email = (email or "").strip().lower()
    if not clean_email or "@" not in clean_email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A valid email is required")
    user = db.query(User).filter(User.email == clean_email).first()
    if user is None:
        user = db.query(User).filter(User.telegram_id == _web_telegram_id(clean_email)).first()
    if user is None:
        user = User(
            telegram_id=_web_telegram_id(clean_email),
            username=clean_email.split("@")[0][:80],
            full_name=(name or "").strip() or clean_email.split("@")[0],
            email=clean_email,
            password_hash=hash_password(password) if password else None,
            force_join_ok=True,
        )
        db.add(user)
        db.flush()
        return user
    if name and not user.full_name:
        user.full_name = name.strip()
    if password and not user.password_hash:
        user.password_hash = hash_password(password)
    if not user.email:
        user.email = clean_email
    return user


@router.get("/payment-methods")
def list_payment_methods(request: Request, db: Session = Depends(get_db)) -> list[dict]:
    return [payment_method_payload(row, request) for row in get_active_payment_methods(db)]


@router.post("/signup")
async def web_signup(request: Request, body: SignupBody, db: Session = Depends(get_db)) -> dict:
    client_ip = request.client.host if request.client else "unknown"
    allowed, retry_after = await check_rate_limit(f"web_signup:{client_ip}", limit=5, window_seconds=60)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many signup attempts. Try again in {retry_after} seconds.",
        )

    email = body.email.strip().lower()
    password = (body.password or "").strip()
    if len(password) < 6:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password must be at least 6 characters")
    existing = db.query(User).filter(User.email == email).first()
    if existing and existing.password_hash:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists")
    user = _get_or_create_web_user(db, email, body.name, password)
    user.password_hash = hash_password(password)
    db.commit()
    db.refresh(user)
    _set_customer_session(request, user.id)
    return {"ok": True, "user": _public_user(user)}


@router.post("/login")
async def web_login(request: Request, body: LoginBody, db: Session = Depends(get_db)) -> dict:
    client_ip = request.client.host if request.client else "unknown"
    allowed, retry_after = await check_rate_limit(f"web_login:{client_ip}", limit=10, window_seconds=60)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many login attempts. Try again in {retry_after} seconds.",
        )

    email = body.email.strip().lower()
    user = db.query(User).filter(User.email == email).first()
    is_valid, needs_rehash = verify_password(body.password, user.password_hash if user else None)
    if not user or not is_valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email or password is incorrect")
    if user.is_banned:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account is banned")

    # Transparent migration: upgrade legacy SHA-256 password hash to modern algorithm
    if needs_rehash:
        user.password_hash = hash_password(body.password)
        db.commit()

    _set_customer_session(request, user.id)
    return {"ok": True, "user": _public_user(user)}


@router.post("/logout")
def web_logout(request: Request) -> dict:
    _clear_customer_session(request)
    return {"ok": True}


@router.get("/me")
def web_me(request: Request, db: Session = Depends(get_db)) -> dict:
    user = get_optional_customer(request, db)
    if not user:
        return {"ok": False, "authenticated": False, "user": None}
    return {
        "ok": True,
        "authenticated": True,
        "user": _public_user(user),
    }


@router.get("/account/dashboard")
def web_account_dashboard(
    current_user: User = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> dict:
    # Ensure all user's fulfilled orders are registered as granted accounts
    sync_user_granted_accounts(db, current_user.id)

    now = datetime.utcnow()
    orders_count = (
        db.query(func.count(Order.id))
        .filter(Order.user_id == current_user.id)
        .scalar()
        or 0
    )
    active_accounts_count = (
        db.query(func.count(GrantedAccount.id))
        .filter(
            GrantedAccount.user_id == current_user.id,
            GrantedAccount.status == "active",
            GrantedAccount.subscription_expires_at > now,
        )
        .scalar()
        or 0
    )
    recent_orders = (
        db.query(Order)
        .options(joinedload(Order.service))
        .filter(Order.user_id == current_user.id)
        .order_by(Order.id.desc())
        .limit(5)
        .all()
    )
    open_claims_count = (
        db.query(func.count(IssueReport.id))
        .filter(
            IssueReport.user_id == current_user.id,
            IssueReport.status.in_([
                "pending_review", "pending", "under_review", "awaiting_evidence",
                "approved", "replacement_processing", "refund_processing", "support_in_progress"
            ]),
        )
        .scalar()
        or 0
    )
    return {
        "ok": True,
        "customer": {
            "id": current_user.id,
            "name": current_user.full_name or (current_user.email or "").split("@")[0],
            "email": current_user.email or "",
        },
        "stats": {
            "total_orders": int(orders_count),
            "active_accounts": int(active_accounts_count),
            "wallet_balance": float(current_user.wallet_usdt or 0.0),
            "open_claims": int(open_claims_count),
        },
        "recent_orders": [
            {
                "order_code": o.order_code,
                "product_name": _plain_text(o.service.name) if o.service else "Product",
                "amount": float(o.amount_usdt or 0.0),
                "status": o.status,
                "created_at": (
                    o.created_at.strftime("%b %d, %Y")
                    if getattr(o, "created_at", None)
                    else None
                ),
            }
            for o in recent_orders
        ],
    }


def _format_customer_order_status(status: str | None, is_preorder: bool = False) -> tuple[str, str]:
    st = (status or "").lower()
    if st == "completed":
        return "Completed", "completed"
    if st == "delivered":
        return "Delivered", "completed"
    if st == "refunded":
        return "Refunded", "refunded"
    if st == "preorder_waiting" or (is_preorder and st in ("pending", "manual_pending")):
        return "Pre-order — Waiting for Stock", "preorder"
    if st in ("processing", "stock_reserved"):
        return "Processing Fulfillment", "processing"
    if st in ("pending", "manual_pending"):
        return "Pending Confirmation", "pending"
    if st in ("cancelled", "canceled", "failed"):
        return "Cancelled", "cancelled"
    return (st.capitalize() or "Pending"), "pending"


@router.get("/account/orders")
def list_customer_orders(
    request: Request,
    status_filter: str = Query("all"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> dict:
    base_query = db.query(Order).filter(Order.user_id == current_user.id)

    filt = (status_filter or "").strip().lower()
    if filt == "active":
        base_query = base_query.filter(
            Order.status.in_(["pending", "manual_pending", "processing", "preorder_waiting", "stock_reserved"])
        )
    elif filt == "completed":
        base_query = base_query.filter(Order.status.in_(["completed", "delivered"]))
    elif filt == "refunded":
        base_query = base_query.filter(or_(Order.status == "refunded", Order.refunded_at.isnot(None)))
    elif filt == "cancelled":
        base_query = base_query.filter(Order.status.in_(["cancelled", "canceled", "failed"]))

    total_count = base_query.count()

    rows = (
        base_query.options(joinedload(Order.service))
        .order_by(Order.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    serialized = []
    for o in rows:
        svc = o.service
        is_pre = bool(getattr(o, "is_preorder", False)) or o.status == "preorder_waiting"
        is_ref = o.status == "refunded" or bool(getattr(o, "refunded_at", None))
        label, badge = _format_customer_order_status(o.status, is_pre)
        serialized.append(
            {
                "order_code": o.order_code,
                "sku": svc.sku if svc else None,
                "product_name": _plain_text(svc.name) if svc else "Product",
                "image_url": absolute_media_url(svc.image_path, request) if svc else None,
                "emoji": _display_emoji(svc.emoji if svc else None, "🛍️"),
                "quantity": int(o.quantity or 1),
                "amount_usdt": float(o.amount_usdt or 0.0),
                "currency": "USDT",
                "status": o.status,
                "status_label": label,
                "status_badge": badge,
                "payment_method": o.payment_method,
                "is_preorder": is_pre,
                "is_refunded": is_ref,
                "refund_amount": float(o.refund_amount) if o.refund_amount is not None else None,
                "refund_method": o.refund_method,
                "created_at": o.created_at.isoformat() if getattr(o, "created_at", None) else None,
                "created_at_display": (
                    o.created_at.strftime("%b %d, %Y %I:%M %p")
                    if getattr(o, "created_at", None)
                    else "—"
                ),
            }
        )

    return {
        "ok": True,
        "total": total_count,
        "limit": limit,
        "offset": offset,
        "orders": serialized,
    }


@router.get("/account/orders/{order_code}")
def get_customer_order_detail(
    order_code: str,
    request: Request,
    current_user: User = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> dict:
    clean_code = (order_code or "").strip()
    order = (
        db.query(Order)
        .options(joinedload(Order.service))
        .filter(Order.order_code == clean_code, Order.user_id == current_user.id)
        .first()
    )
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    svc = order.service
    is_pre = bool(getattr(order, "is_preorder", False)) or order.status == "preorder_waiting"
    is_ref = order.status == "refunded" or bool(getattr(order, "refunded_at", None))
    label, badge = _format_customer_order_status(order.status, is_pre)

    method = (
        db.query(PaymentMethod).filter(PaymentMethod.code == (order.payment_method or "")).first()
        if order.payment_method
        else None
    )

    # Fulfillment / Delivery details (safe for customers)
    st = (order.status or "").lower()
    if st in ("completed", "delivered"):
        delivery_status = "Delivered"
        delivery_info = (
            order.delivered_info.strip()
            if order.delivered_info
            else "Your account details and credentials have been granted."
        )
    elif is_pre:
        delivery_status = "Pre-order Waiting"
        delivery_info = "Your pre-order has been logged and paid. Account details will be delivered automatically as soon as new stock is added."
    elif st == "refunded":
        delivery_status = "Refunded"
        ref_details = []
        if order.refund_amount is not None:
            ref_details.append(f"${order.refund_amount:.2f} USDT")
        if order.refund_method:
            ref_details.append(f"via {order.refund_method.upper()}")
        delivery_info = f"Order was refunded {(' '.join(ref_details)) if ref_details else ''}."
    elif st in ("cancelled", "canceled", "failed"):
        delivery_status = "Cancelled"
        delivery_info = "This order was cancelled."
    else:
        delivery_status = "Pending Verification"
        delivery_info = "Payment confirmation in progress. Fulfillment will complete shortly."

    return {
        "ok": True,
        "order": {
            "order_code": order.order_code,
            "sku": svc.sku if svc else None,
            "product_name": _plain_text(svc.name) if svc else "Product",
            "emoji": _display_emoji(svc.emoji if svc else None, "🛍️"),
            "image_url": absolute_media_url(svc.image_path, request) if svc else None,
            "quantity": int(order.quantity or 1),
            "amount_usdt": float(order.amount_usdt or 0.0),
            "currency": "USDT",
            "status": order.status,
            "status_label": label,
            "status_badge": badge,
            "payment_method": order.payment_method,
            "payment_method_name": method.name if method else order.payment_method,
            "pay_to": method.address if method else None,
            "payment_network": method.network if method else None,
            "payment_instructions": _plain_text(method.instructions) if method else None,
            "delivery_status": delivery_status,
            "delivery_info": delivery_info,
            "is_preorder": is_pre,
            "is_refunded": is_ref,
            "refund_amount": float(order.refund_amount) if order.refund_amount is not None else None,
            "refund_method": order.refund_method,
            "refunded_at": (
                order.refunded_at.strftime("%b %d, %Y %I:%M %p")
                if getattr(order, "refunded_at", None)
                else None
            ),
            "created_at": (
                order.created_at.strftime("%b %d, %Y %I:%M %p")
                if getattr(order, "created_at", None)
                else "—"
            ),
            "claims": [
                format_claim_payload(c, request)
                for c in (
                    db.query(IssueReport)
                    .options(
                        joinedload(IssueReport.service),
                        joinedload(IssueReport.order),
                        joinedload(IssueReport.granted_account),
                        joinedload(IssueReport.replacement_account),
                    )
                    .filter(IssueReport.order_id == order.id, IssueReport.user_id == current_user.id)
                    .order_by(IssueReport.id.desc())
                    .all()
                )
            ],
        },
    }


@router.get("/account/granted-accounts")
def list_customer_granted_accounts(
    request: Request,
    status_filter: str = Query("all"),
    order_code: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> dict:
    # Ensure all user's fulfilled orders have granted account records
    sync_user_granted_accounts(db, current_user.id)

    now = datetime.utcnow()
    base_query = (
        db.query(GrantedAccount)
        .options(joinedload(GrantedAccount.service), joinedload(GrantedAccount.order))
        .filter(GrantedAccount.user_id == current_user.id)
    )

    if order_code and order_code.strip():
        clean_code = order_code.strip()
        base_query = base_query.join(Order).filter(Order.order_code == clean_code)

    filt = (status_filter or "").strip().lower()
    if filt == "active":
        base_query = base_query.filter(
            GrantedAccount.status == "active",
            GrantedAccount.subscription_expires_at > now,
        )
    elif filt == "expired":
        base_query = base_query.filter(
            or_(
                GrantedAccount.status == "expired",
                and_(
                    GrantedAccount.status == "active",
                    GrantedAccount.subscription_expires_at <= now,
                ),
            )
        )
    elif filt == "refunded":
        base_query = base_query.filter(GrantedAccount.status == "refunded")
    elif filt == "frozen":
        base_query = base_query.filter(GrantedAccount.status == "frozen")

    total_count = base_query.count()

    rows = (
        base_query.order_by(GrantedAccount.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    active_count = (
        db.query(func.count(GrantedAccount.id))
        .filter(
            GrantedAccount.user_id == current_user.id,
            GrantedAccount.status == "active",
            GrantedAccount.subscription_expires_at > now,
        )
        .scalar()
        or 0
    )

    serialized = [
        format_granted_account_payload(acc, acc.order, acc.service, request)
        for acc in rows
    ]

    return {
        "ok": True,
        "total": total_count,
        "active_count": int(active_count),
        "limit": limit,
        "offset": offset,
        "accounts": serialized,
    }


@router.get("/account/granted-accounts/{account_id}")
def get_customer_granted_account_detail(
    account_id: int,
    request: Request,
    current_user: User = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> dict:
    account = (
        db.query(GrantedAccount)
        .options(joinedload(GrantedAccount.service), joinedload(GrantedAccount.order))
        .filter(GrantedAccount.id == account_id, GrantedAccount.user_id == current_user.id)
        .first()
    )
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Granted account not found")

    return {
        "ok": True,
        "account": format_granted_account_payload(account, account.order, account.service, request),
    }


@router.get("/account/granted-accounts/{account_id}/refund-estimate")
def get_customer_granted_account_refund_estimate(
    account_id: int,
    current_user: User = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> dict:
    """Authoritative server-calculated pro-rata refund estimate for a customer's granted account."""
    account = (
        db.query(GrantedAccount)
        .options(joinedload(GrantedAccount.service), joinedload(GrantedAccount.order))
        .filter(GrantedAccount.id == account_id, GrantedAccount.user_id == current_user.id)
        .first()
    )
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Granted account not found")

    estimate = calculate_account_refund_estimate(account, account.order)
    return {
        "ok": True,
        "estimate": estimate,
    }


@router.get("/account/wallet")
def get_customer_wallet(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    type_filter: str = Query("all"),
    current_user: User = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> dict:
    """Fetch authenticated customer's wallet balance, summary totals, and transaction ledger."""
    balance = round(float(current_user.wallet_usdt or 0.0), 2)

    # Confirmed credits: refund, admin_credit, deposit
    total_credits = (
        db.query(func.sum(Transaction.amount))
        .filter(
            Transaction.user_id == current_user.id,
            Transaction.status == "confirmed",
            Transaction.tx_type.in_(["refund", "admin_credit", "credit", "deposit"]),
        )
        .scalar()
        or 0.0
    )

    # Confirmed debits: deduct, admin_debit, purchase
    total_debits = (
        db.query(func.sum(Transaction.amount))
        .filter(
            Transaction.user_id == current_user.id,
            Transaction.status == "confirmed",
            Transaction.tx_type.in_(["deduct", "admin_debit", "purchase", "order_payment"]),
        )
        .scalar()
        or 0.0
    )

    # Confirmed refunds
    total_refunds = (
        db.query(func.sum(Transaction.amount))
        .filter(
            Transaction.user_id == current_user.id,
            Transaction.status == "confirmed",
            Transaction.tx_type == "refund",
        )
        .scalar()
        or 0.0
    )

    query = db.query(Transaction).filter(Transaction.user_id == current_user.id)
    tf = (type_filter or "all").lower().strip()
    if tf == "credits":
        query = query.filter(Transaction.tx_type.in_(["refund", "admin_credit", "credit", "deposit"]))
    elif tf == "debits":
        query = query.filter(Transaction.tx_type.in_(["deduct", "admin_debit", "purchase", "order_payment"]))
    elif tf == "refunds":
        query = query.filter(Transaction.tx_type == "refund")

    total_count = query.count()
    rows = (
        query.order_by(Transaction.created_at.desc(), Transaction.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return {
        "ok": True,
        "balance": balance,
        "currency": "USDT",
        "total_credits": round(float(total_credits), 2),
        "total_debits": round(float(total_debits), 2),
        "total_refunds": round(float(total_refunds), 2),
        "total": total_count,
        "limit": limit,
        "offset": offset,
        "type_filter": tf,
        "transactions": [format_customer_transaction(tx) for tx in rows],
    }


# =====================================================================
# CUSTOMER CLAIMS / REPLACEMENT / REFUND ENDPOINTS (PHASE 5)
# =====================================================================

@router.get("/account/claims")
def list_customer_claims(
    request: Request,
    status_filter: str = Query("all"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> dict:
    open_statuses = [
        "pending_review",
        "pending",
        "under_review",
        "awaiting_evidence",
        "approved",
        "replacement_processing",
        "refund_processing",
        "support_in_progress",
    ]

    base_query = (
        db.query(IssueReport)
        .options(
            joinedload(IssueReport.service),
            joinedload(IssueReport.order),
            joinedload(IssueReport.granted_account),
            joinedload(IssueReport.replacement_account),
        )
        .filter(IssueReport.user_id == current_user.id)
    )

    sf = (status_filter or "all").lower().strip()
    if sf == "open":
        base_query = base_query.filter(IssueReport.status.in_(open_statuses))
    elif sf == "resolved":
        base_query = base_query.filter(IssueReport.status == "resolved")
    elif sf in ("rejected", "cancelled"):
        base_query = base_query.filter(IssueReport.status.in_(["rejected", "cancelled"]))

    total_count = base_query.count()
    open_count = (
        db.query(func.count(IssueReport.id))
        .filter(IssueReport.user_id == current_user.id, IssueReport.status.in_(open_statuses))
        .scalar()
        or 0
    )

    rows = (
        base_query.order_by(IssueReport.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return {
        "ok": True,
        "total": total_count,
        "open_count": int(open_count),
        "limit": limit,
        "offset": offset,
        "claims": [format_claim_payload(c, request) for c in rows],
    }


@router.get("/account/claims/{claim_id}")
def get_customer_claim_detail(
    claim_id: int,
    request: Request,
    current_user: User = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> dict:
    claim = (
        db.query(IssueReport)
        .options(
            joinedload(IssueReport.service),
            joinedload(IssueReport.order),
            joinedload(IssueReport.granted_account),
            joinedload(IssueReport.replacement_account),
        )
        .filter(IssueReport.id == claim_id)
        .first()
    )
    if not claim or claim.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found.")

    return {
        "ok": True,
        "claim": format_claim_payload(claim, request),
    }


@router.post("/account/claims")
async def submit_customer_claim(
    request: Request,
    current_user: User = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> dict:
    content_type = (request.headers.get("content-type") or "").lower()

    evidence_file: UploadFile | None = None
    if "multipart/form-data" in content_type:
        form_data = await request.form()
        granted_account_id = form_data.get("granted_account_id")
        resolution_preference = form_data.get("resolution_preference") or "replacement"
        stopped_working_at_str = form_data.get("stopped_working_at")
        problem_description = form_data.get("problem_description") or form_data.get("description") or ""
        file_field = form_data.get("evidence")
        if file_field and hasattr(file_field, "filename") and hasattr(file_field, "read"):
            evidence_file = file_field
    elif "application/json" in content_type:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body.")
        granted_account_id = body.get("granted_account_id")
        resolution_preference = body.get("resolution_preference") or "replacement"
        stopped_working_at_str = body.get("stopped_working_at")
        problem_description = body.get("problem_description") or body.get("description") or ""
    else:
        # Fallback form data
        form_data = await request.form()
        granted_account_id = form_data.get("granted_account_id")
        resolution_preference = form_data.get("resolution_preference") or "replacement"
        stopped_working_at_str = form_data.get("stopped_working_at")
        problem_description = form_data.get("problem_description") or form_data.get("description") or ""
        file_field = form_data.get("evidence")
        if file_field and hasattr(file_field, "filename") and hasattr(file_field, "read"):
            evidence_file = file_field

    if not granted_account_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="granted_account_id is required.")
    if not stopped_working_at_str:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Date account stopped working is required.")
    if not problem_description or len(str(problem_description).strip()) < 10:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Problem description must be at least 10 characters.")

    try:
        acc_id = int(granted_account_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid granted_account_id.")

    # Parse date
    try:
        date_raw = str(stopped_working_at_str).strip().replace("Z", "+00:00")
        if "T" in date_raw:
            parsed_date = datetime.fromisoformat(date_raw)
        else:
            parsed_date = datetime.strptime(date_raw[:10], "%Y-%m-%d")
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid date format for stopped_working_at. Expected YYYY-MM-DD or ISO format.",
        )

    # Validate evidence upload if provided
    evidence_url = None
    evidence_filename = None
    if evidence_file and getattr(evidence_file, "filename", None):
        content = await evidence_file.read()
        if content:
            valid, err = validate_claim_evidence_upload(content, evidence_file.filename)
            if not valid:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err)
            safe_name = safe_upload_filename("claim_evidence", evidence_file.filename)
            target_path = get_upload_dir("claims") / safe_name
            with open(target_path, "wb") as f:
                f.write(content)
            evidence_url = f"/admin/static/uploads/claims/{safe_name}"
            evidence_filename = Path(evidence_file.filename).name[:100]

    account = (
        db.query(GrantedAccount)
        .options(joinedload(GrantedAccount.order), joinedload(GrantedAccount.service))
        .filter(GrantedAccount.id == acc_id)
        .first()
    )
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found.")
    if account.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only file claims on your own accounts.")

    try:
        claim = create_customer_claim(
            db,
            user=current_user,
            granted_account=account,
            resolution_preference=str(resolution_preference),
            stopped_working_at=parsed_date,
            problem_description=str(problem_description),
            evidence_url=evidence_url,
            evidence_filename=evidence_filename,
        )
    except ValueError as val_err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(val_err))
    except PermissionError as perm_err:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(perm_err))

    return {
        "ok": True,
        "message": "Claim submitted successfully.",
        "claim": format_claim_payload(claim, request),
    }


@router.post("/checkout")
def web_checkout(
    body: CheckoutBody,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    if not body.items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cart is empty")
    method = (
        db.query(PaymentMethod)
        .filter(PaymentMethod.is_active.is_(True), PaymentMethod.code == body.payment_method)
        .first()
    )
    if not method:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose a valid payment method")

    # If logged in as customer, link order to authenticated customer unless a different valid email was specified
    logged_in_user = get_optional_customer(request, db)
    clean_email = (body.email or "").strip().lower()
    if logged_in_user and (not clean_email or clean_email == (logged_in_user.email or "").lower()):
        user = logged_in_user
    else:
        user = _get_or_create_web_user(db, body.email, body.name, body.password)
        # If user provided a password during checkout, establish session
        if body.password:
            _set_customer_session(request, user.id)

    created: list[dict] = []
    try:
        for item in body.items:
            service = (
                _active_products_query(db)
                .filter(Service.sku == item.sku)
                .first()
            )
            if not service:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Product not found: {item.sku}")
            unit = float(service.sell_price or 0)
            total = unit * item.qty
            reserve_stock(db, service.id, item.qty)
            order = Order(
                order_code=generate_order_code(db),
                user_id=user.id,
                service_id=service.id,
                link="web_mini_app_order",
                quantity=item.qty,
                amount_usdt=total,
                status="pending",
                order_type="manual",
                payment_method=method.code,
                customer_email=user.email,
                note=f"Web Mini App checkout via {method.name}",
            )
            db.add(order)
            db.flush()
            created.append(
                {
                    "order_code": order.order_code,
                    "sku": service.sku,
                    "name": _plain_text(service.name) or service.name,
                    "qty": item.qty,
                    "amount": total,
                    "status": order.status,
                }
            )
    except InsufficientStockError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.commit()
    return {
        "ok": True,
        "order_code": created[0]["order_code"],
        "orders": created,
        "total": sum(row["amount"] for row in created),
        "payment_method": payment_method_payload(method),
        "user": _public_user(user),
    }


@router.get("/orders/{code}")
def get_web_order(code: str, db: Session = Depends(get_db)) -> dict:
    order = db.query(Order).filter(Order.order_code == code).first()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    service = order.service
    method = (
        db.query(PaymentMethod).filter(PaymentMethod.code == (order.payment_method or "")).first()
        if order.payment_method
        else None
    )
    return {
        "order_code": order.order_code,
        "status": order.status,
        "qty": order.quantity,
        "amount": float(order.amount_usdt or 0),
        "payment_method": order.payment_method,
        "product": (_plain_text(service.name) if service else None) or None,
        "sku": service.sku if service else None,
        "instructions": _plain_text(method.instructions) if method else None,
        "pay_to": method.address if method else None,
        "method_name": method.name if method else order.payment_method,
        "network": method.network if method else None,
    }
