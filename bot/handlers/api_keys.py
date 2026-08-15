import html
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func

from database.models import ApiKey, Order, SessionLocal, User
from utils.helpers import (
    format_usdt,
    generate_api_credentials,
    get_or_create_user,
    get_public_base_url,
    icon_button,
    regenerate_api_credentials,
    render_icon,
)
from utils.menu_commands import MenuCommandFilter
from utils.ui_icons import build_ui_icons

router = Router()


def _api_base() -> str:
    base = get_public_base_url()
    return f"{base}/api/v1" if base else "https://your-app.up.railway.app/api/v1"


def _docs_url() -> str:
    base = get_public_base_url()
    return f"{base}/api/docs/reseller" if base else "https://your-app.up.railway.app/api/docs/reseller"


def _mask_key(api_key: str | None) -> str:
    if not api_key:
        return "—"
    if len(api_key) <= 12:
        return api_key
    return f"{api_key[:10]}…{api_key[-4:]}"


def _user_api_stats(db, user: User) -> dict:
    api_orders = (
        db.query(Order)
        .filter(Order.user_id == user.id, Order.order_type.in_(("api", "stock")))
        .count()
    )
    since = datetime.utcnow() - timedelta(days=30)
    recent_spend = (
        db.query(func.coalesce(func.sum(Order.amount_usdt), 0.0))
        .filter(
            Order.user_id == user.id,
            Order.order_type.in_(("api", "stock")),
            Order.created_at >= since,
        )
        .scalar()
        or 0.0
    )
    active_key = db.query(ApiKey).filter(ApiKey.user_id == user.id, ApiKey.is_active.is_(True)).first()
    return {
        "orders": int(api_orders),
        "recent_spend": float(recent_spend),
        "key": active_key,
        "balance": float(user.wallet_usdt or 0),
    }


