import logging
import os
import re
import shutil
import html
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from utils.security import (
    constant_time_compare,
    is_production,
    safe_upload_filename,
    validate_image_upload,
    verify_password,
)
from utils.rate_limiter import (
    clear_failures,
    is_locked_out,
    record_failure_and_check_lockout,
)
from utils.storage import get_upload_dir, resolve_file_path

from aiogram import Bot
from aiogram.types import FSInputFile
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from database.models import (
    Announcement,
    ApiKey,
    AuditLog,
    BotConfig,
    Category,
    DescriptionTemplate,
    IconPreset,
    IssueReport,
    Language,
    MenuCommand,
    Order,
    PaymentMethod,
    ProductSale,
    Provider,
    ReferralCode,
    ReferralEarning,
    RefundLog,
    Service,
    Stock,
    Transaction,
    User,
    UserProductDiscount,
    Webhook,
    get_db,
)
from admin.period_stats import (
    dashboard_period_stats,
    orders_period_stats,
    sold_accounts_period_stats,
    transactions_period_stats,
    users_period_stats,
)
from utils.audit import entity_display, log_admin_action
from utils.background_tasks import credit_referral_for_order, credit_referral_join_bonus
from utils.helpers import format_commission, generate_api_credentials, get_referral_settings, regenerate_api_credentials
from utils.notifications import broadcast_to_all_users, notify_channel_order_completed, notify_flash_sale, notify_issue_report_resolved, notify_new_product, notify_price_drop, notify_product_sale, notify_referrer_earning, notify_stock_added, notify_user_balance_change, notify_user_order_completed
from utils.provider_api import fetch_services
from utils.refund_tool import (
    calculate_refund,
    credit_wallet_refund,
    mark_manual_refund,
    money,
    notify_wallet_refund,
)
from utils.stock_manager import InsufficientStockError, add_stock, complete_reserved_stock, release_stock, set_stock
from utils.stock_display import apply_provider_stock
from utils.menu_commands import ensure_menu_commands
from utils.pricing import api_computed_sell_price, derive_api_markup_from_sell

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="admin/templates")


def admin_plain_text(value) -> str:
    """Strip Telegram <tg-emoji> wrappers for clean admin table display."""
    text = str(value or "")
    text = re.sub(r"</?tg-emoji[^>]*>", "", text)
    return " ".join(text.split()).strip()


def admin_emoji_glyph(value) -> str:
    """Show only the visible glyph for ID|fallback or plain emoji (never the numeric ID)."""
    text = str(value or "").strip()
    if "|" in text:
        left, _, right = text.partition("|")
        if left.strip().isdigit():
            return (right.strip() or "📦")
    plain = admin_plain_text(text)
    return plain or "📦"


def admin_emoji_html(value):
    """Admin UI: premium custom-emoji thumbnail when value is ID|fallback, else glyph."""
    from markupsafe import Markup

    text = str(value or "").strip()
    if "|" in text:
        left, _, right = text.partition("|")
        eid = left.strip()
        if eid.isdigit():
            fb = html.escape(right.strip() or "✨")
            return Markup(
                f'<img class="emoji-inline-thumb" src="/admin/custom-emoji/{eid}" '
                f'alt="{fb}" loading="lazy" width="22" height="22">'
            )
    # Render embedded <tg-emoji> tags as thumbs; escape the surrounding text.
    if "<tg-emoji" in text.lower():
        parts = re.split(r'(<tg-emoji\s+emoji-id="\d+">[^<]*</tg-emoji>)', text, flags=re.IGNORECASE)
        chunks: list[str] = []
        for part in parts:
            match = re.fullmatch(
                r'<tg-emoji\s+emoji-id="(\d+)">([^<]*)</tg-emoji>',
                part,
                flags=re.IGNORECASE,
            )
            if match:
                eid = match.group(1)
                fb = html.escape(match.group(2) or "✨")
                chunks.append(
                    f'<img class="emoji-inline-thumb" src="/admin/custom-emoji/{eid}" '
                    f'alt="{fb}" loading="lazy" width="22" height="22">'
                )
            else:
                chunks.append(html.escape(part))
        return Markup("".join(chunks) or "📦")
    plain = html.escape(admin_emoji_glyph(text))
    return Markup(plain or "📦")


templates.env.filters["admin_plain"] = admin_plain_text
templates.env.filters["admin_emoji"] = admin_emoji_html


def admin_icon_id(value) -> str:
    from utils.helpers import parse_icon

    text = str(value or "").strip()
    match = re.search(r'<tg-emoji\s+emoji-id="(\d+)">', text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    eid, _fb = parse_icon(text, "")
    return eid or ""


def admin_icon_fallback(value) -> str:
    from utils.helpers import parse_icon

    text = str(value or "").strip()
    match = re.search(
        r'<tg-emoji\s+emoji-id="\d+">([^<]*)</tg-emoji>',
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return (match.group(1) or "✨").strip() or "✨"
    _eid, fb = parse_icon(text, "✨")
    return fb or "✨"


templates.env.filters["admin_icon_id"] = admin_icon_id
templates.env.filters["admin_icon_fb"] = admin_icon_fallback

CATEGORY_UPLOAD_DIR = get_upload_dir("categories")
SERVICE_UPLOAD_DIR = get_upload_dir("services")
PAYMENT_METHOD_UPLOAD_DIR = get_upload_dir("payment_methods")
CUSTOM_EMOJI_UPLOAD_DIR = get_upload_dir("custom_emoji")


async def save_icon_image(icon_image: UploadFile | None, upload_dir: Path, prefix: str) -> str | None:
    """Category/Service/PaymentMethod upload logic with persistent storage and security validation."""
    if not icon_image or not icon_image.filename:
        return None
    try:
        content = await icon_image.read()
        valid, err_msg = validate_image_upload(content, icon_image.filename)
        if not valid:
            logger.warning("Rejected invalid image upload '%s': %s", icon_image.filename, err_msg)
            return None
        safe_name = safe_upload_filename(prefix, icon_image.filename)
        destination = upload_dir / safe_name
        destination.write_bytes(content)
        category_name = upload_dir.name
        return f"/admin/static/uploads/{category_name}/{safe_name}"
    except Exception as exc:
        logger.exception("Error saving uploaded image: %s", exc)
        return None


def _sidebar_badge_counts() -> dict:
    """Real (never hardcoded) unread/attention counts for the sidebar badges.
    Uses its own short-lived session so `render()` doesn't need a `db` param
    threaded through every existing route."""
    from database.models import SessionLocal

    session = SessionLocal()
    try:
        since_24h = datetime.utcnow() - timedelta(hours=24)
        pending_reports = session.query(IssueReport).filter(IssueReport.status == "pending").count()
        pending_transactions = session.query(Transaction).filter(Transaction.status == "pending").count()

        config = session.query(BotConfig).first()

        # Orders: badge already drops on its own as orders get completed
        # (status leaves processing/manual_pending). On top of that, opening
        # /admin/orders now also clears it like a notification — only orders
        # that were touched (created or status-changed) *after* the last time
        # the page was opened still count. No watermark yet (first ever load)
        # = no lower bound, so nothing already-pending vanishes unseen.
        orders_since = config and config.sidebar_seen_orders_at
        orders_query = session.query(Order).filter(Order.status.in_(["processing", "manual_pending"]))
        if orders_since:
            orders_query = orders_query.filter(Order.updated_at >= orders_since)
        orders_needing_attention = orders_query.count()

        # Users: same "since last opened" watermark, falling back to the
        # original fixed 24h window until /admin/users has been opened once.
        users_since = (config and config.sidebar_seen_users_at) or since_24h
        new_users = session.query(User).filter(User.joined_at >= users_since).count()

        # Revenue / Sold Accounts badges aren't a real pending queue (unlike
        # Orders) — they're "new activity" counters. So they use a
        # "since last opened" watermark instead of a fixed 24h window: once the
        # admin opens /admin/revenue or /admin/sold-accounts, the badge clears
        # like a normal unread-notification badge until fresh activity happens.
        revenue_since = (config and config.sidebar_seen_revenue_at) or since_24h
        sold_accounts_since = (config and config.sidebar_seen_sold_accounts_at) or since_24h

        revenue_activity = (
            session.query(Order)
            .filter(Order.status == "completed", Order.completed_at >= revenue_since)
            .count()
        )
        sold_units_24h = (
            session.query(func.coalesce(func.sum(Order.quantity), 0))
            .filter(Order.status == "completed", Order.completed_at >= sold_accounts_since)
            .scalar()
            or 0
        )
        return {
            "refund_tool": pending_reports,
            "orders": orders_needing_attention,
            "users": new_users,
            "transactions": pending_transactions,
            "revenue": revenue_activity,
            "sold_accounts": int(sold_units_24h),
        }
    except Exception:
        logger.exception("Sidebar badge counts failed")
        return {}
    finally:
        session.close()


def _mark_sidebar_seen(db: Session, field: str) -> None:
    """Stamp `now` on the given BotConfig watermark column so the matching
    sidebar badge (Revenue / Sold Accounts) clears the moment this page is
    opened, same as reading a notification."""
    config = db.query(BotConfig).first()
    if not config:
        config = BotConfig()
        db.add(config)
    setattr(config, field, datetime.utcnow())
    db.commit()


def render(request: Request, template_name: str, context: dict | None = None, status_code: int = 200):
    if "csrf_token" not in request.session:
        request.session["csrf_token"] = secrets.token_hex(32)
    csrf_token = request.session["csrf_token"]
    payload = {
        "request": request,
        "csrf_token": csrf_token,
        "sidebar_counts": _sidebar_badge_counts(),
        **(context or {}),
    }
    return templates.TemplateResponse(request, template_name, payload, status_code=status_code)


# Admin session stays alive while the browser is actively used; idle tabs expire.
ADMIN_IDLE_SECONDS = max(int(os.getenv("ADMIN_IDLE_SECONDS", "7200") or 7200), 60)


def _touch_admin_session(request: Request) -> None:
    request.session["admin_last_active"] = time.time()


def admin_required(request: Request) -> None:
    if not request.session.get("admin_logged_in"):
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})

    now = time.time()
    last = float(request.session.get("admin_last_active") or 0)
    # Missing stamp = older session before idle tracking; treat as just-active once.
    if last and (now - last) > ADMIN_IDLE_SECONDS:
        request.session.clear()
        raise HTTPException(
            status_code=303,
            headers={
                "Location": (
                    "/admin/login?error="
                    + quote("Session expired after 2 hours of inactivity. Please log in again.")
                )
            },
        )
    _touch_admin_session(request)


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


@router.get("")
def admin_root() -> RedirectResponse:
    return redirect("/admin/dashboard")


@router.get("/login")
def login_page(request: Request):
    error = request.query_params.get("error")
    return render(request, "login.html", {"error": error})


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    lockout_key = f"admin_login:{client_ip}"

    locked, retry_after = await is_locked_out(lockout_key)
    if locked:
        return render(
            request,
            "login.html",
            {"error": f"Too many failed attempts. Temporary lockout active. Try again in {retry_after} seconds."},
            status_code=429,
        )

    expected_user = (os.getenv("ADMIN_USERNAME") or "admin").strip()
    admin_hash = (os.getenv("ADMIN_PASSWORD_HASH") or "").strip()
    admin_pass = (os.getenv("ADMIN_PASSWORD") or ("admin123" if not is_production() else "")).strip()

    user_matches = constant_time_compare(username.strip(), expected_user)
    pass_matches = False
    if admin_hash:
        pass_matches, _ = verify_password(password, admin_hash)
    elif admin_pass:
        pass_matches = constant_time_compare(password, admin_pass)

    if user_matches and pass_matches:
        await clear_failures(lockout_key)
        request.session["admin_logged_in"] = True
        request.session["csrf_token"] = secrets.token_hex(32)
        _touch_admin_session(request)
        log_admin_action(
            db,
            action="admin.login",
            entity_type="admin_session",
            entity_label="admin session",
            request=request,
        )
        db.commit()
        return redirect("/admin/dashboard")

    locked, retry_after = await record_failure_and_check_lockout(
        lockout_key, max_failures=5, window_seconds=600, lockout_seconds=900
    )
    log_admin_action(
        db,
        action="admin.login.failed",
        entity_type="admin_session",
        entity_label="admin session",
        change={"username": username.strip()[:80]},
        request=request,
    )
    db.commit()

    if locked:
        error_msg = f"Invalid credentials. Account locked out for {retry_after} seconds due to repeated failed attempts."
        return render(request, "login.html", {"error": error_msg}, status_code=429)
    return render(request, "login.html", {"error": "Invalid admin credentials"}, status_code=401)


@router.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    if request.session.get("admin_logged_in"):
        log_admin_action(
            db,
            action="admin.logout",
            entity_type="admin_session",
            entity_label="admin session",
            request=request,
        )
        db.commit()
    request.session.clear()
    return redirect("/admin/login")


@router.get("/session/ping")
def session_ping(request: Request):
    """Lightweight keep-alive while the admin tab is actively used."""
    if not request.session.get("admin_logged_in"):
        return {"ok": False, "logged_in": False}
    now = time.time()
    last = float(request.session.get("admin_last_active") or 0)
    if last and (now - last) > ADMIN_IDLE_SECONDS:
        request.session.clear()
        return {"ok": False, "logged_in": False, "expired": True}
    _touch_admin_session(request)
    return {"ok": True, "logged_in": True, "idle_seconds": ADMIN_IDLE_SECONDS}


@router.get("/dashboard")
def dashboard(
    request: Request,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
):
    admin_required(request)
    qp = request.query_params
    date_from = date_from or qp.get("from")
    date_to = date_to or qp.get("to")
    period_stats = dashboard_period_stats(db, period or qp.get("period"), date_from, date_to)
    recent_orders = db.query(Order).order_by(Order.created_at.desc()).limit(8).all()
    recent_activity = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(12).all()
    return render(
        request,
        "dashboard.html",
        {
            "period_stats": period_stats,
            "recent_orders": recent_orders,
            "recent_activity": recent_activity,
            "entity_display": entity_display,
        },
    )


@router.get("/audit-log")
def audit_log_page(
    request: Request,
    action: str | None = None,
    entity: str | None = None,
    since: str | None = None,
    db: Session = Depends(get_db),
):
    admin_required(request)
    qp = request.query_params
    action = (action or qp.get("action") or "").strip()
    entity = (entity or qp.get("entity") or "").strip()
    since = (since or qp.get("since") or "").strip()

    query = db.query(AuditLog).order_by(AuditLog.created_at.desc())
    if action:
        query = query.filter(AuditLog.action.ilike(f"%{action}%"))
    if entity and entity.lower() != "any":
        query = query.filter(AuditLog.entity_type == entity)
    if since:
        try:
            since_dt = datetime.strptime(since, "%Y-%m-%d")
            query = query.filter(AuditLog.created_at >= since_dt)
        except ValueError:
            pass

    rows = query.limit(200).all()
    entity_types = [
        row[0]
        for row in db.query(AuditLog.entity_type)
        .filter(AuditLog.entity_type.isnot(None))
        .distinct()
        .order_by(AuditLog.entity_type.asc())
        .all()
    ]
    return render(
        request,
        "audit_log.html",
        {
            "rows": rows,
            "entity_types": entity_types,
            "filter_action": action,
            "filter_entity": entity or "any",
            "filter_since": since,
            "entity_display": entity_display,
        },
    )


