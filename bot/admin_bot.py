import html
import os

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from database.models import BotConfig, Order, SessionLocal, Transaction, User
from utils.helpers import get_public_base_url


router = Router()

NO_ACCESS_TEXT = "🚫 No access"
ADMIN_PANEL_REPLY_TEXT = "🔐 Admin Panel"


def get_admin_id() -> str | None:
    """Railway/SMF-style ADMIN_ID, with ADMIN_TG_ID as fallback."""
    return (os.getenv("ADMIN_ID") or os.getenv("ADMIN_TG_ID") or "").strip() or None


def is_admin_user_id(user_id: str | int | None) -> bool:
    admin_id = get_admin_id()
    return bool(admin_id and user_id is not None and str(user_id).strip() == str(admin_id))


def is_admin(message: Message) -> bool:
    """Sirf woh Telegram user jiska ID ENV ADMIN_ID (ya ADMIN_TG_ID) mein set hai."""
    return bool(message.from_user and is_admin_user_id(message.from_user.id))


def sync_admin_tg_id_from_env(db) -> str | None:
    """BotConfig.admin_tg_id ko ENV ADMIN_ID / ADMIN_TG_ID se sync rakhta hai."""
    admin_id = get_admin_id()
    if not admin_id:
        return None
    config = db.query(BotConfig).first()
    if config and config.admin_tg_id != admin_id:
        config.admin_tg_id = admin_id
        db.commit()
    return admin_id


def admin_panel_url() -> str | None:
    """Full login URL.

    Prefer explicit ADMIN_PANEL_URL (e.g. https://xxx.up.railway.app/admin/login).
    Otherwise build from public base URL + /admin/login.
    """
    explicit = (os.getenv("ADMIN_PANEL_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    base = get_public_base_url()
    if not base:
        return None
    return f"{base}/admin/login"


async def deliver_admin_panel_access(message: Message) -> None:
    """Send panel URL + credentials (caller must already verify admin)."""
    db = SessionLocal()
    try:
        sync_admin_tg_id_from_env(db)
    finally:
        db.close()

    username = "admin"
    password = os.getenv("ADMIN_PASSWORD", "admin123")
    url = admin_panel_url()

    if url:
        lines = [
            "🔐 <b>Admin Panel Access</b>",
            "",
            f"🔗 <b>URL:</b> <a href=\"{html.escape(url)}\">{html.escape(url)}</a>",
            f"👤 <b>Username:</b> <code>{html.escape(username)}</code>",
            f"🔑 <b>Password:</b> <code>{html.escape(password)}</code>",
            "",
            "Link open karo → login karo → panel se changes manage karo.",
        ]
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🖥 Open Admin Panel", url=url)]]
        )
        await message.answer(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        return

    await message.answer(
        "🔐 <b>Admin Panel Access</b>\n\n"
        f"👤 <b>Username:</b> <code>{html.escape(username)}</code>\n"
        f"🔑 <b>Password:</b> <code>{html.escape(password)}</code>\n\n"
        "⚠️ Panel URL set nahi hai. Railway Variables mein "
        "<code>ADMIN_PANEL_URL</code> add karo, e.g.\n"
        "<code>https://your-app.up.railway.app/admin/login</code>",
        parse_mode="HTML",
    )


@router.message(Command("admin", "administration"))
@router.message(F.text == ADMIN_PANEL_REPLY_TEXT)
async def admin_panel_access(message: Message) -> None:
    """Admin ko web panel ka URL + login credentials bhejta hai.

    Username hamesha `admin`, password ENV `ADMIN_PASSWORD` se.
    Access sirf ADMIN_ID (ya ADMIN_TG_ID) wale Telegram user ke liye —
    baaki logon ko "No access" milta hai.
    """
    if not is_admin(message):
        await message.answer(NO_ACCESS_TEXT)
        return
    await deliver_admin_panel_access(message)


@router.message(Command("adminstats"))
async def admin_stats(message: Message) -> None:
    if not is_admin(message):
        await message.answer(NO_ACCESS_TEXT)
        return

    db = SessionLocal()
    try:
        sync_admin_tg_id_from_env(db)
        users = db.query(User).count()
        orders = db.query(Order).count()
        pending = db.query(Order).filter(Order.status.in_(["pending", "manual_pending", "processing"])).count()
        deposits = db.query(Transaction).filter(Transaction.tx_type == "deposit", Transaction.status == "pending").count()
    finally:
        db.close()

    url = admin_panel_url()
    panel_line = f"\n🖥 Panel: {url}" if url else "\n🖥 Panel: /admin for credentials + link"
    await message.answer(
        "📊 Admin stats\n"
        f"Users: {users}\n"
        f"Orders: {orders}\n"
        f"Pending orders: {pending}\n"
        f"Pending deposits: {deposits}"
        f"{panel_line}"
    )