def _panel_keyboard(has_key: bool, icons: dict[str, str] | None = None) -> InlineKeyboardMarkup:
    icons = icons or {}
    rows = [
        [
            icon_button(
                "Generate New API Key" if has_key else "Generate API Key",
                icon_value=icons.get("new"),
                icon_fallback="🆕",
                callback_data="api:generate",
                style="success",
            )
        ],
        [
            icon_button(
                "View API Documentation",
                icon_value=icons.get("view"),
                icon_fallback="📖",
                callback_data="api:docs",
                style="primary",
            )
        ],
        [
            icon_button(
                "Refresh",
                icon_value=icons.get("refresh"),
                icon_fallback="🔄",
                callback_data="api:refresh",
                style="primary",
            )
        ],
    ]
    if has_key:
        rows.append(
            [
                icon_button(
                    "Revoke Key",
                    icon_value=icons.get("disabled"),
                    icon_fallback="🛑",
                    callback_data="api:revoke",
                    style="danger",
                )
            ]
        )
    rows.append(
        [
            icon_button(
                "Back",
                icon_value=icons.get("back"),
                icon_fallback="◀️",
                callback_data="menu:back",
                style="danger",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_api_panel_text(user: User, stats: dict, *, flash: str | None = None, icons: dict[str, str] | None = None) -> str:
    icons = icons or {}
    key = stats["key"]
    has_key = bool(key)
    status = "Active" if has_key else "Disabled"
    status_icon = render_icon(
        icons.get("active") if has_key else icons.get("disabled"),
        fallback="✅" if has_key else "🛑",
        html_mode=True,
    )
    api_icon = render_icon(icons.get("api"), "🔗", html_mode=True)
    price_icon = render_icon(icons.get("price"), "💵", html_mode=True)
    stock_icon = render_icon(icons.get("stock"), "📦", html_mode=True)
    usd_icon = render_icon(icons.get("usd"), "💸", html_mode=True)
    key_icon = render_icon(icons.get("key"), "🔑", html_mode=True)
    key_line = html.escape(key.api_key) if key else "—"
    rate = f"{key.rate_limit}/hour" if key else "—"
    base = html.escape(_api_base())
    lines = [
        f"{api_icon} <b>Reseller Product API</b>",
        "",
        "Connect your own bot, website, or reseller panel to this shop.",
        "Orders are delivered from live stock and paid from your wallet balance.",
        "",
        f"{status_icon} <b>Status:</b> {html.escape(status)}",
        f"{price_icon} <b>API Balance:</b> {format_usdt(stats['balance'])}",
        f"{stock_icon} <b>Total API Orders:</b> {stats['orders']}",
        f"{usd_icon} <b>Recent Spend (30d):</b> {format_usdt(stats['recent_spend'])}",
        f"{key_icon} <b>Current Key:</b> <code>{key_line}</code>",
        f"⏱ <b>Rate limit:</b> {html.escape(str(rate))}",
        "",
        "<b>Available actions</b>",
        f"• Product list: <code>GET {base}/products</code>",
        f"• Balance check: <code>GET {base}/account/balance</code>",
        f"• Place order: <code>POST {base}/orders/create</code>",
        "",
        f"Endpoint: <code>{base}</code>",
        "Auth header: <code>Authorization: Bearer YOUR_KEY</code>",
    ]
    if flash:
        lines.insert(2, flash)
        lines.insert(3, "")
    return "\n".join(lines)


def build_api_docs_text(icons: dict[str, str] | None = None) -> str:
    icons = icons or {}
    view_icon = render_icon(icons.get("view"), "📖", html_mode=True)
    base = html.escape(_api_base())
    return "\n".join(
        [
            f"{view_icon} <b>API Documentation</b>",
            "",
            "Send your key in one of these ways:",
            "• <code>Authorization: Bearer YOUR_KEY</code>",
            "• <code>x-api-key: YOUR_KEY</code> (if your client supports custom headers)",
            "",
            "<b>Endpoints</b>",
            f"• Products: <code>GET {base}/products</code>",
            f"• Product: <code>GET {base}/products/{{sku}}</code>",
            f"• Balance: <code>GET {base}/account/balance</code>",
            f"• Place order: <code>POST {base}/orders/create</code>",
            f"• Order status: <code>GET {base}/orders/{{order_code}}</code>",
            f"• Stats: <code>GET {base}/stats</code>",
            "",
            "<b>Order body example</b>",
            "<code>{",
            '  "sku": "capcut_pro_1m",',
            '  "quantity": 1,',
            '  "link": "https://example.com/profile"',
            "}</code>",
            "",
            "Wallet balance is deducted when the order is placed.",
            f"Full docs: {_docs_url()}",
        ]
    )


async def send_api_panel(target, telegram_user, *, flash: str | None = None) -> None:
    db = SessionLocal()
    try:
        user = get_or_create_user(db, str(telegram_user.id), telegram_user.username, telegram_user.full_name)
        stats = _user_api_stats(db, user)
        icons = build_ui_icons(db)
        text = build_api_panel_text(user, stats, flash=flash, icons=icons)
        markup = _panel_keyboard(bool(stats["key"]), icons)
    finally:
        db.close()
    await target.answer(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


@router.message(Command("api"))
@router.message(MenuCommandFilter("api"))
async def api_command(message: Message) -> None:
    # Auto-issue a key the first time so clients don't need admin.
    db = SessionLocal()
    try:
        user = get_or_create_user(db, str(message.from_user.id), message.from_user.username, message.from_user.full_name)
        active = db.query(ApiKey).filter(ApiKey.user_id == user.id, ApiKey.is_active.is_(True)).first()
        flash = None
        if not active:
            key, _secret = generate_api_credentials(db, user)
            flash = f"✅ API key created automatically:\n<code>{html.escape(key.api_key)}</code>"
    finally:
        db.close()
    await send_api_panel(message, message.from_user, flash=flash)


@router.callback_query(F.data == "api:refresh")
async def api_refresh(callback: CallbackQuery) -> None:
    await send_api_panel(callback.message, callback.from_user)
    await callback.answer("Refreshed")


@router.callback_query(F.data == "api:generate")
async def api_generate(callback: CallbackQuery) -> None:
    db = SessionLocal()
    try:
        user = get_or_create_user(db, str(callback.from_user.id), callback.from_user.username, callback.from_user.full_name)
        key, _secret = regenerate_api_credentials(db, user)
        flash = (
            "✅ <b>New API key generated.</b> Old keys were revoked.\n"
            f"<code>{html.escape(key.api_key)}</code>"
        )
    finally:
        db.close()
    await send_api_panel(callback.message, callback.from_user, flash=flash)
    await callback.answer("New key ready")


@router.callback_query(F.data == "api:revoke")
async def api_revoke(callback: CallbackQuery) -> None:
    db = SessionLocal()
    try:
        user = get_or_create_user(db, str(callback.from_user.id), callback.from_user.username, callback.from_user.full_name)
        keys = db.query(ApiKey).filter(ApiKey.user_id == user.id, ApiKey.is_active.is_(True)).all()
        if not keys:
            await callback.answer("No active key", show_alert=True)
            return
        for key in keys:
            key.is_active = False
        db.commit()
        flash = "🛑 All API keys revoked. Generate a new one when ready."
    finally:
        db.close()
    await send_api_panel(callback.message, callback.from_user, flash=flash)
    await callback.answer("Key revoked")


@router.callback_query(F.data == "api:docs")
async def api_docs(callback: CallbackQuery) -> None:
    db = SessionLocal()
    try:
        icons = build_ui_icons(db)
    finally:
        db.close()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                icon_button(
                    "Open full docs page",
                    icon_value=icons.get("view"),
                    icon_fallback="📖",
                    url=_docs_url(),
                )
            ]
            if get_public_base_url()
            else [],
            [
                icon_button(
                    "API Panel",
                    icon_value=icons.get("api"),
                    icon_fallback="🔗",
                    callback_data="api:refresh",
                    style="primary",
                ),
                icon_button(
                    "Back",
                    icon_value=icons.get("back"),
                    icon_fallback="◀️",
                    callback_data="menu:back",
                    style="danger",
                ),
            ],
        ]
    )
    # Remove empty first row when no public URL
    markup.inline_keyboard = [row for row in markup.inline_keyboard if row]
    await callback.message.answer(
        build_api_docs_text(icons),
        parse_mode="HTML",
        reply_markup=markup,
        disable_web_page_preview=True,
    )
    await callback.answer()