@router.get("/providers")
def providers(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    qp = request.query_params
    q = (qp.get("q") or "").strip()
    status = (qp.get("status") or "all").strip().lower()

    query = db.query(Provider)
    if status == "enabled":
        query = query.filter(Provider.is_active.is_(True))
    elif status == "disabled":
        query = query.filter(Provider.is_active.is_(False))
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                Provider.name.ilike(like),
                Provider.api_url.ilike(like),
                Provider.telegram_bot.ilike(like),
                Provider.contact.ilike(like),
                Provider.api_username.ilike(like),
            )
        )
    all_providers = db.query(Provider).all()
    counts = {
        "all": len(all_providers),
        "enabled": sum(1 for p in all_providers if p.is_active),
        "disabled": sum(1 for p in all_providers if not p.is_active),
    }
    return render(
        request,
        "providers.html",
        {
            "providers": query.order_by(Provider.created_at.desc()).all(),
            "message": qp.get("message"),
            "error": qp.get("error"),
            "q": q,
            "status": status if status in {"all", "enabled", "disabled"} else "all",
            "counts": counts,
        },
    )


@router.get("/providers/new")
def providers_new(request: Request, kind: str = "api"):
    admin_required(request)
    kind = (kind or "api").strip().lower()
    if kind not in {"api", "manual"}:
        kind = "api"
    template = "provider_add_api.html" if kind == "api" else "provider_add_manual.html"
    return render(
        request,
        template,
        {
            "kind": kind,
            "error": request.query_params.get("error"),
            "message": request.query_params.get("message"),
        },
    )


