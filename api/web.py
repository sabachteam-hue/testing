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
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload, selectinload

from database.models import (
    BotConfig,
    Category,
    Order,
    PaymentMethod,
    ProductSale,
    Service,
    User,
    get_active_languages,
    get_active_payment_methods,
    get_db,
)
from utils.helpers import generate_order_code, get_mini_app_url, get_public_base_url, parse_icon
from utils.rate_limiter import check_rate_limit
from utils.security import hash_password, verify_password
from utils.stock_display import effective_available_qty
from utils.stock_manager import InsufficientStockError, reserve_stock

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


def _public_user(user: User) -> dict:
    return {
        "id": user.id,
        "name": user.full_name or "",
        "email": user.email or "",
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

    return {"ok": True, "user": _public_user(user)}


@router.post("/checkout")
def web_checkout(body: CheckoutBody, db: Session = Depends(get_db)) -> dict:
    if not body.items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cart is empty")
    method = (
        db.query(PaymentMethod)
        .filter(PaymentMethod.is_active.is_(True), PaymentMethod.code == body.payment_method)
        .first()
    )
    if not method:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose a valid payment method")
    user = _get_or_create_web_user(db, body.email, body.name, body.password)
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