@router.get("/providers/{provider_id}/edit")
def providers_edit_page(provider_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    provider = db.get(Provider, provider_id)
    if not provider:
        return redirect(f"/admin/providers?error={quote('Provider not found')}")
    return render(
        request,
        "provider_edit.html",
        {
            "provider": provider,
            "error": request.query_params.get("error"),
            "message": request.query_params.get("message"),
        },
    )


@router.post("/providers")
def create_provider(
    request: Request,
    name: str = Form(...),
    type: str = Form("manual"),
    api_url: str = Form(""),
    balance_url: str = Form(""),
    api_key: str = Form(""),
    telegram_bot: str = Form(""),
    contact: str = Form(""),
    db: Session = Depends(get_db),
):
    admin_required(request)
    provider_type = "api" if (type or "").strip().lower() == "api" else "manual"
    if provider_type == "manual" and not (telegram_bot or "").strip() and not (contact or "").strip():
        return redirect(
            f"/admin/providers/new?kind=manual&error={quote('At least one contact method (Telegram bot or Admin contact) is required.')}"
        )
    provider = Provider(
        name=name.strip(),
        type=provider_type,
        api_url=(api_url or "").strip() or None,
        balance_url=(balance_url or "").strip() or None,
        api_key=(api_key or "").strip() or None,
        telegram_bot=(telegram_bot or "").strip() or None,
        contact=(contact or "").strip() or None,
    )
    db.add(provider)
    db.flush()
    log_admin_action(
        db,
        action="provider.created",
        entity_type="provider",
        entity_id=str(provider.id),
        entity_label=f"provider #{provider.id} · {provider.name}",
        change={"type": provider.type, "name": provider.name},
        request=request,
    )
    db.commit()
    return redirect(f"/admin/providers?message={quote('Provider added successfully')}")


@router.post("/providers/{provider_id}/edit")
def edit_provider(
    provider_id: int,
    request: Request,
    name: str = Form(...),
    type: str = Form("manual"),
    api_url: str = Form(""),
    balance_url: str = Form(""),
    api_key: str = Form(""),
    telegram_bot: str = Form(""),
    contact: str = Form(""),
    db: Session = Depends(get_db),
):
    admin_required(request)
    provider = db.get(Provider, provider_id)
    if not provider:
        return redirect(f"/admin/providers?error={quote('Provider not found')}")
    before = {"name": provider.name, "type": provider.type}
    provider.name = name
    provider.type = type
    provider.api_url = api_url.strip() or None
    provider.balance_url = balance_url.strip() or None
    provider.api_key = api_key.strip() or None
    provider.telegram_bot = telegram_bot.strip() or None
    provider.contact = contact.strip() or None
    db.add(provider)
    log_admin_action(
        db,
        action="provider.updated",
        entity_type="provider",
        entity_id=str(provider.id),
        entity_label=f"provider #{provider.id} · {provider.name}",
        change={"before": before, "after": {"name": provider.name, "type": provider.type}},
        request=request,
    )
    db.commit()
    return redirect(f"/admin/providers?message={quote('Provider updated successfully')}")


@router.post("/providers/{provider_id}/toggle")
def toggle_provider(provider_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    provider = db.get(Provider, provider_id)
    if provider:
        provider.is_active = not provider.is_active
        log_admin_action(
            db,
            action="provider.enabled" if provider.is_active else "provider.disabled",
            entity_type="provider",
            entity_id=str(provider.id),
            entity_label=f"provider #{provider.id} · {provider.name}",
            change={"is_active": provider.is_active},
            request=request,
        )
        db.commit()
    return redirect("/admin/providers")


@router.post("/providers/{provider_id}/remove")
def remove_provider(provider_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    provider = db.get(Provider, provider_id)
    if not provider:
        return redirect(f"/admin/providers?error={quote('Provider not found')}")

    linked_count = db.query(Service).filter(Service.provider_id == provider_id, Service.is_deleted.is_(False)).count()
    if linked_count > 0:
        return redirect(
            f"/admin/providers?error={quote(f'Cannot delete: {linked_count} product(s) linked to this provider. Move or delete them first.')}"
        )

    label = f"provider #{provider.id} · {provider.name}"
    log_admin_action(
        db,
        action="provider.deleted",
        entity_type="provider",
        entity_id=str(provider.id),
        entity_label=label,
        request=request,
    )
    db.delete(provider)
    db.commit()
    return redirect(f"/admin/providers?message={quote('Provider removed successfully')}")


@router.post("/providers/{provider_id}/sync")
async def sync_provider(provider_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    provider = db.get(Provider, provider_id)
    if not provider:
        return redirect("/admin/providers?error=Provider not found")
    try:
        created, updated, balance, balance_err = await sync_provider_products(db, provider)
    except Exception as exc:
        return redirect(f"/admin/providers?error={quote('Sync failed: ' + str(exc)[:160])}")
    log_admin_action(
        db,
        action="provider.synced",
        entity_type="provider",
        entity_id=str(provider.id),
        entity_label=f"provider #{provider.id} · {provider.name}",
        change={"updated": updated, "created": created, "balance": balance, "balance_err": balance_err},
        request=request,
    )
    db.commit()
    if balance is not None:
        balance_note = f" Balance: ${balance:.2f} USDT."
    elif balance_err:
        balance_note = f" Balance: not returned ({balance_err[:220]})."
    else:
        balance_note = " Balance: not returned by API."
    return redirect(
        f"/admin/providers?message={quote(f'Synced price/stock for {updated} imported product(s).{balance_note} New products are not auto-added — use Import Products.')}"
    )


@router.get("/import-services")
async def import_services(request: Request, provider_id: int | None = None, db: Session = Depends(get_db)):
    admin_required(request)
    providers = db.query(Provider).filter(Provider.type == "api", Provider.is_active.is_(True)).all()
    categories = db.query(Category).order_by(Category.sort_order.asc()).all()
    imported = []
    error = request.query_params.get("error")
    if provider_id:
        provider = db.get(Provider, provider_id)
        try:
            imported = await fetch_services(provider)
        except Exception as exc:
            error = str(exc)
    return render(
        request,
        "import_services.html",
        {
            "providers": providers,
            "categories": categories,
            "imported": imported[:100],
            "provider_id": provider_id,
            "error": error,
            "message": request.query_params.get("message"),
        },
    )


def _resolve_import_category(db: Session, category_id: int | None) -> Category | None:
    if not category_id:
        return None
    return db.get(Category, category_id)


def apply_imported_service(
    db: Session,
    *,
    provider_id: int,
    provider_service_id: str,
    name: str,
    description: str,
    cost_price: float,
    commission_pct: float,
    markup_fixed_usdt: float,
    category: Category,
    min_qty: int,
    max_qty: int,
    initial_stock: int,
) -> tuple[Service, bool]:
    """Create or update an imported API product. Returns (service, should_notify)."""
    fixed = max(float(markup_fixed_usdt or 0), 0.0)
    sell_price = api_computed_sell_price(cost_price, commission_pct, fixed)
    service = (
        db.query(Service)
        .filter(
            Service.provider_id == provider_id,
            Service.provider_service_id == str(provider_service_id),
        )
        .first()
    )
    if not service:
        sku = make_service_sku(category.name, provider_service_id)
        service = db.query(Service).filter(Service.sku == sku).first()
    should_notify = False
    if service:
        was_deleted = bool(service.is_deleted)
        # Revive if this was previously soft-deleted (hidden) so the SKU
        # doesn't stay permanently blocked from being re-imported.
        service.is_deleted = False
        service.provider_id = provider_id
        service.category_id = category.id
        service.provider_service_id = str(provider_service_id)
        service.name = name
        service.description = description or None
        service.cost_price = cost_price
        service.commission_pct = commission_pct
        service.markup_fixed_usdt = fixed
        # Re-import resets to auto pricing from this form (unless admin locked sell).
        if not getattr(service, "manual_sell_price", False):
            service.sell_price = sell_price
        service.min_qty = min_qty
        service.max_qty = max_qty
        service.is_active = initial_stock > 0
        stock = service.stock or Stock(service_id=service.id)
        stock.quantity = initial_stock
        stock.reserved_qty = min(stock.reserved_qty or 0, initial_stock)
        db.add(stock)
        should_notify = was_deleted
    else:
        sku = make_service_sku(category.name, provider_service_id)
        service = Service(
            provider_id=provider_id,
            category_id=category.id,
            provider_service_id=str(provider_service_id),
            sku=sku,
            name=name,
            description=description or None,
            cost_price=cost_price,
            sell_price=sell_price,
            commission_pct=commission_pct,
            markup_fixed_usdt=fixed,
            manual_sell_price=False,
            min_qty=min_qty,
            max_qty=max_qty,
            is_active=initial_stock > 0,
        )
        db.add(service)
        db.flush()
        db.add(Stock(service_id=service.id, quantity=initial_stock, reserved_qty=0))
        should_notify = True
    return service, should_notify


def _import_redirect(provider_id: int, *, message: str | None = None, error: str | None = None) -> RedirectResponse:
    path = f"/admin/import-services?provider_id={provider_id}"
    if message:
        path += f"&message={quote(message)}"
    if error:
        path += f"&error={quote(error)}"
    return redirect(path)


@router.post("/import-services")
async def save_imported_service(
    request: Request,
    provider_id: int = Form(...),
    provider_service_id: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    cost_price: float = Form(0.0),
    commission_pct: float = Form(25.0),
    markup_fixed_usdt: float = Form(0.0),
    category_id: int | None = Form(None),
    min_qty: int = Form(1),
    max_qty: int = Form(10000),
    initial_stock: int = Form(0),
    db: Session = Depends(get_db),
):
    admin_required(request)

    category = _resolve_import_category(db, category_id)
    if not category:
        return _import_redirect(provider_id, error="Select a product category from your panel before importing.")

    service, should_notify = apply_imported_service(
        db,
        provider_id=provider_id,
        provider_service_id=provider_service_id,
        name=name,
        description=description,
        cost_price=cost_price,
        commission_pct=commission_pct,
        markup_fixed_usdt=markup_fixed_usdt,
        category=category,
        min_qty=min_qty,
        max_qty=max_qty,
        initial_stock=initial_stock,
    )
    db.commit()
    if should_notify:
        await notify_new_product(service)
    return _import_redirect(provider_id, message=f"Imported “{service.name}” into {category.name}.")


@router.post("/import-services/bulk")
async def bulk_import_services(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    form = await request.form()
    try:
        provider_id = int(form.get("provider_id") or 0)
    except (TypeError, ValueError):
        provider_id = 0
    if not provider_id:
        return redirect("/admin/import-services?error=" + quote("Provider is required."))

    selected = form.getlist("selected")
    if not selected:
        return _import_redirect(provider_id, error="Select at least one product to import.")

    results: list[tuple[Service, bool, str]] = []
    errors: list[str] = []
    for raw_idx in selected:
        idx = str(raw_idx)
        category_raw = form.get(f"category_id_{idx}")
        try:
            category_id = int(category_raw) if category_raw not in (None, "") else None
        except (TypeError, ValueError):
            category_id = None
        category = _resolve_import_category(db, category_id)
        if not category:
            errors.append(f"Row {idx}: category required")
            continue
        provider_service_id = (form.get(f"provider_service_id_{idx}") or "").strip()
        name = (form.get(f"name_{idx}") or "").strip()
        if not provider_service_id or not name:
            errors.append(f"Row {idx}: missing product data")
            continue
        try:
            cost_price = float(form.get(f"cost_price_{idx}") or 0)
            commission_pct = float(form.get(f"commission_pct_{idx}") or 25)
            markup_fixed_usdt = float(form.get(f"markup_fixed_usdt_{idx}") or 0)
            min_qty = int(form.get(f"min_qty_{idx}") or 1)
            max_qty = int(form.get(f"max_qty_{idx}") or 10000)
            initial_stock = int(form.get(f"initial_stock_{idx}") or 0)
        except (TypeError, ValueError):
            errors.append(f"Row {idx}: invalid numbers")
            continue
        description = form.get(f"description_{idx}") or name
        service, should_notify = apply_imported_service(
            db,
            provider_id=provider_id,
            provider_service_id=provider_service_id,
            name=name,
            description=description,
            cost_price=cost_price,
            commission_pct=commission_pct,
            markup_fixed_usdt=markup_fixed_usdt,
            category=category,
            min_qty=min_qty,
            max_qty=max_qty,
            initial_stock=initial_stock,
        )
        results.append((service, should_notify, category.name))

    if not results and errors:
        return _import_redirect(provider_id, error="; ".join(errors[:3]))

    db.commit()
    for service, should_notify, _cat_name in results:
        if should_notify:
            await notify_new_product(service)

    msg = f"Imported {len(results)} product(s)."
    if errors:
        msg += f" Skipped {len(errors)}."
    return _import_redirect(provider_id, message=msg)


def make_service_sku(category_name: str, provider_service_id: str) -> str:
    plain_cat = admin_plain_text(category_name)
    base = f"{plain_cat}_{provider_service_id}".lower()
    return re.sub(r"[^a-z0-9]+", "_", base).strip("_")[:120]


def make_manual_service_sku(name: str, db: Session) -> str:
    """Auto-generate a unique internal SKU from the product name (admin does not enter SKU)."""
    plain = admin_plain_text(name)
    base = re.sub(r"[^a-z0-9]+", "_", (plain or "product").lower()).strip("_")[:100] or "product"
    sku = base
    suffix = 1
    while db.query(Service).filter(Service.sku == sku).first():
        sku = f"{base}_{suffix}"[:120]
        suffix += 1
    return sku


def apply_provider_catalog_item_to_service(db: Session, service: Service, item: dict) -> None:
    """Apply one provider catalog row onto an existing Service (cost/stock/sell only).

    Never changes SKU, name, description, category, orders, or soft-delete.
    """
    cost_price = float(item.get("cost") or item.get("rate") or 0)
    available = int(item.get("available") or 0)
    commission_pct = service.commission_pct if service.commission_pct is not None else 25.0
    fixed = float(getattr(service, "markup_fixed_usdt", 0) or 0)

    service.cost_price = cost_price
    # Keep flash/promo sale price intact while a ProductSale is active.
    active_sale = (
        db.query(ProductSale)
        .filter(ProductSale.service_id == service.id, ProductSale.is_active.is_(True))
        .first()
    )
    computed_sell = api_computed_sell_price(cost_price, commission_pct, fixed)
    if active_sale:
        active_sale.original_price = computed_sell
    else:
        service.sell_price = computed_sell

    stock = service.stock or Stock(service_id=service.id)
    apply_provider_stock(stock, available, service.fulfillment_type)
    db.add(stock)


async def sync_linked_service(db: Session, service: Service) -> tuple[bool, str]:
    """Fetch remote catalog and update ONLY this linked product by provider_service_id.

    Does not create catalog rows and does not change SKU / name / order history.
    """
    if not service.provider_id or not service.provider_service_id:
        return False, "Product is not linked to a provider product ID"
    provider = db.get(Provider, service.provider_id)
    if not provider:
        return False, "Provider not found"
    try:
        products = await fetch_services(provider)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    target = str(service.provider_service_id)
    match = None
    for item in products:
        rid = item.get("service") or item.get("id")
        if rid is not None and str(rid) == target:
            match = item
            break
    if not match:
        return False, f"Provider product ID '{target}' not found on {provider.name}"
    apply_provider_catalog_item_to_service(db, service, match)
    db.commit()
    cost = match.get("cost") or match.get("rate") or 0
    avail = match.get("available") or 0
    return True, f"cost={cost}, stock={avail}"


async def sync_provider_products(db: Session, provider: Provider) -> tuple[int, int, float | None, str | None]:
    """Refresh cost + sell price + stock for products the admin already imported.

    Does NOT create new catalog rows — new provider products only appear after
    an explicit Import click on Import Products.

    Does NOT overwrite localized catalog copy: name, description, min/max qty,
    images, warranty, soft-delete flag, etc. stay as the admin set them.

    Sell price always follows the original import-style formula:
    cost + saved markup % + fixed USDT (same as before; edit sell updates that markup).

    Upstream wallet balance is refreshed AFTER product sync succeeds, and is
    best-effort: balance/API failures never fail or roll back price/stock sync.
    """
    products = await fetch_services(provider)
    created = 0
    updated = 0
    for item in products:
        provider_service_id_value = item.get("service") or item.get("id")
        if not provider_service_id_value:
            continue
        provider_service_id = str(provider_service_id_value)

        service = (
            db.query(Service)
            .filter(
                Service.provider_id == provider.id,
                Service.provider_service_id == provider_service_id,
            )
            .first()
        )
        if not service:
            # Older imports may only match on SKU
            category_name = item.get("category") or "Imported"
            sku = make_service_sku(category_name, provider_service_id)
            service = (
                db.query(Service)
                .filter(Service.provider_id == provider.id, Service.sku == sku)
                .first()
            )
        if not service:
            continue

        apply_provider_catalog_item_to_service(db, service, item)
        updated += 1

    # Commit product/price/stock first — identical to pre-balance behavior.
    db.commit()

    # Best-effort wallet refresh: never undo or fail the product sync above.
    balance: float | None = getattr(provider, "api_balance", None)
    balance_err: str | None = None
    try:
        from utils.provider_balance import sync_provider_balance

        balance, _username, balance_err = await sync_provider_balance(db, provider)
        db.commit()
    except Exception:  # noqa: BLE001
        logger = __import__("logging").getLogger(__name__)
        logger.exception("[PROVIDER-BALANCE] ignored after successful product sync for %s", provider.name)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        balance = getattr(provider, "api_balance", None)
        balance_err = "balance sync crashed (see logs)"

    return created, updated, balance, balance_err


@router.get("/services")
def services(request: Request, q: str = "", db: Session = Depends(get_db)):
    admin_required(request)
    query = db.query(Service).filter(Service.is_deleted.is_(False))
    needle = (q or "").strip()
    if needle:
        like = f"%{needle}%"
        query = (
            query.outerjoin(Category, Service.category_id == Category.id)
            .outerjoin(Provider, Service.provider_id == Provider.id)
            .filter(
                or_(
                    Service.name.ilike(like),
                    Service.sku.ilike(like),
                    Service.provider_service_id.ilike(like),
                    Category.name.ilike(like),
                    Provider.name.ilike(like),
                )
            )
            .distinct()
        )
    return render(
        request,
        "services.html",
        {
            "services": query.order_by(Service.sort_order.asc(), Service.name.asc()).all(),
            "providers": db.query(Provider).all(),
            "categories": db.query(Category).order_by(Category.sort_order).all(),
            "icon_presets": db.query(IconPreset).order_by(IconPreset.sort_order.asc(), IconPreset.name.asc()).all(),
            "search_q": needle,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/services/new")
def services_new(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    return render(
        request,
        "service_add.html",
        {
            "providers": db.query(Provider).all(),
            "categories": db.query(Category).order_by(Category.sort_order).all(),
            "icon_presets": db.query(IconPreset).order_by(IconPreset.sort_order.asc(), IconPreset.name.asc()).all(),
            "description_templates": db.query(DescriptionTemplate).order_by(DescriptionTemplate.sort_order.asc(), DescriptionTemplate.name.asc()).all(),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/services/{service_id}/edit")
def services_edit_page(service_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    service = db.get(Service, service_id)
    if not service or service.is_deleted:
        return redirect(f"/admin/services?error={quote('Product not found')}")
    return render(
        request,
        "service_edit.html",
        {
            "service": service,
            "providers": db.query(Provider).all(),
            "categories": db.query(Category).order_by(Category.sort_order).all(),
            "icon_presets": db.query(IconPreset).order_by(IconPreset.sort_order.asc(), IconPreset.name.asc()).all(),
            "description_templates": db.query(DescriptionTemplate).order_by(DescriptionTemplate.sort_order.asc(), DescriptionTemplate.name.asc()).all(),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/description-templates")
async def save_description_template(
    request: Request,
    name: str = Form(...),
    body: str = Form(""),
    db: Session = Depends(get_db),
):
    """Save current product description as a reusable template."""
    admin_required(request)
    from utils.helpers import strip_leading_description_label

    clean_name = (name or "").strip()
    clean_body = strip_leading_description_label(body)
    wants_json = (request.headers.get("x-requested-with") or "").lower() == "fetch"
    if not clean_name:
        if wants_json:
            return JSONResponse({"ok": False, "error": "Template name is required"}, status_code=400)
        return redirect(f"/admin/services/new?error={quote('Template name is required')}")
    if not clean_body:
        if wants_json:
            return JSONResponse({"ok": False, "error": "Template body is empty"}, status_code=400)
        return redirect(f"/admin/services/new?error={quote('Template body is empty')}")

    existing = db.query(DescriptionTemplate).filter(DescriptionTemplate.name == clean_name).first()
    if existing:
        existing.body = clean_body
        row = existing
    else:
        row = DescriptionTemplate(name=clean_name, body=clean_body, sort_order=0)
        db.add(row)
    db.commit()
    db.refresh(row)
    if wants_json:
        return {"ok": True, "id": row.id, "name": row.name, "body": row.body}
    return redirect(f"/admin/services/new?message={quote(f'Template saved: {clean_name}')}")


@router.post("/description-templates/{template_id}/delete")
def delete_description_template(template_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    row = db.get(DescriptionTemplate, template_id)
    if row:
        db.delete(row)
        db.commit()
    return redirect(f"/admin/services/new?message={quote('Template deleted')}")


@router.get("/custom-emoji/{emoji_id}")
async def custom_emoji_asset(emoji_id: str, request: Request):
    """Serve a cached Telegram custom-emoji image for admin dropdown previews."""
    admin_required(request)
    eid = (emoji_id or "").strip()
    if not eid.isdigit():
        raise HTTPException(status_code=404, detail="Not found")
    from utils.custom_emoji_assets import ensure_custom_emoji_cached

    path = await ensure_custom_emoji_cached(eid)
    if not path:
        raise HTTPException(status_code=404, detail="Emoji not available")
    media = {
        ".webp": "image/webp",
        ".png": "image/png",
        ".gif": "image/gif",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webm": "video/webm",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media, filename=path.name)


@router.post("/services")
async def create_service(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    warranty: str = Form(""),
    emoji: str = Form(""),
    icon_image: UploadFile | None = File(None),
    provider_id: int | None = Form(None),
    category_id: int | None = Form(None),
    cost_price: float = Form(0.0),
    sell_price: float = Form(0.0),
    commission_pct: float = Form(0.0),
    min_qty: int = Form(1),
    max_qty: int = Form(10000),
    initial_stock: int = Form(0),
    sort_order: int = Form(0),
    fulfillment_type: str = Form("auto"),
    require_email: str = Form(""),
    db: Session = Depends(get_db),
):
    admin_required(request)
    fulfillment_type = fulfillment_type if fulfillment_type in {"manual", "stock"} else "auto"
    need_email = require_email.lower() == "true"
    image_path = await save_icon_image(icon_image, SERVICE_UPLOAD_DIR, "svc")
    sku = make_manual_service_sku(name, db)
    warranty_value = warranty.strip() or None

    existing = db.query(Service).filter(Service.sku == sku).first()
    if existing and not existing.is_deleted:
        error_msg = f"A product with a similar name already exists (ID #{existing.id}). Edit that product or use a different name."
        return redirect(f"/admin/services/new?error={quote(error_msg)}")

    if existing and existing.is_deleted:
        # This product belonged to a previously hidden (soft-deleted) row.
        # Revive it with the new details instead of crashing on a duplicate.
        from utils.helpers import extract_icon_from_rich_text, strip_leading_description_label

        clean_description = strip_leading_description_label(description) or None
        icon_from_name = extract_icon_from_rich_text(name, "🛒")
        existing.is_deleted = False
        existing.name = name
        existing.description = clean_description
        existing.warranty = warranty_value
        existing.emoji = (emoji.strip() or None) or (icon_from_name if icon_from_name != "📦" else None)
        if image_path:
            existing.image_path = image_path
        existing.provider_id = provider_id or None
        existing.category_id = category_id or None
        existing.cost_price = cost_price
        existing.sell_price = sell_price
        existing.commission_pct = commission_pct
        existing.min_qty = min_qty
        existing.max_qty = max_qty
        existing.sort_order = sort_order
        existing.fulfillment_type = fulfillment_type
        existing.require_email = need_email
        existing.is_active = initial_stock > 0
        stock = existing.stock or Stock(service_id=existing.id)
        stock.quantity = initial_stock
        stock.reserved_qty = min(stock.reserved_qty or 0, initial_stock)
        existing.stock = stock
        db.add(stock)
        db.commit()
        await notify_new_product(existing)
        return redirect(f"/admin/services?message={quote('Product restored and updated')}")

    from utils.helpers import extract_icon_from_rich_text, strip_leading_description_label

    clean_description = strip_leading_description_label(description) or None
    icon_from_name = extract_icon_from_rich_text(name, "🛒")
    service = Service(
        sku=sku,
        name=name,
        description=clean_description,
        warranty=warranty_value,
        emoji=(emoji.strip() or None) or (icon_from_name if icon_from_name != "📦" else None),
        image_path=image_path,
        provider_id=provider_id or None,
        category_id=category_id or None,
        cost_price=cost_price,
        sell_price=sell_price,
        commission_pct=commission_pct,
        min_qty=min_qty,
        max_qty=max_qty,
        sort_order=sort_order,
        fulfillment_type=fulfillment_type,
        require_email=need_email,
        is_active=initial_stock > 0,
    )
    db.add(service)
    db.flush()
    db.add(Stock(service_id=service.id, quantity=initial_stock, reserved_qty=0))
    db.commit()
    await notify_new_product(service)
    return redirect("/admin/services")


@router.post("/services/{service_id}/edit")
async def edit_service(
    service_id: int,
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    warranty: str = Form(""),
    emoji: str | None = Form(None),
    icon_image: UploadFile | None = File(None),
    remove_image: str = Form(""),
    category_id: int | None = Form(None),
    provider_id: int | None = Form(None),
    provider_service_id: str = Form(""),
    sync_from_provider: str = Form(""),
    cost_price: float = Form(0.0),
    sell_price: float = Form(0.0),
    markup_fixed_usdt: float = Form(0.0),
    commission_pct: float = Form(0.0),
    sort_order: int = Form(0),
    fulfillment_type: str = Form("auto"),
    require_email: str = Form(""),
    db: Session = Depends(get_db),
):
    admin_required(request)
    service = db.get(Service, service_id)
    if not service:
        return redirect(f"/admin/services/{service_id}/edit?error={quote('Product not found')}")

    new_provider_id = provider_id or None
    new_psid = (provider_service_id or "").strip() or None
    if new_provider_id and not new_psid:
        return redirect(
            f"/admin/services/{service_id}/edit?error={quote('Provider product ID is required when a provider is selected (Import Products → ID column)')}"
        )
    if not new_provider_id:
        new_psid = None
    if new_provider_id and new_psid:
        conflict = (
            db.query(Service)
            .filter(
                Service.provider_id == new_provider_id,
                Service.provider_service_id == new_psid,
                Service.id != service.id,
            )
            .first()
        )
        if conflict:
            return redirect(
                f"/admin/services/{service_id}/edit?error={quote(f'Provider product ID already linked to another product: {conflict.name} (#{conflict.id})')}"
            )

    # This is the fix for products getting permanently stuck in one category:
    # previously there was no way to change category_id on an existing
    # product, only when it was first created/imported. Now the admin can
    # move it to any category (or set it to "No category") at any time.
    # SKU is intentionally never changed here — keeps order/history stable.
    from utils.helpers import extract_icon_from_rich_text, strip_leading_description_label

    service.name = name
    service.description = strip_leading_description_label(description) or None
    service.warranty = warranty.strip() or None
    if emoji is not None:
        service.emoji = emoji.strip() or None
    else:
        derived = extract_icon_from_rich_text(name, service.emoji or "🛒")
        if derived:
            service.emoji = derived
    if remove_image.lower() == "true":
        service.image_path = None
    new_image_path = await save_icon_image(icon_image, SERVICE_UPLOAD_DIR, "svc")
    if new_image_path:
        service.image_path = new_image_path
    service.category_id = category_id or None
    service.provider_id = new_provider_id
    service.provider_service_id = new_psid
    service.sort_order = sort_order
    # min_qty / max_qty stay as already stored (defaults 1 / 10000) — not edited on this form.
    service.fulfillment_type = fulfillment_type if fulfillment_type in {"manual", "stock"} else "auto"
    service.require_email = require_email.lower() == "true"

    # Same sync model as before (cost + markup), but editing sell price updates
    # the saved markup so the new price sticks on the next sync.
    pct = max(float(commission_pct or 0), 0.0)
    fixed_form = max(float(markup_fixed_usdt or 0), 0.0)
    cost = max(float(cost_price or 0), 0.0)
    sell = float(sell_price or 0)
    service.cost_price = cost
    service.manual_sell_price = False

    if service.provider_id:
        formula = api_computed_sell_price(cost, pct, fixed_form)
        if abs(sell - formula) > 0.009:
            # Admin changed sell → save as new lasting markup (old sync keeps working).
            pct, fixed_form, sell = derive_api_markup_from_sell(cost, sell, pct)
        else:
            sell = formula
        service.commission_pct = pct
        service.markup_fixed_usdt = fixed_form
        service.sell_price = sell
    else:
        service.commission_pct = pct
        service.markup_fixed_usdt = fixed_form
        service.sell_price = sell

    db.commit()

    msg = f"Updated {service.name}"
    do_sync = sync_from_provider.lower() == "true"
    if do_sync and service.provider_id and service.provider_service_id:
        ok, detail = await sync_linked_service(db, service)
        if ok:
            msg += f" — synced from provider ({detail})"
        else:
            return redirect(
                f"/admin/services/{service_id}/edit?message={quote(msg)}&error={quote('Link saved, but sync failed: ' + detail)}"
            )
    return redirect(f"/admin/services?message={quote(msg)}")


@router.post("/services/{service_id}/toggle")
def toggle_service(service_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    service = db.get(Service, service_id)
    if service:
        service.is_active = not service.is_active
        db.commit()
    return redirect("/admin/services")


@router.post("/services/{service_id}/delete")
def delete_service(service_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    service = db.get(Service, service_id)
    if not service:
        return redirect("/admin/services")

    has_orders = db.query(Order).filter(Order.service_id == service_id).first() is not None
    if has_orders:
        # Keep the row so existing order history stays valid; just hide it from the list.
        service.is_deleted = True
        service.is_active = False
        db.commit()
        return redirect(f"/admin/services?message={quote('Product hidden (kept for existing order history)')}")

    # No orders reference this product yet, safe to remove completely.
    if service.stock:
        db.delete(service.stock)
    db.delete(service)
    db.commit()
    return redirect(f"/admin/services?message={quote('Product permanently deleted')}")


def _merge_product_choices(db: Session):
    from utils.product_display import service_sold_units

    products = (
        db.query(Service)
        .filter(Service.is_deleted.is_(False))
        .order_by(Service.sort_order.asc(), Service.name.asc())
        .all()
    )
    rows = []
    for product in products:
        sold = service_sold_units(db, product.id)
        provider_name = product.provider.name if product.provider else "Manual"
        rows.append(
            {
                "id": product.id,
                "label": f"#{product.id} · {admin_plain_text(product.name)} · {provider_name} · sold {sold}",
                "name": admin_plain_text(product.name),
                "provider": provider_name,
                "sold": sold,
                "active": product.is_active,
            }
        )
    return rows


@router.get("/merge-products")
def merge_products_page(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    return render(
        request,
        "merge_products.html",
        {
            "products": _merge_product_choices(db),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/merge-products")
def merge_products_action(
    request: Request,
    keep_id: int = Form(...),
    merge_id: int = Form(...),
    confirm: str = Form(""),
    db: Session = Depends(get_db),
):
    admin_required(request)
    if confirm.lower() != "merge":
        return redirect(
            f"/admin/merge-products?error={quote('Type MERGE in the confirm box to proceed')}"
        )
    from utils.product_merge import merge_products

    try:
        result = merge_products(db, keep_id=keep_id, merge_id=merge_id)
        log_admin_action(
            db,
            action="admin.product.merge",
            entity_type="service",
            entity_id=str(result.keep_id),
            entity_label=f"merged #{result.merge_id} → #{result.keep_id}",
            change={
                "keep_id": result.keep_id,
                "merge_id": result.merge_id,
                "orders_moved": result.orders_moved,
                "discounts_moved": result.discounts_moved,
                "discounts_skipped": result.discounts_skipped,
                "sales_moved": result.sales_moved,
                "sales_deactivated": result.sales_deactivated,
                "stock_lines_merged": result.stock_lines_merged,
                "qty_added": result.qty_added,
            },
            request=request,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return redirect(f"/admin/merge-products?error={quote(str(exc))}")
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return redirect(f"/admin/merge-products?error={quote(f'Merge failed: {exc}')}")

    msg = (
        f"Merged #{result.merge_id} into #{result.keep_id}: "
        f"{result.orders_moved} order(s), "
        f"{result.stock_lines_merged} stock line(s), "
        f"{result.discounts_moved} discount(s). Duplicate hidden."
    )
    return redirect(f"/admin/merge-products?message={quote(msg)}")


@router.get("/stock")
def stock_management(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    stocks = (
        db.query(Stock)
        .join(Service)
        .filter(Service.is_deleted.is_(False))
        .order_by(Service.sort_order.asc(), Service.name.asc())
        .all()
    )
    for stock in stocks:
        stock.login_line_count = len(
            [line for line in (stock.login_details or "").splitlines() if line.strip()]
        )
    return render(
        request,
        "stock_management.html",
        {
            "stocks": stocks,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


def _stock_services(db: Session):
    return (
        db.query(Service)
        .filter(Service.is_deleted.is_(False))
        .order_by(Service.sort_order.asc(), Service.name.asc())
        .all()
    )


@router.get("/stock/new")
def stock_add_page(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    return render(
        request,
        "stock_add.html",
        {
            "services": _stock_services(db),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/stock/notify")
def stock_notify_page(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    return render(
        request,
        "stock_notify.html",
        {
            "services": _stock_services(db),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/stock/{service_id}/edit")
def stock_edit_page(service_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    service = db.get(Service, service_id)
    if not service or service.is_deleted:
        return redirect(f"/admin/stock?error={quote('Product not found')}")
    stock = service.stock
    if not stock:
        return redirect(f"/admin/stock?error={quote('Stock row not found')}")
    return render(
        request,
        "stock_edit.html",
        {
            "service": service,
            "stock": stock,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/stock/add")
async def add_stock_route(
    request: Request,
    service_id: int = Form(...),
    stock_type: str = Form("account"),
    quantity: int = Form(0),
    is_unlimited: str = Form("false"),
    notes: str = Form(""),
    login_details: str = Form(""),
    bulk_file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    admin_required(request)
    from utils.stock_file_import import extract_stock_lines_from_upload, merge_login_lines

    stock_type = "quantity" if stock_type == "quantity" else "account"
    unlimited = stock_type == "quantity" and is_unlimited.lower() in {"true", "1", "on", "yes"}

    if stock_type == "account":
        try:
            file_lines = await extract_stock_lines_from_upload(bulk_file)
        except ValueError as exc:
            return redirect(f"/admin/stock/new?error={quote(str(exc))}")
        lines = merge_login_lines(login_details, file_lines)
        add_quantity = len(lines)
        if add_quantity < 1:
            return redirect(f"/admin/stock/new?error={quote('Add at least one account line or choose Quantity Stock.')}" )
        details = "\n".join(lines)
    else:
        add_quantity = 0 if unlimited else max(int(quantity or 0), 0)
        if not unlimited and add_quantity < 1:
            return redirect(f"/admin/stock/new?error={quote('Quantity must be at least 1, or enable Unlimited Stock.')}" )
        details = None

    try:
        stock = add_stock(
            db, service_id, add_quantity, notes or None, details,
            stock_type=stock_type, is_unlimited=unlimited,
        )
    except ValueError as exc:
        return redirect(f"/admin/stock/new?error={quote(str(exc))}")

    if unlimited:
        return redirect(f"/admin/stock?message={quote('Unlimited quantity stock enabled.')}" )
    await notify_stock_added(stock.service, add_quantity)
    label = "account line(s)" if stock_type == "account" else "unit(s)"
    return redirect(f"/admin/stock?message={quote(f'Stock added ({add_quantity} {label}). Notification sent.')}" )


@router.post("/stock/update")
async def update_stock_route(
    request: Request,
    service_id: int = Form(...),
    quantity: int = Form(...),
    db: Session = Depends(get_db),
):
    """Set Stock = fake notification only.

    Does NOT change inventory quantity. Used when API stock cannot push bot
    broadcasts — admin enters a display "Added" number and clients get the
    usual Stock updated! message while DB stock stays unchanged.
    """
    admin_required(request)
    service = db.get(Service, service_id)
    if not service or service.is_deleted:
        return redirect(f"/admin/stock/notify?error={quote('Product not found')}")
    if quantity < 1:
        return redirect(f"/admin/stock/notify?error={quote('Notification quantity must be at least 1')}")
    # Refresh relationship so notify_stock_added can read current available qty.
    stock = db.query(Stock).filter(Stock.service_id == service_id).first()
    if stock:
        db.refresh(stock)
    sent = await notify_stock_added(service, quantity, fake_notify=True)
    return redirect(
        f"/admin/stock?message={quote(f'Stock notification sent to {sent} users (inventory unchanged)')}"
    )


@router.post("/stock/{service_id}/edit")
def edit_stock_route(
    service_id: int,
    request: Request,
    quantity: int = Form(...),
    reserved_qty: int = Form(0),
    low_stock_threshold: int = Form(10),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    """Update qty / reserved / threshold / notes. Logins are edited via View stock."""
    admin_required(request)
    service = db.get(Service, service_id)
    if not service or service.is_deleted:
        return redirect(f"/admin/stock?error={quote('Product not found')}")
    stock = service.stock
    try:
        set_stock(
            db,
            service_id,
            quantity,
            reserved_qty,
            notes,
            stock.login_details if stock else None,
            low_stock_threshold=low_stock_threshold,
            replace_login_details=False,
        )
    except ValueError as exc:
        return redirect(f"/admin/stock/{service_id}/edit?error={quote(str(exc))}")
    return redirect(f"/admin/stock?message={quote(f'Updated stock for {service.name}')}")


@router.post("/stock/{service_id}/logins")
async def edit_stock_logins_route(
    service_id: int,
    request: Request,
    login_details: str = Form(""),
    db: Session = Depends(get_db),
):
    """Update account/login lines from View stock. New lines trigger stock notify (1 line = 1 stock)."""
    admin_required(request)
    service = db.get(Service, service_id)
    if not service or service.is_deleted:
        return redirect(f"/admin/stock?error={quote('Product not found')}")
    stock = service.stock
    if not stock:
        return redirect(f"/admin/stock?error={quote('Stock row not found')}")

    old_lines = [line.strip() for line in (stock.login_details or "").splitlines() if line.strip()]
    lines = [line.strip() for line in (login_details or "").splitlines() if line.strip()]
    added = max(0, len(lines) - len(old_lines))
    cleaned = "\n".join(lines) if lines else None
    stock.stock_type = "account"
    stock.is_unlimited = False
    reserved = max(int(stock.reserved_qty or 0), 0)
    # 1 login/promo line = 1 stock unit (+ reserved holds).
    quantity = max(len(lines) + reserved, reserved)

    try:
        set_stock(
            db,
            service_id,
            quantity,
            reserved,
            stock.notes,
            cleaned or "",
            low_stock_threshold=stock.low_stock_threshold,
            replace_login_details=True,
        )
    except ValueError as exc:
        return redirect(f"/admin/stock?error={quote(str(exc))}")

    db.refresh(service)
    if service.stock:
        db.refresh(service.stock)

    if added > 0:
        sent = await notify_stock_added(service, added)
        return redirect(
            f"/admin/stock?message={quote(f'Added {added} line(s) for {service.name}. Stock notification sent to {sent} users.')}"
        )
    return redirect(f"/admin/stock?message={quote(f'Updated logins for {service.name}')}")



# ── Product Sales (Flash / Season End / etc.) ─────────────────

SALE_TYPE_OPTIONS = [
    ("flash", "Flash Sale"),
    ("season_end", "Season End Sale"),
    ("new_year", "New Year Sale"),
    ("black_friday", "Black Friday"),
    ("cyber_monday", "Cyber Monday"),
    ("clearance", "Clearance Sale"),
    ("weekend", "Weekend Sale"),
    ("summer", "Summer Sale"),
    ("winter", "Winter Sale"),
    ("spring", "Spring Sale"),
    ("eid", "Eid Sale"),
    ("valentine", "Valentine Sale"),
    ("christmas", "Christmas Sale"),
    ("mega", "Mega Sale"),
    ("introductory", "Introductory Offer"),
    ("price_drop", "Price Drop"),
    ("back_to_school", "Back to School"),
    ("anniversary", "Anniversary Sale"),
    ("mid_season", "Mid-Season Sale"),
    ("liquidation", "Liquidation Sale"),
]


@router.get("/sales")
def sales_list(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    sales = (
        db.query(ProductSale)
        .options(joinedload(ProductSale.service))
        .order_by(ProductSale.created_at.desc())
        .all()
    )
    products = (
        db.query(Service)
        .filter(Service.is_active.is_(True), Service.is_deleted.is_(False))
        .order_by(Service.name.asc())
        .all()
    )
    return render(
        request,
        "sales.html",
        {
            "sales": sales,
            "products": products,
            "sale_types": SALE_TYPE_OPTIONS,
        },
    )


def _find_user_for_discount(db: Session, user_query: str) -> User | None:
    """Resolve Telegram ID or @username to a User row."""
    raw = (user_query or "").strip()
    if not raw:
        return None
    if raw.startswith("@"):
        return db.query(User).filter(User.username.ilike(raw.lstrip("@"))).first()
    # Prefer exact telegram_id match (stored as string).
    user = db.query(User).filter(User.telegram_id == raw).first()
    if user:
        return user
    if raw.isdigit():
        user = db.query(User).filter(User.telegram_id == str(int(raw))).first()
        if user:
            return user
    return db.query(User).filter(User.username.ilike(raw.lstrip("@"))).first()


@router.get("/user-discounts")
def user_discounts_list(
    request: Request,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    admin_required(request)
    from utils.pricing import apply_discount_to_price

    search = (q or "").strip().lstrip("@")
    query = (
        db.query(UserProductDiscount)
        .options(
            joinedload(UserProductDiscount.user),
            joinedload(UserProductDiscount.service),
        )
        .order_by(UserProductDiscount.created_at.desc())
    )
    if search:
        like = f"%{search}%"
        query = query.join(User, User.id == UserProductDiscount.user_id).outerjoin(
            Service, Service.id == UserProductDiscount.service_id
        ).filter(
            or_(
                User.telegram_id.ilike(like),
                User.username.ilike(like),
                User.full_name.ilike(like),
                Service.name.ilike(like),
            )
        )
    discounts = query.all()
    for row in discounts:
        base = float(row.service.sell_price) if row.service else 0.0
        row.effective_price = apply_discount_to_price(base, row)

    products = (
        db.query(Service)
        .filter(Service.is_active.is_(True), Service.is_deleted.is_(False))
        .order_by(Service.name.asc())
        .all()
    )
    qp = request.query_params
    return render(
        request,
        "user_discounts.html",
        {
            "discounts": discounts,
            "products": products,
            "q": search,
            "message": qp.get("success") or qp.get("message"),
            "error": qp.get("error"),
        },
    )


@router.post("/user-discounts/create")
def user_discounts_create(
    request: Request,
    user_query: str = Form(...),
    service_id: int = Form(...),
    discount_type: str = Form(...),
    value: float = Form(...),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    admin_required(request)
    dtype = (discount_type or "").strip().lower()
    if dtype not in ("percent", "fixed", "price"):
        return redirect(f"/admin/user-discounts?error={quote('Invalid discount type')}")
    if value < 0:
        return redirect(f"/admin/user-discounts?error={quote('Value must be >= 0')}")
    if dtype == "percent" and value > 100:
        return redirect(f"/admin/user-discounts?error={quote('Percent must be 0–100')}")

    user = _find_user_for_discount(db, user_query)
    if not user:
        return redirect(
            f"/admin/user-discounts?error={quote('User not found — they must /start the bot first')}"
        )

    service = db.get(Service, service_id)
    if not service or service.is_deleted:
        return redirect(f"/admin/user-discounts?error={quote('Product not found')}")

    # One active discount per user+product: deactivate previous active rows.
    existing = (
        db.query(UserProductDiscount)
        .filter(
            UserProductDiscount.user_id == user.id,
            UserProductDiscount.service_id == service.id,
            UserProductDiscount.is_active.is_(True),
        )
        .all()
    )
    for old in existing:
        old.is_active = False

    db.add(
        UserProductDiscount(
            user_id=user.id,
            service_id=service.id,
            discount_type=dtype,
            value=float(value),
            note=(note or "").strip() or None,
            is_active=True,
        )
    )
    db.commit()
    return redirect(f"/admin/user-discounts?success={quote('Discount saved')}")


@router.post("/user-discounts/{discount_id}/toggle")
def user_discounts_toggle(discount_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    row = db.get(UserProductDiscount, discount_id)
    if not row:
        return redirect(f"/admin/user-discounts?error={quote('Discount not found')}")
    if not row.is_active:
        # Activating: turn off other active discounts for same user+product.
        others = (
            db.query(UserProductDiscount)
            .filter(
                UserProductDiscount.user_id == row.user_id,
                UserProductDiscount.service_id == row.service_id,
                UserProductDiscount.is_active.is_(True),
                UserProductDiscount.id != row.id,
            )
            .all()
        )
        for other in others:
            other.is_active = False
        row.is_active = True
    else:
        row.is_active = False
    db.commit()
    return redirect("/admin/user-discounts")


@router.post("/user-discounts/{discount_id}/delete")
def user_discounts_delete(discount_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    row = db.get(UserProductDiscount, discount_id)
    if row:
        db.delete(row)
        db.commit()
    return redirect("/admin/user-discounts")


@router.post("/sales/create")
async def sales_create(
    request: Request,
    service_id: int = Form(...),
    sale_type: str = Form(...),
    sale_price: float = Form(...),
    duration_hours: str = Form(""),
    activate: str | None = Form(None),
    db: Session = Depends(get_db),
):
    admin_required(request)
    service = db.get(Service, service_id)
    if not service or service.is_deleted:
        return redirect(f"/admin/sales?error={quote('Product not found')}")

    current = float(service.sell_price or 0)
    if sale_price <= 0 or sale_price >= current:
        return redirect(
            f"/admin/sales?error={quote('Sale price must be > 0 and below current price')}"
        )

    valid = {key for key, _ in SALE_TYPE_OPTIONS}
    if sale_type not in valid:
        return redirect(f"/admin/sales?error={quote('Invalid sale type')}")

    hours = None
    raw_hours = (duration_hours or "").strip()
    if raw_hours:
        try:
            hours = max(1, min(int(raw_hours), 24 * 60))
        except ValueError:
            hours = None
    if sale_type == "flash" and not hours:
        hours = 24

    sale = ProductSale(
        service_id=service.id,
        sale_type=sale_type,
        original_price=current,
        sale_price=float(sale_price),
        duration_hours=hours,
        is_active=False,
    )
    db.add(sale)
    db.commit()
    db.refresh(sale)

    if activate in {"1", "true", "on", "yes"}:
        await _activate_product_sale(db, sale)
        return redirect(f"/admin/sales?success={quote('Sale created and activated')}")
    return redirect(f"/admin/sales?success={quote('Sale created')}")


@router.post("/sales/{sale_id}/activate")
async def sales_activate(sale_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    sale = db.query(ProductSale).options(joinedload(ProductSale.service)).filter(ProductSale.id == sale_id).first()
    if not sale:
        return redirect(f"/admin/sales?error={quote('Sale not found')}")
    await _activate_product_sale(db, sale)
    return redirect(f"/admin/sales?success={quote('Sale activated & notification sent')}")


@router.post("/sales/{sale_id}/deactivate")
async def sales_deactivate(sale_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    sale = db.query(ProductSale).options(joinedload(ProductSale.service)).filter(ProductSale.id == sale_id).first()
    if not sale:
        return redirect(f"/admin/sales?error={quote('Sale not found')}")
    _deactivate_product_sale(db, sale, hard_delete=False)
    return redirect(f"/admin/sales?success={quote('Sale deactivated')}")


@router.post("/sales/{sale_id}/delete")
async def sales_delete(sale_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    sale = db.query(ProductSale).options(joinedload(ProductSale.service)).filter(ProductSale.id == sale_id).first()
    if not sale:
        return redirect(f"/admin/sales?error={quote('Sale not found')}")
    _deactivate_product_sale(db, sale, hard_delete=True)
    return redirect(f"/admin/sales?success={quote('Sale deleted')}")


async def _activate_product_sale(db: Session, sale: ProductSale) -> None:
    """Apply sale price, mark active, broadcast bot notification."""
    service = sale.service or db.get(Service, sale.service_id)
    if not service:
        return

    others = (
        db.query(ProductSale)
        .filter(
            ProductSale.service_id == service.id,
            ProductSale.is_active.is_(True),
            ProductSale.id != sale.id,
        )
        .all()
    )
    for other in others:
        if other.original_price and float(service.sell_price or 0) == float(other.sale_price or 0):
            service.sell_price = float(other.original_price)
        other.is_active = False
        other.ends_at = datetime.utcnow()

    if not sale.original_price or float(sale.original_price) <= 0:
        sale.original_price = float(service.sell_price or 0)

    old_price = float(sale.original_price)
    new_price = float(sale.sale_price)
    service.sell_price = new_price
    sale.is_active = True
    sale.starts_at = datetime.utcnow()
    if sale.duration_hours:
        sale.ends_at = datetime.utcnow() + timedelta(hours=int(sale.duration_hours))
    else:
        sale.ends_at = None
    sale.notified_at = datetime.utcnow()
    db.commit()

    try:
        await notify_product_sale(service, old_price, new_price, hours=sale.duration_hours)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sale notify failed for sale_id=%s: %s", sale.id, exc)


def _deactivate_product_sale(db: Session, sale: ProductSale, *, hard_delete: bool = False) -> None:
    """Restore original price and deactivate or delete the sale row."""
    service = sale.service or db.get(Service, sale.service_id)
    if service and sale.is_active and sale.original_price is not None:
        # Only restore if the live price still matches the sale price
        if abs(float(service.sell_price or 0) - float(sale.sale_price or 0)) < 0.0001:
            service.sell_price = float(sale.original_price)
    sale.is_active = False
    sale.ends_at = datetime.utcnow()
    if hard_delete:
        db.delete(sale)
    db.commit()


def _find_order_for_refund(db: Session, query: str) -> Order | None:
    """Lookup by numeric id or order_code."""
    raw = (query or "").strip()
    if not raw:
        return None
    q = (
        db.query(Order)
        .options(
            joinedload(Order.user),
            joinedload(Order.service).joinedload(Service.stock),
            joinedload(Order.refund_logs),
        )
    )
    if raw.isdigit():
        order = q.filter(Order.id == int(raw)).first()
        if order:
            return order
    return q.filter(or_(Order.order_code == raw, Order.order_code.ilike(raw))).first()


def _refund_tool_base_context(db: Session) -> dict:
    """Shared 'Recent refunds' + 'Reported problems' data reused by every
    Refund Tool view (page load, calculate search, order-status lookup)."""
    recent = (
        db.query(RefundLog)
        .order_by(RefundLog.created_at.desc())
        .limit(25)
        .all()
    )
    issue_reports = (
        db.query(IssueReport)
        .options(joinedload(IssueReport.user), joinedload(IssueReport.order))
        .order_by(IssueReport.created_at.desc())
        .limit(50)
        .all()
    )
    return {"recent_logs": recent, "issue_reports": issue_reports}


@router.get("/refund-tool")
def refund_tool_page(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    return render(
        request,
        "refund_tool.html",
        {
            "order": None,
            "breakdown": None,
            "subscription_days_input": "",
            "search_q": "",
            "lookup_q": "",
            "lookup_result": None,
            **_refund_tool_base_context(db),
            "error": request.query_params.get("error"),
            "success": request.query_params.get("success"),
        },
    )


@router.post("/refund-tool/search")
def refund_tool_search(
    request: Request,
    q: str = Form(""),
    subscription_days: str = Form(""),
    db: Session = Depends(get_db),
):
    admin_required(request)
    base_ctx = _refund_tool_base_context(db)
    order = _find_order_for_refund(db, q)
    if not order:
        return render(
            request,
            "refund_tool.html",
            {
                "order": None,
                "breakdown": None,
                "subscription_days_input": subscription_days,
                "search_q": q,
                "lookup_q": "",
                "lookup_result": None,
                **base_ctx,
                "error": "Order not found.",
                "success": None,
            },
        )

    days_raw = (subscription_days or "").strip()
    days_override = int(days_raw) if days_raw.isdigit() and int(days_raw) > 0 else None
    breakdown = calculate_refund(order, subscription_days=days_override)
    return render(
        request,
        "refund_tool.html",
        {
            "order": order,
            "breakdown": breakdown,
            "subscription_days_input": str(days_override or ""),
            "search_q": q,
            "lookup_q": "",
            "lookup_result": None,
            **base_ctx,
            "error": None,
            "success": None,
        },
    )


@router.post("/refund-tool/check-order")
def refund_tool_check_order(
    request: Request,
    order_lookup: str = Form(""),
    db: Session = Depends(get_db),
):
    """Pure lookup: has this Order ID already been refunded? Never performs
    a refund — just shows the existing refund record, if any."""
    admin_required(request)
    base_ctx = _refund_tool_base_context(db)

    raw = (order_lookup or "").strip()
    lookup_result = None
    error = None
    if not raw:
        error = "Enter an Order ID or code to search."
    else:
        found = _find_order_for_refund(db, raw)
        if not found:
            error = "Order not found."
        else:
            latest_log = (
                db.query(RefundLog)
                .filter(RefundLog.order_id == found.id)
                .order_by(RefundLog.created_at.desc())
                .first()
            )
            refunded = bool(getattr(found, "refund_method", None)) or found.status == "refunded"
            lookup_result = {
                "order_code": found.order_code,
                "refunded": refunded,
                "refund_amount": found.refund_amount,
                "refund_method": found.refund_method,
                "refunded_at": found.refunded_at,
                "note": latest_log.note if latest_log else None,
            }

    return render(
        request,
        "refund_tool.html",
        {
            "order": None,
            "breakdown": None,
            "subscription_days_input": "",
            "search_q": "",
            "lookup_q": raw,
            "lookup_result": lookup_result,
            **base_ctx,
            "error": error,
            "success": None,
        },
    )


@router.post("/refund-tool/report/{report_id}/resolve")
async def refund_tool_resolve_report(
    report_id: int,
    request: Request,
    admin_note: str = Form(""),
    db: Session = Depends(get_db),
):
    """Reported problems → Resolve: saves the admin's note, marks the report
    Resolved, and sends that exact note to the reporting client only."""
    admin_required(request)
    note = (admin_note or "").strip()
    if not note:
        return redirect(f"/admin/refund-tool?error={quote('Write a note before marking resolved.')}")

    report = (
        db.query(IssueReport)
        .options(joinedload(IssueReport.user))
        .filter(IssueReport.id == report_id)
        .first()
    )
    if not report:
        return redirect(f"/admin/refund-tool?error={quote('Reported problem not found.')}")

    report.admin_note = note
    report.status = "resolved"
    report.resolved_at = datetime.utcnow()
    db.commit()

    if report.user and report.user.telegram_id:
        await notify_issue_report_resolved(report.user.telegram_id, report.order_code, note, db=db)

    return redirect(f"/admin/refund-tool?success={quote('Report marked resolved and client notified.')}")


@router.post("/refund-tool/confirm")
async def refund_tool_confirm(
    request: Request,
    order_id: int = Form(...),
    subscription_days: int = Form(...),
    refund_method: str = Form(...),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    admin_required(request)
    method = (refund_method or "").strip().lower()
    if method not in {"wallet", "manual"}:
        return redirect(f"/admin/refund-tool?error={quote('Invalid refund method')}")

    order = (
        db.query(Order)
        .options(
            joinedload(Order.user),
            joinedload(Order.service).joinedload(Service.stock),
        )
        .filter(Order.id == order_id)
        .first()
    )
    if not order or not order.user:
        return redirect(f"/admin/refund-tool?error={quote('Order not found')}")

    if getattr(order, "refund_method", None) or order.status == "refunded":
        return redirect(f"/admin/refund-tool?error={quote('Already refunded (wallet/manual).')}")

    if subscription_days <= 0:
        return redirect(f"/admin/refund-tool?error={quote('Enter valid subscription days')}")

    breakdown = calculate_refund(order, subscription_days=subscription_days)
    if not breakdown.has_refund:
        msg = breakdown.message or "Already complete / no refund found"
        return redirect(f"/admin/refund-tool?error={quote(msg)}")

    try:
        if method == "wallet":
            new_balance, _tx = credit_wallet_refund(
                db,
                order=order,
                user=order.user,
                amount=breakdown.refund_amount,
                breakdown=breakdown,
                admin_actor="admin",
                note=(note or "").strip() or None,
            )
            db.commit()
            await notify_wallet_refund(
                order.user.telegram_id,
                order.order_code,
                breakdown.refund_amount,
                new_balance,
                db=db,
            )
            ok = (
                f"Wallet refund ${float(money(breakdown.refund_amount)):.2f} "
                f"for {order.order_code}. New balance ${float(money(new_balance)):.2f}"
            )
            return redirect(f"/admin/refund-tool?success={quote(ok)}")

        mark_manual_refund(
            db,
            order=order,
            amount=breakdown.refund_amount,
            breakdown=breakdown,
            admin_actor="admin",
            note=(note or "").strip() or None,
        )
        db.commit()
        ok = (
            f"Manual refund ${float(money(breakdown.refund_amount)):.2f} "
            f"marked for {order.order_code}"
        )
        return redirect(f"/admin/refund-tool?success={quote(ok)}")
    except ValueError as exc:
        db.rollback()
        return redirect(f"/admin/refund-tool?error={quote(str(exc))}")
    except Exception:
        db.rollback()
        logger.exception("Refund tool confirm failed for order %s", order_id)
        return redirect(f"/admin/refund-tool?error={quote('Refund failed. Check logs.')}")


@router.get("/orders")
def orders(
    request: Request,
    status: str | None = None,
    q: str | None = None,
    period: str | None = None,
    db: Session = Depends(get_db),
):
    admin_required(request)
    _mark_sidebar_seen(db, "sidebar_seen_orders_at")
    qp = request.query_params
    date_from = qp.get("from")
    date_to = qp.get("to")
    query = (
        db.query(Order)
        .options(
            joinedload(Order.user),
            joinedload(Order.service).joinedload(Service.provider),
        )
    )
    if status:
        query = query.filter(Order.status == status)
    search = (q or "").strip().lstrip("@")
    if search:
        like = f"%{search}%"
        query = query.join(User, Order.user_id == User.id).join(Service, Order.service_id == Service.id).filter(
            or_(
                Order.order_code.ilike(like),
                Order.note.ilike(like),
                Order.delivered_info.ilike(like),
                Order.customer_email.ilike(like),
                User.username.ilike(like),
                User.telegram_id.ilike(like),
                User.full_name.ilike(like),
                Service.name.ilike(like),
            )
        )
    rows = query.order_by(Order.created_at.desc()).all()
    for order in rows:
        order.payment_method_label = _order_payment_method(order, db)
    return render(
        request,
        "orders.html",
        {
            "orders": rows,
            "status": status,
            "q": search,
            "period_stats": orders_period_stats(db, period or qp.get("period"), date_from, date_to),
        },
    )


@router.get("/sold-accounts")
def sold_accounts(
    request: Request,
    period: str | None = None,
    service_id: int | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    """Per-product completed sold units with the same period calendar as Orders."""
    admin_required(request)
    _mark_sidebar_seen(db, "sidebar_seen_sold_accounts_at")
    qp = request.query_params
    date_from = qp.get("from")
    date_to = qp.get("to")
    selected_id = service_id or None
    if selected_id is not None and selected_id <= 0:
        selected_id = None

    period_stats = sold_accounts_period_stats(
        db,
        period or qp.get("period"),
        date_from,
        date_to,
        service_id=selected_id,
    )
    start, end = period_stats.pop("_range")

    products = (
        db.query(Service)
        .options(joinedload(Service.category))
        .filter(Service.is_deleted.is_(False))
        .order_by(Service.sort_order.asc(), Service.name.asc())
        .all()
    )

    search = (q or "").strip()
    if search:
        needle = search.lower()
        products = [p for p in products if needle in admin_plain_text(p.name).lower()]

    if selected_id:
        products = [p for p in products if p.id == selected_id]

    period_rows = (
        db.query(
            Order.service_id,
            func.coalesce(func.sum(Order.quantity), 0).label("sold_units"),
            func.count(Order.id).label("order_count"),
            func.coalesce(func.sum(Order.amount_usdt), 0.0).label("revenue"),
        )
        .filter(
            Order.status == "completed",
            Order.created_at >= start,
            Order.created_at <= end,
        )
        .group_by(Order.service_id)
        .all()
    )
    period_map = {
        int(row.service_id): {
            "sold_units": int(row.sold_units or 0),
            "order_count": int(row.order_count or 0),
            "revenue": float(row.revenue or 0),
        }
        for row in period_rows
        if row.service_id is not None
    }

    all_time_rows = (
        db.query(
            Order.service_id,
            func.coalesce(func.sum(Order.quantity), 0).label("sold_units"),
        )
        .filter(Order.status == "completed")
        .group_by(Order.service_id)
        .all()
    )
    all_time_map = {
        int(row.service_id): int(row.sold_units or 0)
        for row in all_time_rows
        if row.service_id is not None
    }

    rows = []
    for product in products:
        period_data = period_map.get(product.id, {"sold_units": 0, "order_count": 0, "revenue": 0.0})
        rows.append(
            {
                "product": product,
                "sold_units": period_data["sold_units"],
                "order_count": period_data["order_count"],
                "revenue": period_data["revenue"],
                "all_time_sold": all_time_map.get(product.id, 0),
            }
        )
    rows.sort(key=lambda item: (-item["sold_units"], -item["all_time_sold"], admin_plain_text(item["product"].name).lower()))

    # Hide zero-sold products when browsing all products (keep them when filtering one product / search).
    show_zeros = bool(selected_id or search)
    if not show_zeros:
        rows = [row for row in rows if row["sold_units"] > 0 or row["all_time_sold"] > 0]

    return render(
        request,
        "sold_accounts.html",
        {
            "rows": rows,
            "products": (
                db.query(Service)
                .filter(Service.is_deleted.is_(False))
                .order_by(Service.sort_order.asc(), Service.name.asc())
                .all()
            ),
            "service_id": selected_id,
            "q": search,
            "period_stats": period_stats,
            "show_zeros": show_zeros,
        },
    )


def _order_payment_method(order: Order, db: Session | None = None) -> str:
    """Label for how the customer paid (WALLET / PAYFAST / BEP20 / …)."""
    stored = (getattr(order, "payment_method", None) or "").strip()
    if stored:
        return stored.upper()

    note = (order.note or "").strip()
    lower = note.lower()
    if "paid with wallet" in lower or lower.startswith("paid with wallet"):
        return "WALLET"
    if "payfast" in lower or "awaiting payfast" in lower:
        return "PAYFAST"
    match = re.search(r"\bvia\s+([A-Za-z0-9_]+)\b", note, flags=re.IGNORECASE)
    if match:
        return match.group(1).upper()
    match = re.search(r"Awaiting\s+([A-Za-z0-9_]+)\s+payment", note, flags=re.IGNORECASE)
    if match:
        return match.group(1).upper()
    match = re.search(r"Paid via\s+([A-Za-z0-9_]+)", note, flags=re.IGNORECASE)
    if match:
        return match.group(1).upper()

    # Fallback: linked PayFast / deposit transaction
    if db is not None:
        tx = (
            db.query(Transaction)
            .filter(
                Transaction.user_id == order.user_id,
                Transaction.note == f"payfast_order:{order.id}",
            )
            .first()
        )
        if tx:
            return "PAYFAST"
        tx = (
            db.query(Transaction)
            .filter(
                Transaction.user_id == order.user_id,
                Transaction.note.ilike(f"%{order.order_code}%"),
            )
            .order_by(Transaction.created_at.desc())
            .first()
        )
        if tx:
            if tx.tx_type == "deduct":
                return "WALLET"
            via = re.search(r"\bvia\s+([A-Za-z0-9_]+)\b", tx.note or "", flags=re.IGNORECASE)
            if via:
                return via.group(1).upper()
            if "payfast" in (tx.note or "").lower():
                return "PAYFAST"
    return "-"


@router.post("/orders/{order_id}/status")
async def update_order(order_id: int, request: Request, status: str = Form(...), delivered_info: str = Form(""), db: Session = Depends(get_db)):
    admin_required(request)
    order = db.get(Order, order_id)
    referral_notifications: list[dict] = []
    if order:
        previous = order.status
        order.status = status
        if delivered_info.strip():
            order.delivered_info = delivered_info.strip()
        refund_info = None
        completed_now = status == "completed" and previous != "completed"
        if completed_now:
            order.completed_at = datetime.utcnow()
            order.note = order.note or "Delivered manually by admin."
            try:
                complete_reserved_stock(db, order.service_id, order.quantity)
            except InsufficientStockError:
                # Reserved qty was already released/desynced (e.g. order was
                # previously marked failed/cancelled, or this is an API-fulfilled
                # order with nothing reserved). Never 500 the admin — the admin's
                # manual "completed" decision must still be saved.
                logger.warning(
                    "[ADMIN-ORDER] complete_reserved_stock skipped (already released) for order %s",
                    order.order_code,
                )
            referral_notifications += credit_referral_for_order(db, order)
            referral_notifications += credit_referral_join_bonus(db, order.user)
        elif status in {"failed", "cancelled", "expired"} and previous not in {"failed", "cancelled", "expired"}:
            # Always free reserved stock when abandoning an unpaid/unfinished order.
            if previous in {"pending", "manual_pending", "processing"}:
                release_stock(db, order.service_id, order.quantity)
            # Only refund wallet when the customer actually paid from wallet.
            # Unpaid PayFast / external checkouts (status=pending) must NOT credit a refund.
            wallet_paid = "paid with wallet" in ((order.note or "") if previous != "pending" else "").lower()
            if not wallet_paid and previous != "pending":
                deduct = (
                    db.query(Transaction)
                    .filter(
                        Transaction.user_id == order.user_id,
                        Transaction.tx_type == "deduct",
                        Transaction.status == "confirmed",
                        Transaction.note.ilike(f"%{order.order_code}%"),
                    )
                    .first()
                )
                wallet_paid = deduct is not None
            if wallet_paid:
                order.user.wallet_usdt += order.amount_usdt
                db.add(
                    Transaction(
                        user_id=order.user_id,
                        amount=order.amount_usdt,
                        tx_type="refund",
                        status="confirmed",
                        blockchain_status="confirmed",
                        note=f"Refund for {order.order_code}",
                    )
                )
                refund_info = (order.user.telegram_id, order.amount_usdt, order.order_code)
            if status == "expired" and not (order.note or "").lower().startswith("expired:"):
                order.note = "Expired / cancelled by admin (unpaid checkout)."
            # Mark linked unpaid PayFast deposit as expired too (not pending).
            if previous == "pending":
                for tx in (
                    db.query(Transaction)
                    .filter(
                        Transaction.user_id == order.user_id,
                        Transaction.status == "pending",
                        Transaction.note == f"payfast_order:{order.id}",
                    )
                    .all()
                ):
                    tx.status = "expired"
                    tx.blockchain_status = "expired"
                    if tx.note and "Expired:" not in tx.note:
                        tx.note = f"{tx.note} | Expired / cancelled by admin."
        log_admin_action(
            db,
            action="order.status_updated",
            entity_type="order",
            entity_id=str(order.id),
            entity_label=f"order #{order.order_code}",
            change={"from": previous, "to": status},
            request=request,
        )
        db.commit()
        if refund_info:
            telegram_id, amount, order_code = refund_info
            await notify_user_balance_change(telegram_id, amount, f"Refund for order {order_code}")
        if completed_now:
            await notify_user_order_completed(order, order.service)
            await notify_channel_order_completed(order, order.service, db)
        for payload in referral_notifications:
            await notify_referrer_earning(**payload)
    return redirect("/admin/orders")


@router.get("/users")
def users(request: Request, period: str | None = None, q: str | None = None, db: Session = Depends(get_db)):
    admin_required(request)
    _mark_sidebar_seen(db, "sidebar_seen_users_at")
    qp = request.query_params
    search = (q or "").strip().lstrip("@")
    query = db.query(User)
    if search:
        like = f"%{search}%"
        query = query.filter(
            or_(
                User.username.ilike(like),
                User.telegram_id.ilike(like),
                User.full_name.ilike(like),
            )
        )
    return render(
        request,
        "users.html",
        {
            "users": query.order_by(User.joined_at.desc()).all(),
            "q": search,
            "period_stats": users_period_stats(db, period or qp.get("period"), qp.get("from"), qp.get("to")),
        },
    )


@router.post("/users/{user_id}/credit")
async def credit_user(user_id: int, request: Request, amount: float = Form(...), note: str = Form(""), db: Session = Depends(get_db)):
    admin_required(request)
    user = db.get(User, user_id)
    if user and amount > 0:
        user.wallet_usdt += amount
        db.add(Transaction(user_id=user.id, amount=amount, tx_type="admin_credit", status="confirmed", blockchain_status="confirmed", note=note or "Admin credit"))
        db.commit()
        await notify_user_balance_change(user.telegram_id, amount, note or "Admin credit")
    return redirect("/admin/users")


@router.post("/users/{user_id}/debit")
async def debit_user(user_id: int, request: Request, amount: float = Form(...), note: str = Form(""), db: Session = Depends(get_db)):
    admin_required(request)
    user = db.get(User, user_id)
    if user and amount > 0:
        deduct_amount = min(amount, user.wallet_usdt)
        user.wallet_usdt -= deduct_amount
        db.add(Transaction(user_id=user.id, amount=deduct_amount, tx_type="admin_debit", status="confirmed", blockchain_status="confirmed", note=note or "Admin deduction"))
        db.commit()
        await notify_user_balance_change(user.telegram_id, -deduct_amount, note or "Admin deduction")
    return redirect("/admin/users")


@router.post("/users/{user_id}/toggle-ban")
def toggle_ban(user_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    user = db.get(User, user_id)
    if user:
        user.is_banned = not user.is_banned
        db.commit()
    return redirect("/admin/users")


def _transaction_payment_method(tx: Transaction) -> str:
    """Best-effort label for which payment method the customer used."""
    if tx.verification and tx.verification.blockchain:
        return (tx.verification.blockchain or "").strip().upper() or "-"
    note = (tx.note or "").strip()
    if not note:
        # PayFast wallet top-ups used to create pending rows with no note/hash.
        if tx.tx_type == "deposit" and not tx.tx_hash and tx.status == "pending":
            return "PAYFAST?"
        return "-"
    lower = note.lower()
    if lower.startswith("payfast_order:") or "payfast" in lower:
        return "PAYFAST"
    match = re.search(r"\bvia\s+([A-Za-z0-9_]+)\b", note, flags=re.IGNORECASE)
    if match:
        return match.group(1).upper()
    if lower.startswith("deposit via "):
        return note.split(" ", 2)[-1].strip().upper() or "-"
    return "-"


def _transaction_pending_reason(tx: Transaction) -> str:
    """Human-readable reason a row is still pending (or verification failed)."""
    if tx.verification and (tx.verification.reason or "").strip():
        return tx.verification.reason.strip()
    if tx.status != "pending":
        return "-"
    note = (tx.note or "").strip()
    lower = note.lower()
    if lower.startswith("payfast_order:") or "payfast" in lower:
        return "Waiting for PayFast payment / callback (checkout opened, not confirmed yet)"
    if tx.tx_type == "deposit" and not tx.tx_hash:
        return "Waiting for PayFast payment / callback (no TX hash yet)"
    if note:
        return note
    return "Awaiting verification or admin review"


@router.get("/transactions")
def transactions(
    request: Request,
    status: str | None = None,
    q: str | None = None,
    period: str | None = None,
    db: Session = Depends(get_db),
):
    admin_required(request)
    qp = request.query_params
    date_from = qp.get("from")
    date_to = qp.get("to")
    query = db.query(Transaction).options(
        joinedload(Transaction.verification),
        joinedload(Transaction.user),
    )
    if status:
        query = query.filter(Transaction.status == status)
    search = (q or "").strip().lstrip("@")
    if search:
        like = f"%{search}%"
        query = query.join(User).filter(
            or_(
                User.username.ilike(like),
                User.telegram_id.ilike(like),
                User.full_name.ilike(like),
                Transaction.note.ilike(like),
                Transaction.tx_hash.ilike(like),
            )
        )
    rows = query.order_by(Transaction.created_at.desc()).all()
    for tx in rows:
        tx.payment_method_label = _transaction_payment_method(tx)
        tx.pending_reason_label = _transaction_pending_reason(tx)
    return render(
        request,
        "transactions.html",
        {
            "transactions": rows,
            "status": status or "",
            "q": search,
            "period_stats": transactions_period_stats(db, period or qp.get("period"), date_from, date_to),
        },
    )


@router.post("/transactions/{transaction_id}/status")
async def transaction_status(transaction_id: int, request: Request, status: str = Form(...), db: Session = Depends(get_db)):
    admin_required(request)
    from utils.payment_security import payment_ref_already_used

    tx = db.get(Transaction, transaction_id)
    referral_notifications: list[dict] = []
    if tx:
        previous = tx.status
        should_notify = False

        if status == "confirmed" and tx.tx_type == "deposit":
            # Security: never credit without a payment reference / verified proof,
            # never credit a duplicate TXID, never credit anyone except tx.user.
            has_proof = bool(tx.tx_hash) or (
                tx.verification is not None
                and (tx.verification.verification_status or "").lower() in {"verified", "confirmed"}
            )
            if previous in {"expired", "rejected", "failed"} and not has_proof:
                return redirect("/admin/transactions?error=cannot_confirm_unpaid")
            if not has_proof:
                return redirect("/admin/transactions?error=missing_payment_proof")
            if tx.tx_hash and payment_ref_already_used(db, tx.tx_hash, exclude_transaction_id=tx.id):
                return redirect("/admin/transactions?error=duplicate_txid")
            tx.status = "confirmed"
            tx.blockchain_status = "confirmed"
            if tx.verified_at is None:
                tx.user.wallet_usdt += tx.amount
                tx.verified_at = datetime.utcnow()
                should_notify = True
                referral_notifications += credit_referral_join_bonus(db, tx.user)
        elif status == "expired":
            tx.status = "expired"
            tx.blockchain_status = "expired"
            if previous == "pending":
                from utils.checkout_expire import linked_order_id_from_tx

                order_id = linked_order_id_from_tx(tx)
                order = db.get(Order, order_id) if order_id else None
                if order and order.status == "pending" and int(order.user_id) == int(tx.user_id):
                    order.status = "expired"
                    order.note = "Expired / cancelled by admin (unpaid checkout)."
                    release_stock(db, order.service_id, order.quantity)
                if tx.note and "Expired:" not in tx.note:
                    tx.note = f"{tx.note} | Expired / cancelled by admin."
        else:
            tx.status = status
            tx.blockchain_status = "failed"

        telegram_id = tx.user.telegram_id
        amount = tx.amount
        db.commit()
        if should_notify:
            await notify_user_balance_change(telegram_id, amount, "Deposit confirmed")
        for payload in referral_notifications:
            await notify_referrer_earning(**payload)
    return redirect("/admin/transactions")


@router.get("/revenue")
def revenue(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    _mark_sidebar_seen(db, "sidebar_seen_revenue_at")
    since = datetime.utcnow() - timedelta(days=30)
    completed = db.query(Order).filter(Order.status == "completed").all()
    monthly = [order for order in completed if order.completed_at and order.completed_at >= since]
    return render(request, "revenue.html", {"completed": completed, "monthly": monthly})


@router.get("/announcements")
def announcements(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    return render(
        request,
        "announcements.html",
        {
            "announcements": db.query(Announcement).order_by(Announcement.created_at.desc()).all(),
            "icon_presets": db.query(IconPreset).order_by(IconPreset.sort_order.asc(), IconPreset.name.asc()).all(),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/announcements")
async def create_announcement(
    request: Request,
    message: str = Form(...),
    image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    admin_required(request)
    image_path = None
    if image and image.filename:
        upload_dir = get_upload_dir("announcements")
        saved = await save_icon_image(image, upload_dir, "ann")
        if saved:
            # store path relative for FSInputFile (no leading slash)
            image_path = saved.lstrip("/")
    db.add(Announcement(message=message, image_path=image_path))
    db.commit()
    return redirect(f"/admin/announcements?message={quote('Announcement created')}")


@router.post("/announcements/{announcement_id}/send")
async def send_announcement(announcement_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    announcement = db.get(Announcement, announcement_id)
    if not announcement:
        return redirect("/admin/announcements")

    from utils.notifications import post_to_notify_channel

    bot = Bot(token=os.getenv("BOT_TOKEN"))
    users = db.query(User).all()
    sent_count = 0
    resolved_photo = resolve_file_path(announcement.image_path) if announcement.image_path else None
    for user in users:
        try:
            if resolved_photo and resolved_photo.is_file():
                photo = FSInputFile(str(resolved_photo))
                await bot.send_photo(
                    chat_id=user.telegram_id,
                    photo=photo,
                    caption=announcement.message,
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=announcement.message,
                    parse_mode="HTML",
                )
            sent_count += 1
        except Exception as exc:
            logger.warning(f"Failed to send to {user.telegram_id}: {exc}")
    await bot.session.close()

    # Same announcement also posts to Notify Channel.
    channel_ok = await post_to_notify_channel(
        announcement.message,
        db=db,
        parse_mode="HTML",
        photo_path=announcement.image_path or None,
    )

    announcement.sent_count = sent_count
    announcement.is_sent = True
    db.commit()
    extra = " + channel" if channel_ok else ""
    return redirect(f"/admin/announcements?message={quote(f'Sent to {sent_count} users{extra}')}")


@router.get("/api-management")
def api_management(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    return render(request, "api_management.html", {"api_keys": db.query(ApiKey).order_by(ApiKey.created_at.desc()).all(), "users": db.query(User).order_by(User.joined_at.desc()).all()})


@router.post("/api-management/generate")
def generate_key(request: Request, user_id: int = Form(...), rate_limit: int = Form(100), db: Session = Depends(get_db)):
    admin_required(request)
    user = db.get(User, user_id)
    if user:
        # Rotate: revoke previous active keys, issue new one with chosen rate limit.
        regenerate_api_credentials(db, user, rate_limit)
    return redirect("/admin/api-management")


@router.post("/api-management/{key_id}/toggle")
def toggle_key(key_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    key = db.get(ApiKey, key_id)
    if key:
        key.is_active = not key.is_active
        db.commit()
    return redirect("/admin/api-management")


@router.get("/referrals")
def referrals(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    config = db.query(BotConfig).first()
    codes = db.query(ReferralCode).order_by(ReferralCode.created_at.desc()).all()
    earnings = db.query(ReferralEarning).order_by(ReferralEarning.created_at.desc()).all()
    users = db.query(User).order_by(User.joined_at.desc()).all()
    user_lookup = {user.id: (user.username or user.full_name or user.telegram_id) for user in users}
    total_earned = sum(earning.amount_earned for earning in earnings if earning.status != "voided_self_referral")
    pending_earned = sum(earning.amount_earned for earning in earnings if earning.status == "pending")
    voided_count = sum(1 for earning in earnings if earning.status == "voided_self_referral")
    settings = get_referral_settings(db)
    return render(
        request,
        "referral_management.html",
        {
            "codes": codes,
            "earnings": earnings,
            "users": users,
            "user_lookup": user_lookup,
            "total_earned": total_earned,
            "pending_earned": pending_earned,
            "voided_count": voided_count,
            "program_enabled": bool(config and config.referral_enabled),
            "program_type": settings["program_type"],
            "commission_type": settings["commission_type"],
            "commission_value": settings["commission_value"],
            "commission_label": format_commission(settings["commission_type"], settings["commission_value"]),
        },
    )


@router.post("/referrals/program/toggle")
async def toggle_referral_program(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    config = db.query(BotConfig).first()
    if not config:
        config = BotConfig()
        db.add(config)
    config.referral_enabled = not config.referral_enabled
    now_enabled = config.referral_enabled
    db.commit()
    if now_enabled:
        from utils.ui_icons import label_icons

        icons = label_icons(db)
        await broadcast_to_all_users(
            f"{icons['party']} Referral Program is now ACTIVE!\n\n"
            f"Share your referral link and earn commission on every purchase "
            f"made by people you invite. Send /referral to get your personal link.",
            parse_mode="HTML",
        )
    return redirect("/admin/referrals")


@router.post("/referrals/program/update")
def update_referral_program(
    request: Request,
    program_type: str = Form(...),
    commission_type: str = Form(...),
    commission_value: float = Form(...),
    db: Session = Depends(get_db),
):
    """Single place to configure the referral program: pick ONE mode (Per Link
    Earning or Per Purchase Earning) and ONE commission (percent or a flat
    USDT amount). Because only one program_type is ever stored, a referral
    link can never earn from both modes at the same time — switching modes
    here is what turns the other one off."""
    admin_required(request)
    if program_type not in {"per_link", "per_purchase"}:
        program_type = "per_purchase"
    if commission_type not in {"percent", "fixed"}:
        commission_type = "percent"
    # Per-link bonuses are always a flat USDT amount — there's no order total
    # to take a percentage of at join time.
    if program_type == "per_link":
        commission_type = "fixed"

    config = db.query(BotConfig).first()
    if not config:
        config = BotConfig()
        db.add(config)
    config.referral_program_type = program_type
    config.referral_commission_type = commission_type
    config.referral_commission_value = max(0.0, commission_value)
    db.commit()
    return redirect("/admin/referrals")


@router.post("/referrals/{code_id}/toggle")
def toggle_referral_code(code_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    referral = db.get(ReferralCode, code_id)
    if referral:
        referral.is_active = not referral.is_active
        db.commit()
    return redirect("/admin/referrals")


@router.get("/integrations")
def integrations(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    return render(request, "integrations.html", {"api_keys": db.query(ApiKey).filter(ApiKey.is_active.is_(True)).all(), "webhooks": db.query(Webhook).all()})


@router.get("/settings")
def settings(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    config = db.query(BotConfig).first()
    return render(
        request,
        "settings.html",
        {"config": config, "message": request.query_params.get("message")},
    )

@router.post("/settings")
async def update_settings(
    request: Request,
    background_tasks: BackgroundTasks,
    admin_tg_id: str = Form(""),
    welcome_msg: str = Form("Welcome to SMF SHOP!"),
    maintenance: str = Form(""),
    support_username: str = Form(""),
    support_url: str = Form(""),
    support_email: str = Form(""),
    support_note: str = Form(""),
    support_whatsapp: str = Form(""),
    tg_channel_url: str = Form(""),
    whatsapp_channel_url: str = Form(""),
    orders_notify_chat_id: str = Form(""),
    channel_notify_chat_id: str = Form(""),
    force_join_enabled: str = Form(""),
    force_join_channel: str = Form(""),
    force_join_channel_url: str = Form(""),
    force_join_group: str = Form(""),
    force_join_group_url: str = Form(""),
    mini_app_url: str = Form(""),
    db: Session = Depends(get_db),
):
    admin_required(request)
    config = db.query(BotConfig).first() or BotConfig()
    was_maintenance = bool(getattr(config, "maintenance", False))
    # Checkbox posts "true" when ticked; missing field when unticked (same as force_join).
    maintenance_on = (maintenance or "").strip().lower() in {"true", "1", "on", "yes"}
    # Source of truth is ADMIN_ID (or ADMIN_TG_ID) env — keep DB in sync with it.
    env_admin_id = (os.getenv("ADMIN_ID") or os.getenv("ADMIN_TG_ID") or "").strip() or None
    config.admin_tg_id = env_admin_id or (admin_tg_id or None)
    config.welcome_msg = welcome_msg
    config.maintenance = maintenance_on
    config.support_username = support_username or None
    config.support_url = support_url or None
    config.support_email = support_email or None
    config.support_note = support_note or None
    config.support_whatsapp = support_whatsapp.strip() or None
    config.tg_channel_url = tg_channel_url.strip() or None
    config.whatsapp_channel_url = whatsapp_channel_url.strip() or None

    # Resolve public t.me / @username → numeric -100… ids (and validate bot can see them).
    from utils.notifications import resolve_telegram_chat_ref

    notify_notes: list[str] = []
    raw_buy_group = orders_notify_chat_id.strip() or ""
    raw_channel = channel_notify_chat_id.strip() or ""

    if raw_buy_group:
        buy_id, buy_err = await resolve_telegram_chat_ref(
            raw_buy_group,
            expect_types=("group", "supergroup"),
        )
        if buy_id:
            config.orders_notify_chat_id = buy_id
            notify_notes.append(f"Notify group id={buy_id}")
        else:
            config.orders_notify_chat_id = raw_buy_group
        if buy_err:
            notify_notes.append(buy_err)
    else:
        config.orders_notify_chat_id = None

    if raw_channel:
        ch_id, ch_err = await resolve_telegram_chat_ref(
            raw_channel,
            expect_types=("channel", "group", "supergroup"),
        )
        if ch_id:
            config.channel_notify_chat_id = ch_id
            notify_notes.append(f"Notify channel id={ch_id}")
        else:
            config.channel_notify_chat_id = raw_channel
        if ch_err:
            notify_notes.append(ch_err)
    else:
        config.channel_notify_chat_id = None

    config.force_join_enabled = force_join_enabled.lower() == "true"
    config.force_join_channel = force_join_channel.strip() or None
    config.force_join_channel_url = force_join_channel_url.strip() or None

    raw_fj_group = force_join_group.strip() or ""
    if raw_fj_group:
        fj_id, fj_err = await resolve_telegram_chat_ref(
            raw_fj_group,
            expect_types=("group", "supergroup"),
        )
        if fj_id:
            config.force_join_group = fj_id
            notify_notes.append(f"Force-join group id={fj_id}")
        else:
            config.force_join_group = raw_fj_group
        if fj_err:
            notify_notes.append(fj_err)
    else:
        config.force_join_group = None
    config.force_join_group_url = force_join_group_url.strip() or None

    from utils.helpers import normalize_mini_app_url

    raw_mini_app = mini_app_url.strip()
    if raw_mini_app:
        normalized_mini = normalize_mini_app_url(raw_mini_app)
        if not normalized_mini:
            return redirect(
                f"/admin/settings?message={quote('Mini App URL must be https:// (localhost http allowed for testing)')}"
            )
        config.mini_app_url = normalized_mini
    else:
        config.mini_app_url = None
    db.add(config)
    log_admin_action(
        db,
        action="setting.changed",
        entity_type="app_setting",
        entity_id="bot_config",
        entity_label="app settings",
        change={"maintenance": maintenance_on, "welcome_updated": True},
        request=request,
    )
    db.commit()

    bot = getattr(request.app.state, "bot", None)
    if bot is not None:
        from bot.bot_main import apply_mini_app_menu_button

        background_tasks.add_task(apply_mini_app_menu_button, bot)

    # Background so Save returns immediately; broadcast can take a while.
    if maintenance_on and not was_maintenance:
        from utils.maintenance import on_maintenance_enabled

        background_tasks.add_task(on_maintenance_enabled)
        return redirect(
            f"/admin/settings?message={quote('Maintenance ON — broadcasting notice and hiding bot commands…')}"
        )
    if was_maintenance and not maintenance_on:
        from utils.maintenance import on_maintenance_disabled

        background_tasks.add_task(on_maintenance_disabled)
        return redirect(
            f"/admin/settings?message={quote('Maintenance OFF — notifying users that the bot is active…')}"
        )
    if maintenance_on and was_maintenance:
        # Keep slash commands hidden after deploys / other setting saves.
        from utils.maintenance import hide_global_bot_commands

        background_tasks.add_task(hide_global_bot_commands)

    if notify_notes:
        return redirect(f"/admin/settings?message={quote(' | '.join(notify_notes))}")
    return redirect("/admin/settings")


@router.post("/settings/payment-config")
async def update_payment_config(
    request: Request,
    usdt_address: str = Form(""),
    usdt_network: str = Form("BEP20"),
    min_deposit: float = Form(1.0),
    auto_verify_enabled: bool = Form(False),
    bscscan_api_key: str = Form(""),
    tronscan_api_key: str = Form(""),
    usd_to_pkr_rate: float = Form(280.0),
    payfast_merchant_id: str = Form(""),
    payfast_secured_key: str = Form(""),
    payfast_store_id: str = Form(""),
    payfast_base_url: str = Form("https://ipg2.apps.net.pk"),
    payfast_tutorial_url: str = Form(""),
    db: Session = Depends(get_db),
):
    admin_required(request)
    config = db.query(BotConfig).first() or BotConfig()
    config.usdt_address = usdt_address or None
    config.usdt_network = usdt_network
    config.min_deposit = min_deposit
    config.auto_verify_enabled = auto_verify_enabled
    config.bscscan_api_key = bscscan_api_key or None
    config.tronscan_api_key = tronscan_api_key or None
    config.usd_to_pkr_rate = usd_to_pkr_rate or 280.0
    config.payfast_merchant_id = payfast_merchant_id or None
    config.payfast_secured_key = payfast_secured_key or None
    config.payfast_store_id = payfast_store_id or None
    config.payfast_base_url = payfast_base_url or "https://ipg2.apps.net.pk"
    config.payfast_tutorial_url = payfast_tutorial_url or None
    db.add(config)
    log_admin_action(
        db,
        action="setting.changed",
        entity_type="app_setting",
        entity_id="bot_config",
        entity_label="payment configuration",
        change={"payment_config_updated": True},
        request=request,
    )
    db.commit()
    return redirect(f"/admin/payment-methods?tab=config&message={quote('Payment configuration saved')}")


# ---------------------------------------------------------------------------
# Languages (Add / Edit / Delete / Activate-Deactivate)
# ---------------------------------------------------------------------------

@router.get("/languages")
def languages(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    return render(
        request,
        "languages.html",
        {
            "languages": db.query(Language).order_by(Language.sort_order.asc()).all(),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/languages")
def create_language(
    request: Request,
    name: str = Form(...),
    code: str = Form(...),
    flag: str = Form("🌐"),
    sort_order: int = Form(0),
    db: Session = Depends(get_db),
):
    admin_required(request)
    clean_code = code.strip().lower()
    if not clean_code:
        return redirect(f"/admin/languages?error={quote('Code is required')}")

    existing = db.query(Language).filter(Language.code == clean_code).first()
    if existing:
        return redirect(f"/admin/languages?error={quote(f'Code {clean_code} already exists')}")

    db.add(Language(name=name, code=clean_code, flag=flag or "🌐", sort_order=sort_order, is_active=True))
    db.commit()
    return redirect(f"/admin/languages?message={quote('Language added')}")


@router.post("/languages/{language_id}/edit")
def edit_language(
    language_id: int,
    request: Request,
    name: str = Form(...),
    flag: str = Form("🌐"),
    sort_order: int = Form(0),
    db: Session = Depends(get_db),
):
    admin_required(request)
    language = db.get(Language, language_id)
    if not language:
        return redirect(f"/admin/languages?error={quote('Language not found')}")
    language.name = name
    language.flag = flag or "🌐"
    language.sort_order = sort_order
    db.commit()
    return redirect(f"/admin/languages?message={quote('Language updated')}")


@router.post("/languages/{language_id}/toggle")
def toggle_language(language_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    language = db.get(Language, language_id)
    if language:
        language.is_active = not language.is_active
        db.commit()
    return redirect("/admin/languages")


@router.post("/languages/{language_id}/delete")
def delete_language(language_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    language = db.get(Language, language_id)
    if not language:
        return redirect("/admin/languages")
    db.delete(language)
    db.commit()
    return redirect(f"/admin/languages?message={quote('Language deleted')}")


# ---------------------------------------------------------------------------
# Payment Methods (Add / Edit / Delete / Activate-Deactivate)
# ---------------------------------------------------------------------------

@router.get("/payment-methods")
def payment_methods(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    from utils.helpers import rich_name_with_icon

    methods = db.query(PaymentMethod).order_by(PaymentMethod.sort_order.asc()).all()
    for method in methods:
        method.name_rich = rich_name_with_icon(method.name, method.icon, "💳")
    tab = request.query_params.get("tab") or "methods"
    return render(
        request,
        "payment_methods.html",
        {
            "methods": methods,
            "icon_presets": db.query(IconPreset).order_by(IconPreset.sort_order.asc(), IconPreset.name.asc()).all(),
            "config": db.query(BotConfig).first(),
            "tab": tab,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/payment-methods/new")
def payment_methods_new(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    return render(
        request,
        "payment_method_add.html",
        {
            "icon_presets": db.query(IconPreset).order_by(IconPreset.sort_order.asc(), IconPreset.name.asc()).all(),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/payment-methods")
async def create_payment_method(
    request: Request,
    name: str = Form(...),
    code: str = Form(...),
    method_type: str = Form("manual"),
    network: str = Form(""),
    address: str = Form(""),
    icon_image: UploadFile | None = File(None),
    instructions: str = Form(""),
    sort_order: int = Form(0),
    db: Session = Depends(get_db),
):
    admin_required(request)
    from utils.helpers import extract_icon_from_rich_text

    clean_code = re.sub(r"[^A-Za-z0-9_]+", "_", code.strip()).upper()
    if not clean_code:
        return redirect(f"/admin/payment-methods/new?error={quote('Code is required')}")

    existing = db.query(PaymentMethod).filter(PaymentMethod.code == clean_code).first()
    if existing:
        return redirect(f"/admin/payment-methods/new?error={quote(f'Code {clean_code} already exists')}")

    raw_name = (name or "").strip()
    if not raw_name:
        return redirect(f"/admin/payment-methods/new?error={quote('Name is required')}")
    # Keep PaymentMethod.name plain (no embedded <tg-emoji> markup) since it's
    # rendered with html.escape() in customer-facing wallet/checkout messages
    # elsewhere — the emoji picked in the Name field is captured separately
    # into the existing `icon` column instead, same as before.
    clean_icon = extract_icon_from_rich_text(raw_name, "💳")
    clean_name = admin_plain_text(raw_name) or raw_name

    image_path = await save_icon_image(icon_image, PAYMENT_METHOD_UPLOAD_DIR, "pm")
    db.add(
        PaymentMethod(
            name=clean_name,
            code=clean_code,
            method_type=method_type,
            network=network or None,
            address=address or None,
            icon=clean_icon,
            image_path=image_path,
            instructions=instructions or None,
            sort_order=sort_order,
            is_active=True,
        )
    )
    db.commit()
    return redirect(f"/admin/payment-methods?message={quote('Payment method added')}")


@router.post("/payment-methods/{method_id}/edit")
async def edit_payment_method(
    method_id: int,
    request: Request,
    name: str = Form(...),
    method_type: str = Form("manual"),
    network: str = Form(""),
    address: str = Form(""),
    icon_image: UploadFile | None = File(None),
    remove_image: str = Form(""),
    instructions: str = Form(""),
    sort_order: int = Form(0),
    db: Session = Depends(get_db),
):
    admin_required(request)
    from utils.helpers import extract_icon_from_rich_text

    method = db.get(PaymentMethod, method_id)
    if not method:
        return redirect(f"/admin/payment-methods?error={quote('Payment method not found')}")

    raw_name = (name or "").strip() or method.name
    # Same as create: keep the stored name plain and capture the emoji into
    # the existing `icon` column, so customer-facing html.escape(method.name)
    # usages elsewhere keep working exactly as before.
    method.icon = extract_icon_from_rich_text(raw_name, method.icon or "💳")
    method.name = admin_plain_text(raw_name) or raw_name
    method.method_type = method_type
    method.network = network or None
    method.address = address or None
    if remove_image.lower() == "true":
        method.image_path = None
    new_image_path = await save_icon_image(icon_image, PAYMENT_METHOD_UPLOAD_DIR, "pm")
    if new_image_path:
        method.image_path = new_image_path
    method.instructions = instructions or None
    method.sort_order = sort_order
    db.commit()
    return redirect(f"/admin/payment-methods?message={quote('Payment method updated')}")


@router.post("/payment-methods/{method_id}/toggle")
def toggle_payment_method(method_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    method = db.get(PaymentMethod, method_id)
    if method:
        method.is_active = not method.is_active
        db.commit()
    return redirect("/admin/payment-methods")


@router.post("/payment-methods/{method_id}/delete")
def delete_payment_method(method_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    method = db.get(PaymentMethod, method_id)
    if not method:
        return redirect("/admin/payment-methods")
    db.delete(method)
    db.commit()
    return redirect(f"/admin/payment-methods?message={quote('Payment method deleted')}")


# ---------------------------------------------------------------------------
# Categories (Add / Edit / Delete / Activate-Deactivate)
# ---------------------------------------------------------------------------

async def save_category_image(icon_image: UploadFile | None) -> str | None:
    return await save_icon_image(icon_image, CATEGORY_UPLOAD_DIR, "cat")


@router.get("/categories")
def categories_page(request: Request, q: str = "", db: Session = Depends(get_db)):
    admin_required(request)
    query = db.query(Category)
    needle = (q or "").strip()
    if needle:
        like = f"%{needle}%"
        query = query.filter(or_(Category.name.ilike(like), Category.emoji.ilike(like)))
    return render(
        request,
        "categories.html",
        {
            "categories": query.order_by(Category.sort_order.asc()).all(),
            "icon_presets": db.query(IconPreset).order_by(IconPreset.sort_order.asc(), IconPreset.name.asc()).all(),
            "search_q": needle,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/categories/new")
def categories_new(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    return render(
        request,
        "category_add.html",
        {
            "icon_presets": db.query(IconPreset).order_by(IconPreset.sort_order.asc(), IconPreset.name.asc()).all(),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/categories/{category_id}/edit")
def categories_edit_page(category_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    category = db.get(Category, category_id)
    if not category:
        return redirect(f"/admin/categories?error={quote('Category not found')}")
    from utils.helpers import rich_name_with_icon

    return render(
        request,
        "category_edit.html",
        {
            "category": category,
            "category_name_rich": rich_name_with_icon(category.name, category.emoji),
            "icon_presets": db.query(IconPreset).order_by(IconPreset.sort_order.asc(), IconPreset.name.asc()).all(),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/commands")
def commands_page(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    ensure_menu_commands(db)
    db.commit()
    return render(
        request,
        "commands.html",
        {
            "commands": db.query(MenuCommand).order_by(MenuCommand.sort_order.asc(), MenuCommand.key.asc()).all(),
            "icon_presets": db.query(IconPreset).order_by(IconPreset.sort_order.asc(), IconPreset.name.asc()).all(),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/commands/{command_id}/edit")
def edit_command(
    request: Request,
    command_id: int,
    name: str = Form(...),
    reply_name: str = Form(""),
    icon: str = Form(...),
    sort_order: int = Form(0),
    preset_value: str = Form(""),
    db: Session = Depends(get_db),
):
    admin_required(request)
    row = db.get(MenuCommand, command_id)
    if not row:
        return redirect(f"/admin/commands?error={quote('Command not found')}")

    clean_name = name.strip()
    clean_reply = reply_name.strip() or None
    clean_icon = (preset_value or icon).strip()
    if not clean_name:
        return redirect(f"/admin/commands?error={quote('Name is required')}")
    if not clean_icon:
        return redirect(f"/admin/commands?error={quote('Icon is required')}")

    # Accept plain emoji, or ID|fallback (same as category/product icons).
    if "|" in clean_icon:
        emoji_id, _, fallback = clean_icon.partition("|")
        if not emoji_id.strip().isdigit():
            return redirect(f"/admin/commands?error={quote('Premium emoji ID must be digits only')}")
        clean_icon = f"{emoji_id.strip()}|{(fallback.strip() or '✨')}"

    row.name = clean_name
    row.reply_name = clean_reply
    row.icon = clean_icon
    row.sort_order = sort_order
    db.commit()
    return redirect(f"/admin/commands?message={quote(f'Command {row.key} updated')}")


@router.get("/icon-presets")
def icon_presets_page(request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    presets = db.query(IconPreset).order_by(IconPreset.sort_order.asc(), IconPreset.name.asc()).all()
    return render(
        request,
        "icon_presets.html",
        {
            "presets": presets,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/icon-presets/apply-description")
def apply_product_description_legacy(request: Request):
    """Old Icon Presets description builder — moved to Products description templates."""
    admin_required(request)
    return redirect(
        f"/admin/services/new?error={quote('Description templates now live on Add/Edit Product')}"
    )


@router.post("/icon-presets")
def create_icon_preset(
    request: Request,
    name: str = Form(...),
    emoji_id: str = Form(...),
    fallback_emoji: str = Form("📦"),
    sort_order: int = Form(0),
    db: Session = Depends(get_db),
):
    admin_required(request)
    clean_name = name.strip()
    clean_id = emoji_id.strip()
    if not clean_name or not clean_id.isdigit():
        return redirect(f"/admin/icon-presets?error={quote('Name and a numeric emoji ID are required')}")

    existing = db.query(IconPreset).filter(IconPreset.name == clean_name).first()
    if existing:
        return redirect(f"/admin/icon-presets?error={quote(f'Preset {clean_name} already exists')}")

    db.add(IconPreset(name=clean_name, emoji_id=clean_id, fallback_emoji=fallback_emoji or "📦", sort_order=sort_order))
    db.commit()
    return redirect(f"/admin/icon-presets?message={quote('Preset added')}")


@router.post("/icon-presets/{preset_id}/edit")
def edit_icon_preset(
    preset_id: int,
    request: Request,
    name: str = Form(...),
    emoji_id: str = Form(...),
    fallback_emoji: str = Form("📦"),
    sort_order: int = Form(0),
    db: Session = Depends(get_db),
):
    admin_required(request)
    preset = db.get(IconPreset, preset_id)
    if not preset:
        return redirect(f"/admin/icon-presets?error={quote('Preset not found')}")
    clean_id = emoji_id.strip()
    if not clean_id.isdigit():
        return redirect(f"/admin/icon-presets?error={quote('Emoji ID must be numeric')}")
    preset.name = name.strip() or preset.name
    preset.emoji_id = clean_id
    preset.fallback_emoji = fallback_emoji or "📦"
    preset.sort_order = sort_order
    db.commit()
    return redirect(f"/admin/icon-presets?message={quote('Preset updated')}")


@router.post("/icon-presets/{preset_id}/delete")
def delete_icon_preset(preset_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    preset = db.get(IconPreset, preset_id)
    if preset:
        db.delete(preset)
        db.commit()
    return redirect(f"/admin/icon-presets?message={quote('Preset deleted')}")


@router.post("/categories")
async def create_category(
    request: Request,
    name: str = Form(...),
    sort_order: int = Form(0),
    db: Session = Depends(get_db),
):
    admin_required(request)
    from utils.helpers import extract_icon_from_rich_text

    clean_name = (name or "").strip()
    if not clean_name:
        return redirect(f"/admin/categories/new?error={quote('Name is required')}")

    plain = admin_plain_text(clean_name)
    existing = db.query(Category).filter(Category.name == clean_name).first()
    if not existing:
        # Also block duplicate plain names (emoji-only difference).
        for row in db.query(Category).all():
            if admin_plain_text(row.name).lower() == plain.lower():
                existing = row
                break
    if existing:
        return redirect(f"/admin/categories/new?error={quote(f'Category {plain} already exists')}")

    db.add(
        Category(
            name=clean_name,
            emoji=extract_icon_from_rich_text(clean_name, "📦"),
            sort_order=sort_order,
            is_active=True,
        )
    )
    db.commit()
    return redirect(f"/admin/categories?message={quote('Category added')}")


@router.post("/categories/{category_id}/edit")
async def edit_category(
    category_id: int,
    request: Request,
    name: str = Form(...),
    sort_order: int = Form(0),
    db: Session = Depends(get_db),
):
    admin_required(request)
    from utils.helpers import extract_icon_from_rich_text

    category = db.get(Category, category_id)
    if not category:
        return redirect(f"/admin/categories?error={quote('Category not found')}")

    clean_name = (name or "").strip() or category.name
    category.name = clean_name
    category.emoji = extract_icon_from_rich_text(clean_name, category.emoji or "📦")
    category.sort_order = sort_order

    db.commit()
    return redirect(f"/admin/categories?message={quote('Category updated')}")


@router.post("/categories/{category_id}/toggle")
def toggle_category(category_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    category = db.get(Category, category_id)
    if category:
        category.is_active = not category.is_active
        db.commit()
    return redirect("/admin/categories")


@router.post("/categories/{category_id}/delete")
def delete_category(category_id: int, request: Request, db: Session = Depends(get_db)):
    admin_required(request)
    category = db.get(Category, category_id)
    if not category:
        return redirect("/admin/categories")

    linked_count = db.query(Service).filter(Service.category_id == category_id, Service.is_deleted.is_(False)).count()
    if linked_count > 0:
        return redirect(
            f"/admin/categories?error={quote(f'Cannot delete: {linked_count} product(s) use this category. Move or delete them first.')}"
        )

    db.delete(category)
    db.commit()
    return redirect(f"/admin/categories?message={quote('Category deleted')}")
