"""
Central place for every Telegram notification the bot sends out:
- admin ko har order/purchase ki notification (manual orders clearly flagged)
- user ko uske wallet balance change (add/deduct) ki notification
- naya product ya stock add hone par sab users ko broadcast

Har function apna khud ka short-lived aiogram Bot instance banata hai
(same pattern jo already /admin/announcements/{id}/send route mein use ho raha
hai), isliye ye admin panel (FastAPI routes) aur bot handlers (aiogram) dono
jagah se safely call ho sakte hain.
"""

import asyncio
import html
import logging
import os
import re

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove

from database.models import BotConfig, SessionLocal, User

logger = logging.getLogger(__name__)

# Any markup Telegram accepts on send_message (inline OR reply / remove).
_BroadcastMarkup = InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove | None


def _bot_token() -> str:
    return (os.getenv("BOT_TOKEN") or "").strip().strip('"').strip("'")


def _get_admin_id(db=None) -> str | None:
    own_session = db is None
    db = db or SessionLocal()
    try:
        config = db.query(BotConfig).first()
        admin_id = config.admin_tg_id if config and config.admin_tg_id else None
        return admin_id or os.getenv("ADMIN_ID") or os.getenv("ADMIN_TG_ID")
    finally:
        if own_session:
            db.close()


async def send_admin_message(text: str, db=None, parse_mode: str | None = None) -> None:
    """Sirf admin ko (BotConfig.admin_tg_id) message bhejta hai."""
    token = _bot_token()
    admin_id = _get_admin_id(db)
    if not token or not admin_id:
        logger.warning("Admin notification skipped: BOT_TOKEN or admin id not configured")
        return
    bot = Bot(token=token)
    try:
        await bot.send_message(chat_id=admin_id, text=text, parse_mode=parse_mode)
    except TelegramAPIError as exc:
        logger.warning(f"Failed to notify admin: {exc}")
    finally:
        await bot.session.close()


async def send_user_message(
    telegram_id: str,
    text: str,
    parse_mode: str | None = None,
    reply_markup: _BroadcastMarkup = None,
) -> bool:
    token = _bot_token()
    if not token:
        return False
    bot = Bot(token=token)
    try:
        await bot.send_message(chat_id=telegram_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
        return True
    except TelegramAPIError as exc:
        logger.warning(f"Failed to notify user {telegram_id}: {exc}")
        return False
    finally:
        await bot.session.close()


async def notify_issue_report_resolved(telegram_id: str, order_code: str, note: str, db=None) -> None:
    """Sends the admin's resolution note for a client's reported problem
    back to that same client only (Refund Tool → Reported problems → Resolve)."""
    if not telegram_id:
        return
    from utils.ui_icons import label_icons

    own_session = db is None
    session = db or SessionLocal()
    try:
        icons = label_icons(session)
    finally:
        if own_session:
            session.close()

    text = (
        f"{icons['tick']} Your reported problem has been resolved\n\n"
        f"{icons['order']} Order: {html.escape(order_code)}\n"
        f"{icons['note']} {html.escape(note)}"
    )
    await send_user_message(telegram_id, text, parse_mode="HTML")


async def broadcast_to_all_users(
    text: str,
    db=None,
    reply_markup: _BroadcastMarkup = None,
    parse_mode: str | None = None,
) -> int:
    """Sab (non-banned) users ko ek text message broadcast karta hai. Returns sent count.

    Accepts inline OR reply keyboards (maintenance-off restores the Quick reply menu).
    Paces sends and retries FloodWait so later users are not skipped while admin
    (often early in the user list) still receives the keyboard.
    """
    own_session = db is None
    db = db or SessionLocal()
    try:
        users = db.query(User).filter(User.is_banned.is_(False)).all()
    finally:
        if own_session:
            db.close()

    token = _bot_token()
    if not token or not users:
        return 0

    bot = Bot(token=token)
    sent = 0
    try:
        for user in users:
            delivered = False
            for _attempt in range(4):
                try:
                    await bot.send_message(
                        chat_id=user.telegram_id,
                        text=text,
                        reply_markup=reply_markup,
                        parse_mode=parse_mode,
                    )
                    sent += 1
                    delivered = True
                    break
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(float(getattr(exc, "retry_after", 1) or 1) + 0.5)
                except TelegramAPIError as exc:
                    logger.warning(f"Broadcast failed for {user.telegram_id}: {exc}")
                    break
            if delivered:
                # Mild pacing — avoids Telegram dropping the rest of the list.
                await asyncio.sleep(0.05)
    finally:
        await bot.session.close()
    return sent


# ---------------------------------------------------------------------------
# Domain-specific helpers — call these from bot handlers / admin routes
# ---------------------------------------------------------------------------

async def notify_referrer_new_join(referrer_telegram_id: str, username: str | None, full_name: str | None, settings: dict) -> None:
    """Sent the moment someone starts the bot using a referral link — tells the
    referrer who joined and, depending on the active program type, whether
    they're paid right away (per purchase) or once the new user becomes active
    (per link), so nobody wonders why a fresh join didn't add money yet."""
    from utils.helpers import format_commission
    from utils.ui_icons import label_icons

    label = f"@{username}" if username else (full_name or "A new user")
    commission_label = format_commission(settings["commission_type"], settings["commission_value"])
    if settings["program_type"] == "per_link":
        reward_line = f"You'll earn {commission_label} once they become active (their first deposit or purchase)."
    else:
        reward_line = f"You'll earn {commission_label} on any purchases they make."

    icons = label_icons()
    text = (
        f"{icons['party']} New referral joined!\n\n"
        f"{label} joined using your referral link.\n"
        f"{reward_line}"
    )
    await send_user_message(referrer_telegram_id, text, parse_mode="HTML")


async def notify_referrer_earning(referrer_telegram_id: str, amount: float, reason: str = "") -> None:
    """Sent whenever a referral commission/bonus is actually credited to the
    referrer's referral balance."""
    from utils.ui_icons import label_icons

    icons = label_icons()
    text = (
        f"{icons['referral']} Referral earning credited!\n\n"
        f"{icons['added']} {amount:.2f} USDT added to your referral balance."
    )
    if reason:
        text += f"\n{reason}"
    await send_user_message(referrer_telegram_id, text, parse_mode="HTML")


async def notify_admin_new_order(order, user, service) -> None:
    """Har order/purchase par admin ko notification. Manual-fulfillment products
    ke liye header aur ek extra warning line add ho jati hai taake admin turant
    samajh jaye ke ye order khud complete karna hai."""
    is_manual_product = getattr(service, "fulfillment_type", "auto") == "manual"

    header = "🆕🛠 MANUAL ORDER — needs completion" if is_manual_product else "🆕 New order received"
    customer_label = user.full_name or user.username or user.telegram_id
    username_line = f"@{user.username}" if user.username else "no username"

    lines = [
        header,
        "",
        f"Order: {order.order_code}",
        f"Product: {service.name}",
        f"Quantity: {order.quantity}",
        f"Amount: {order.amount_usdt:.2f} USDT",
        f"Customer: {customer_label} ({username_line}, ID: {user.telegram_id})",
        f"Status: {order.status}",
    ]
    customer_email = (getattr(order, "customer_email", None) or "").strip()
    if customer_email:
        lines.append(f"Email: {customer_email}")
    admin_note = _admin_order_notification_note(order)
    if admin_note:
        lines.append(f"Note: {admin_note}")
    if is_manual_product:
        lines.append("")
        if customer_email:
            lines.append(
                f"⚠️ MANUAL fulfillment — invite/add {customer_email} to the plan, "
                "then mark the order completed from Admin Panel → Orders."
            )
        else:
            lines.append(
                "⚠️ This product is set to MANUAL fulfillment. Please DM the customer "
                "for any account/email details needed, complete the order, then mark "
                "it completed from Admin Panel → Orders."
            )

    await send_admin_message("\n".join(lines))


def _admin_order_notification_note(order) -> str | None:
    """Short admin DM note — never repeat full login/delivery credentials.

    Credentials live in order.delivered_info (Admin → Orders). The customer
    already receives the copyable receipt separately.
    """
    if getattr(order, "delivered_info", None) and (order.status or "") == "completed":
        return "Auto-delivered to customer (see Orders panel for credentials)."
    note = (order.note or "").strip()
    if not note:
        return None
    lowered = note.lower()
    if lowered.startswith("provider delivery:") or "your account details" in lowered:
        return "Auto-delivered to customer via provider API."
    if len(note) > 180:
        return note[:177] + "..."
    return note


def stock_note_text(service) -> str | None:
    """Service ke Stock 'Notes' field mein agar admin ne kuch likha ho (jaise
    warranty ki shart, use-instructions, etc.) to usay return karta hai — taake
    delivery ke waqt customer ko bhi dikhaya ja sake, chahe delivery API se ho,
    stock se ho, ya admin manually complete kare."""
    stock = getattr(service, "stock", None)
    note = stock.notes if stock else None
    return note.strip() if note and note.strip() else None


def copyable_block(text: str) -> str:
    """Kisi bhi text (login/account details) ko Telegram ke HTML <pre> block mein
    wrap karta hai — Telegram clients is par automatically ek 'copy' button dikhate
    hain, taake user ek tap mein poora account detail copy kar sake."""
    return f"<pre>{html.escape(text)}</pre>"


def _plain_delivery_credentials(credentials: str | None) -> str:
    """Ensure delivery credentials are plain text (no nested <pre>/<b>/HTML)."""
    from utils.helpers import strip_html_tags

    text = strip_html_tags(credentials or "").strip()
    if not text:
        return "—"
    # Drop a thank-you line if it was accidentally saved inside credentials.
    lines = []
    for line in text.splitlines():
        if re.match(r"(?i)^\s*thank you for shopping\b", line.strip()):
            continue
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    return cleaned or "—"


def resolve_product_warranty(service) -> str:
    """Use the dedicated Service.warranty field only (not name / description)."""
    from utils.helpers import strip_html_tags

    field = strip_html_tags(getattr(service, "warranty", None) or "").strip()
    return field or "—"


def build_delivery_copyable_text(order, service, credentials: str) -> str:
    """Plain text that goes inside the Telegram copyable <pre> block."""
    from utils.helpers import strip_html_tags

    name = strip_html_tags(getattr(service, "name", None) or "Product").strip() or "Product"
    qty = int(getattr(order, "quantity", 1) or 1)
    price = float(getattr(order, "amount_usdt", 0) or 0)
    warranty = resolve_product_warranty(service)
    creds = _plain_delivery_credentials(credentials)
    return "\n".join(
        [
            f"Product Name: {name}",
            f"Product Quantity: {qty}",
            f"Product Price: {price:.2f} USDT",
            f"Product Warranty: {warranty}",
            "",
            "Your Delivery Details:",
            creds,
        ]
    )


# Telegram text/caption limits make large account dumps fail silently.
# 10+ accounts (or very long payloads) are sent as a downloadable .txt file.
DELIVERY_TXT_MIN_ACCOUNTS = 10
_TELEGRAM_SAFE_HTML_LEN = 3500


def count_delivery_accounts(credentials: str | None) -> int:
    return len([line for line in (credentials or "").splitlines() if line.strip()])


def should_send_delivery_as_file(
    *,
    credentials: str | None,
    quantity: int | None = None,
    receipt_html: str | None = None,
) -> bool:
    if int(quantity or 0) >= DELIVERY_TXT_MIN_ACCOUNTS:
        return True
    if count_delivery_accounts(credentials) >= DELIVERY_TXT_MIN_ACCOUNTS:
        return True
    if credentials and len(credentials) > 3000:
        return True
    if receipt_html and len(receipt_html) > _TELEGRAM_SAFE_HTML_LEN:
        return True
    return False


def delivery_filename(order) -> str:
    code = re.sub(r"[^A-Za-z0-9_-]+", "_", str(getattr(order, "order_code", None) or "order"))
    return f"{code}_accounts.txt"


def delivery_file_notice_html(order) -> str:
    qty = int(getattr(order, "quantity", 0) or 0)
    lines = count_delivery_accounts(getattr(order, "delivered_info", None))
    shown = qty if qty >= DELIVERY_TXT_MIN_ACCOUNTS else max(qty, lines)
    code = html.escape(str(getattr(order, "order_code", "") or "order"))
    return (
        f"📦 <b>{shown} accounts</b> delivered as a text file "
        f"(<code>{code}_accounts.txt</code>). Download the attachment below."
    )


def format_delivery_receipt_html(order, service, credentials: str | None = None) -> str:
    """Copyable receipt + bold thank-you line, or a short file notice for bulk orders."""
    from utils.helpers import BRAND_NAME

    creds = credentials if credentials is not None else (getattr(order, "delivered_info", None) or "")
    if should_send_delivery_as_file(credentials=creds, quantity=getattr(order, "quantity", 0)):
        thanks = f"<b>Thank you for shopping on {html.escape(BRAND_NAME)}</b>"
        return f"{delivery_file_notice_html(order)}\n{thanks}"

    block = copyable_block(build_delivery_copyable_text(order, service, creds))
    thanks = f"<b>Thank you for shopping on {html.escape(BRAND_NAME)}</b>"
    receipt = f"{block}\n{thanks}"
    if should_send_delivery_as_file(credentials=creds, quantity=getattr(order, "quantity", 0), receipt_html=receipt):
        return f"{delivery_file_notice_html(order)}\n{thanks}"
    return receipt


def build_delivery_txt_bytes(order, service, credentials: str) -> bytes:
    return build_delivery_copyable_text(order, service, credentials).encode("utf-8")


async def send_delivery_txt_document(
    *,
    bot: Bot | None = None,
    chat_id: str | int | None = None,
    message=None,
    order,
    service,
    credentials: str,
    caption: str | None = None,
) -> bool:
    """Send accounts as a .txt document (Message.answer_document or Bot.send_document)."""
    from aiogram.types import BufferedInputFile

    payload = build_delivery_txt_bytes(order, service, credentials)
    document = BufferedInputFile(payload, filename=delivery_filename(order))
    caption_text = caption or delivery_file_notice_html(order)
    if message is not None:
        try:
            await message.answer_document(document=document, caption=caption_text, parse_mode="HTML")
            return True
        except TelegramAPIError as exc:
            logger.warning("Failed to send delivery txt via message: %s", exc)
            return False

    token = _bot_token()
    if not chat_id or (bot is None and not token):
        return False
    owns_bot = bot is None
    bot = bot or Bot(token=token)
    try:
        await bot.send_document(
            chat_id=chat_id,
            document=document,
            caption=caption_text,
            parse_mode="HTML",
        )
        return True
    except TelegramAPIError as exc:
        logger.warning("Failed to send delivery txt to %s: %s", chat_id, exc)
        return False
    finally:
        if owns_bot:
            await bot.session.close()


async def maybe_send_delivery_file(
    *,
    order,
    service,
    credentials: str | None = None,
    message=None,
    telegram_id: str | None = None,
    bot: Bot | None = None,
) -> bool:
    """Attach .txt when order has 10+ accounts / long delivery. Returns True if sent."""
    creds = credentials if credentials is not None else (getattr(order, "delivered_info", None) or "")
    if not creds:
        return False
    if not should_send_delivery_as_file(credentials=creds, quantity=getattr(order, "quantity", 0)):
        return False
    if message is not None:
        return await send_delivery_txt_document(
            message=message,
            order=order,
            service=service,
            credentials=creds,
        )
    chat = telegram_id or (order.user.telegram_id if getattr(order, "user", None) else None)
    return await send_delivery_txt_document(
        bot=bot,
        chat_id=chat,
        order=order,
        service=service,
        credentials=creds,
    )


async def notify_user_order_completed(order, service) -> None:
    """Jab admin panel se koi order manually 'completed' mark ho (delivered_info
    ke sath), customer ko turant ek clean, formatted DM jata hai jisme order ki
    poori detail aur (agar di gayi ho) copyable account login shamil hota hai."""
    from bot.keyboards import post_order_actions_keyboard
    from utils.menu_commands import get_command_map
    from utils.ui_icons import build_ui_icons, label_icons

    db = SessionLocal()
    try:
        icons = label_icons(db)
        ui_icons = build_ui_icons(db)
        commands = get_command_map(db)
    finally:
        db.close()
    lines = [
        f"{icons['tick']} Your Order is Completed Successfully",
        "",
        f"{icons['order']} Order: {html.escape(order.order_code)}",
        f"{icons['product']} Product: {html.escape(service.name)}",
        f"{icons['quantity']} Quantity: {order.quantity}",
        f"{icons['price']} Price: {order.amount_usdt:.2f} USDT",
    ]
    if order.delivered_info:
        lines.append("")
        lines.append(format_delivery_receipt_html(order, service, order.delivered_info))
    lines.append("")
    lines.append(
        f'{icons["orders"]} Tap "Order History" in the menu (or send /orders) anytime to view this order again.'
    )

    keyboard = post_order_actions_keyboard(order.id, commands=commands, icons=ui_icons)
    await send_user_message(order.user.telegram_id, "\n".join(lines), parse_mode="HTML", reply_markup=keyboard)
    await maybe_send_delivery_file(
        order=order,
        service=service,
        credentials=order.delivered_info,
        telegram_id=order.user.telegram_id,
    )
    note = stock_note_text(service)
    if note:
        await send_user_message(
            order.user.telegram_id,
            f"{icons['note']} Note:\n{html.escape(note)}",
            parse_mode="HTML",
        )


async def notify_user_balance_change(telegram_id: str, amount: float, note: str = "") -> None:
    """User ko uske wallet balance ke add/deduct hone par DM."""
    from utils.ui_icons import label_icons

    is_credit = amount >= 0
    icons = label_icons()
    sign = icons["added"] if is_credit else "➖"
    verb = "credited to" if is_credit else "deducted from"
    text = (
        f"{icons['wallet']} Wallet update\n\n"
        f"{sign} {abs(amount):.2f} USDT {verb} your balance.\n"
    )
    if note:
        text += f"Reason: {note}\n"
    await send_user_message(telegram_id, text, parse_mode="HTML")


def _product_notify_icons(db) -> dict[str, str]:
    """Premium Icon Presets for product / stock / sale broadcasts (unicode fallbacks)."""
    from utils.stock_display import preset_icon

    return {
        "new": preset_icon(db, ("new",), "🆕"),
        "update": preset_icon(db, ("update", "updated", "stockupdate"), "📦"),
        "added": preset_icon(db, ("added",), "➕"),
        "stock": preset_icon(db, ("stock", "currentstock", "current_stock"), "📦"),
        "price": preset_icon(db, ("price",), "💵"),
        # Sale template (same icons for every sale type)
        "sale": preset_icon(db, ("sale", "flashsale", "flash_sale", "flash"), "🛍️"),
        "hurry": preset_icon(db, ("hurry", "hurryup", "urgent"), "🦖"),
        "was": preset_icon(db, ("was", "oldprice", "price"), "💲"),
        "now": preset_icon(db, ("now", "newprice", "rocket"), "🚀"),
    }


def _sale_notify_hours(hours: int | None) -> int:
    return max(1, int(hours or 24))


def build_sale_notify_html(
    service,
    old_price: float,
    new_price: float,
    hours: int | None = None,
    *,
    db=None,
) -> str:
    """SafwanTiger-style sale card — same template for every sale type.

    SALE emoji  <b>Flash Sale - Limited Time Offer</b>
    product line
    WAS emoji   Was: <s>1.55 USDT</s>
    NOW emoji   Now: 1.00 USDT
    HURRY emoji <b>Hurry End in 12 Hours</b>
    """
    own_session = db is None
    db = db or SessionLocal()
    try:
        icons = _product_notify_icons(db)
        hrs = _sale_notify_hours(hours)
        was = f"{float(old_price):.2f}"
        now = f"{float(new_price):.2f}"
        return (
            f"{icons['sale']} <b>Flash Sale - Limited Time Offer</b>\n\n"
            f"{_product_title_html(service, db)}\n"
            f"{icons['was']} Was: <s>{was} USDT</s>\n"
            f"{icons['now']} Now: {now} USDT\n"
            f"{icons['hurry']} <b>Hurry End in {hrs} Hours</b>"
        )
    finally:
        if own_session:
            db.close()


async def notify_product_sale(
    service,
    old_price: float,
    new_price: float,
    hours: int | None = None,
) -> int:
    """Broadcast sale to users + Notify Group + Notify Channel (flash/price-drop)."""
    from utils.helpers import icon_button
    from utils.ui_icons import build_ui_icons

    db = SessionLocal()
    try:
        text = build_sale_notify_html(service, old_price, new_price, hours, db=db)
        user_markup = InlineKeyboardMarkup(
            inline_keyboard=[[_buy_now_button(service, db)]]
        )
        buy_icon = build_ui_icons(db).get("buy")
    finally:
        db.close()

    sent = await broadcast_to_all_users(text, reply_markup=user_markup, parse_mode="HTML")

    # Group/channel posts cannot use callback buttons — open the bot via t.me link.
    link_markup = None
    token = _bot_token()
    if token:
        bot = Bot(token=token)
        try:
            me = await bot.get_me()
            bot_username = (me.username or "").strip()
            if bot_username:
                bot_url = f"https://t.me/{bot_username}?start=products"
                link_markup = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            icon_button(
                                "Buy now",
                                icon_value=buy_icon,
                                icon_fallback="🛒",
                                url=bot_url,
                            )
                        ]
                    ]
                )
        except TelegramAPIError as exc:
            logger.warning("Sale link button skipped (getMe failed): %s", exc)
        finally:
            await bot.session.close()

    group_ok = await post_to_notify_group(text, reply_markup=link_markup, parse_mode="HTML")
    channel_ok = await post_to_notify_channel(text, reply_markup=link_markup, parse_mode="HTML")
    logger.info(
        "Sale notification users_sent=%s group=%s channel=%s",
        sent,
        group_ok,
        channel_ok,
    )
    return sent


async def post_to_notify_channel(
    text: str,
    *,
    db=None,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = "HTML",
    photo_path: str | None = None,
) -> bool:
    """Post announcements / flash-sale / maintenance to Settings → Notify Channel."""
    chat_id = _notify_channel_id(db)
    token = _bot_token()
    if not chat_id or not token:
        if not chat_id:
            logger.info(
                "Channel post skipped: no channel_notify_chat_id / force_join_channel / tg_channel_url"
            )
        return False

    bot = Bot(token=token)
    try:
        if photo_path:
            from aiogram.types import FSInputFile

            await bot.send_photo(
                chat_id=chat_id,
                photo=FSInputFile(photo_path),
                caption=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
        return True
    except TelegramAPIError as exc:
        logger.warning("Failed to post notification to channel %s: %s", chat_id, exc)
        return False
    finally:
        await bot.session.close()


async def post_to_notify_group(
    text: str,
    *,
    db=None,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = "HTML",
    photo_path: str | None = None,
) -> bool:
    """Post all shop alerts to Settings → Notify Group (premium-friendly)."""
    chat_id = _notify_group_id(db)
    token = _bot_token()
    if not chat_id or not token:
        if not chat_id:
            logger.info(
                "Group post skipped: no orders_notify_chat_id / force_join_group"
            )
        return False

    bot = Bot(token=token)
    try:
        if photo_path:
            from aiogram.types import FSInputFile

            await bot.send_photo(
                chat_id=chat_id,
                photo=FSInputFile(photo_path),
                caption=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
        return True
    except TelegramAPIError as exc:
        logger.warning("Failed to post notification to group %s: %s", chat_id, exc)
        return False
    finally:
        await bot.session.close()


async def notify_price_drop(service, old_price: float, new_price: float, *, sale_label: str = "Price Drop") -> int:
    """Alias — all sale types use the same Flash Sale Limited Time template."""
    _ = sale_label
    return await notify_product_sale(service, old_price, new_price, hours=24)


async def notify_flash_sale(service, old_price: float, new_price: float, hours: int) -> int:
    """Alias — flash sales use the shared sale template."""
    return await notify_product_sale(service, old_price, new_price, hours=hours)


def _utf16_len(text: str) -> int:
    """Telegram entity offsets use UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def _resolve_icon_parts(
    db,
    value: str | None,
    preset_names: tuple[str, ...],
    fallback: str,
    *,
    upgrade_plain_to_preset: bool = True,
) -> tuple[str, str | None]:
    """Return (display_emoji, custom_emoji_id_or_None) for message entities / HTML."""
    from utils.helpers import parse_icon
    from utils.stock_display import preset_icon_value

    raw = (value or "").strip()
    emoji_id, display = parse_icon(raw, fallback)
    if emoji_id:
        return display, emoji_id

    if upgrade_plain_to_preset or not raw:
        preset_val = preset_icon_value(db, preset_names, "")
        if preset_val:
            preset_id, preset_display = parse_icon(preset_val, fallback)
            if preset_id:
                return preset_display, preset_id
            if not raw:
                return preset_display, None

    return (display if raw else fallback), None


def _resolve_icon_html(
    db,
    value: str | None,
    preset_names: tuple[str, ...],
    fallback: str,
    *,
    upgrade_plain_to_preset: bool = True,
) -> str:
    """Premium icon for HTML messages: field value, else Icon Preset, else fallback."""
    from utils.helpers import render_icon

    display, emoji_id = _resolve_icon_parts(
        db,
        value,
        preset_names,
        fallback,
        upgrade_plain_to_preset=upgrade_plain_to_preset,
    )
    if emoji_id:
        return render_icon(f"{emoji_id}|{display}", fallback, html_mode=True)
    return display


def _product_title_html(service, db=None) -> str:
    """Product line: premium product icon + name (keeps embedded <tg-emoji> in name)."""
    from utils.helpers import render_rich_html

    name_html = render_rich_html(service.name)
    if "<tg-emoji" in (service.name or ""):
        return name_html

    emoji_val = (getattr(service, "emoji", None) or "").strip()
    # Always show an icon: product emoji if set, else Buy/Shop/Product preset.
    icon_html = _resolve_icon_html(
        db,
        emoji_val or None,
        ("buy", "shop", "product"),
        "🛍️",
        upgrade_plain_to_preset=False,
    )
    return f"{icon_html} {name_html}"


def _product_title_parts(service, db=None) -> tuple[str, list[tuple[int, int, str]]]:
    """Plain product title + list of (utf16_offset, utf16_length, custom_emoji_id)."""
    from utils.helpers import strip_html_tags

    name_plain = strip_html_tags(getattr(service, "name", None) or "").strip() or "Product"
    entities: list[tuple[int, int, str]] = []
    raw_name = getattr(service, "name", None) or ""

    # Name already embeds a premium tg-emoji — use that as the product icon.
    match = re.search(r'<tg-emoji emoji-id="(\d+)">(.*?)</tg-emoji>', raw_name, re.DOTALL)
    if match:
        display = (match.group(2) or "🛍️").strip() or "🛍️"
        emoji_id = match.group(1)
        rest = strip_html_tags(raw_name).replace(display, "", 1).strip() or name_plain
        # Prefer full stripped name (without duplicating the icon glyph if it was only in the tag).
        rest = name_plain
        if rest.startswith(display):
            rest = rest[len(display) :].strip()
        text = f"{display} {rest}".strip() if rest else display
        entities.append((0, _utf16_len(display), emoji_id))
        return text, entities

    emoji_val = (getattr(service, "emoji", None) or "").strip()
    display, emoji_id = _resolve_icon_parts(
        db,
        emoji_val or None,
        ("buy", "shop", "product"),
        "🛍️",
        # Empty emoji → upgrade from Buy/Shop presets; plain unicode stays as set.
        upgrade_plain_to_preset=not bool(emoji_val),
    )
    text = f"{display} {name_plain}".strip()
    if emoji_id:
        entities.append((0, _utf16_len(display), emoji_id))
    return text, entities


def _buy_now_button(service, db):
    from utils.helpers import icon_button
    from utils.ui_icons import build_ui_icons

    icons = build_ui_icons(db)
    return icon_button(
        "Buy now",
        icon_value=icons.get("buy"),
        icon_fallback="🛒",
        callback_data=f"svc:{service.id}",
    )


async def _channel_open_shop_markup(db=None) -> InlineKeyboardMarkup | None:
    """Channel posts cannot use callback buttons — link opens the bot instead."""
    from utils.helpers import icon_button
    from utils.ui_icons import build_ui_icons

    token = _bot_token()
    if not token:
        return None
    buy_icon = build_ui_icons(db).get("buy") if db is not None else None
    bot = Bot(token=token)
    try:
        me = await bot.get_me()
        bot_username = (me.username or "").strip()
        if not bot_username:
            return None
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    icon_button(
                        "Buy now",
                        icon_value=buy_icon,
                        icon_fallback="🛒",
                        url=f"https://t.me/{bot_username}",
                    )
                ]
            ]
        )
    except TelegramAPIError as exc:
        logger.warning("Channel Buy now button skipped (getMe failed): %s", exc)
        return None
    finally:
        await bot.session.close()


async def notify_new_product(service) -> int:
    """Naya product create hone par sab users ko broadcast + notify channel."""
    from utils.stock_display import effective_available_qty

    available = effective_available_qty(service)
    db = SessionLocal()
    try:
        icons = _product_notify_icons(db)
        text = (
            f"{icons['new']} New product added!\n\n"
            f"{_product_title_html(service, db)}\n"
            f"{icons['added']} Added: {available}\n"
            f"{icons['stock']} Current stock: {available}\n"
            f"{icons['price']} Price: ${service.sell_price:.2f}"
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[_buy_now_button(service, db)]]
        )
        channel_markup = await _channel_open_shop_markup(db)
    finally:
        db.close()
    sent = await broadcast_to_all_users(text, reply_markup=markup, parse_mode="HTML")
    posted = await post_to_notify_group(text, reply_markup=channel_markup, parse_mode="HTML")
    if posted:
        logger.info("New product posted to notify group (users_sent=%s)", sent)
    return sent


async def notify_stock_added(service, quantity_added: int, *, fake_notify: bool = False) -> int:
    """Broadcast "Stock updated!" to all users + notify channel.

    fake_notify=True (Set Stock): inventory is NOT changed — message still shows
    Added + a display current stock (real available + fake added) so API products
    can announce restocks without writing the stock table.
    """
    from utils.stock_display import effective_available_qty

    available = effective_available_qty(service)
    display_current = available + int(quantity_added) if fake_notify else available
    db = SessionLocal()
    try:
        icons = _product_notify_icons(db)
        text = (
            f"{icons['update']} Stock updated!\n\n"
            f"{_product_title_html(service, db)}\n"
            f"{icons['added']} Added: {quantity_added}\n"
            f"{icons['stock']} Current stock: {display_current}\n"
            f"{icons['price']} Price: ${service.sell_price:.2f}"
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[_buy_now_button(service, db)]]
        )
        channel_markup = await _channel_open_shop_markup(db)
    finally:
        db.close()
    sent = await broadcast_to_all_users(text, reply_markup=markup, parse_mode="HTML")
    posted = await post_to_notify_group(text, reply_markup=channel_markup, parse_mode="HTML")
    if posted:
        logger.info("Stock update posted to notify group (users_sent=%s)", sent)
    return sent


def _normalize_channel_chat_id(value: str | None) -> str | None:
    """Accept @username, public t.me links, or numeric -100… chat IDs.

    Private invite links (t.me/+… / joinchat) cannot be used as chat_id — return None.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    # Private invite — cannot resolve without already being a member + numeric id.
    if re.search(r"(?:t\.me/|telegram\.me/)(\+|joinchat/)", raw, re.IGNORECASE):
        return None
    match = re.search(r"(?:t\.me/|telegram\.me/)([A-Za-z0-9_]+)", raw, re.IGNORECASE)
    if match:
        slug = match.group(1)
        if slug.lower() in {"joinchat", "addstickers", "share", "proxy"}:
            return None
        return f"@{slug}"
    if raw.startswith("@"):
        return raw
    if re.fullmatch(r"-?\d+", raw):
        return raw
    if re.fullmatch(r"[A-Za-z0-9_]{4,}", raw):
        return f"@{raw}"
    return raw


def _is_private_invite_link(value: str | None) -> bool:
    raw = (value or "").strip()
    return bool(re.search(r"(?:t\.me/|telegram\.me/)(\+|joinchat/)", raw, re.IGNORECASE))


async def resolve_telegram_chat_ref(
    value: str | None,
    *,
    bot: Bot | None = None,
    expect_types: tuple[str, ...] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve Settings value → numeric chat id via getChat.

    Returns (chat_id, error_message). chat_id is a string like "-100123…".
    """
    raw = (value or "").strip()
    if not raw:
        return None, None
    if _is_private_invite_link(raw):
        return None, (
            "Private invite link (t.me/+) se ID nahi milti. "
            "Group mein bot add karo → @useridsbot ya @RawDataBot se -100… id copy karo."
        )
    normalized = _normalize_channel_chat_id(raw)
    if not normalized:
        return None, f"Invalid chat value: {raw[:80]}"

    # Already numeric — still verify via getChat when bot available.
    owns_bot = bot is None
    token = _bot_token()
    if not token and owns_bot:
        # Can't resolve @username without bot; keep normalized as-is for numeric only.
        if re.fullmatch(r"-?\d+", normalized):
            return normalized, None
        return normalized, (
            "BOT_TOKEN missing — @username / link resolve nahi hui. "
            "Numeric -100… id paste karo."
        )

    if owns_bot:
        bot = Bot(token=token)
    try:
        chat = await bot.get_chat(normalized)
        chat_id = str(chat.id)
        type_val = str(getattr(getattr(chat, "type", None), "value", getattr(chat, "type", "")) or "")
        if expect_types and type_val and type_val not in expect_types:
            nice = "/".join(expect_types)
            return None, (
                f"Yeh chat type '{type_val}' hai — yahan {nice} chahiye. "
                f"(Telegram id thi {chat_id}, save nahi ki.)"
            )
        return chat_id, None
    except TelegramAPIError as exc:
        return None, (
            f"Telegram chat resolve fail ({normalized}): {exc}. "
            "Bot ko us group/channel mein Admin banao, phir Save dubara dabao. "
            "Ya @userinfobot / @RawDataBot se -100… id paste karo."
        )
    finally:
        if owns_bot and bot is not None:
            await bot.session.close()


def _notify_channel_id(db=None) -> str | None:
    """CHANNEL for announcements / flash-sale / maintenance only (no purchases)."""
    own_session = db is None
    db = db or SessionLocal()
    try:
        config = db.query(BotConfig).first()
        for candidate in (
            (getattr(config, "channel_notify_chat_id", None) if config else None),
            (getattr(config, "force_join_channel", None) if config else None),
            (config.tg_channel_url if config else None),
            os.getenv("CHANNEL_NOTIFY_CHAT_ID"),
        ):
            chat_id = _normalize_channel_chat_id(candidate)
            if chat_id:
                return chat_id
        return None
    finally:
        if own_session:
            db.close()


def _notify_group_id(db=None) -> str | None:
    """GROUP for all alerts: buy / stock / product / sale / maintenance (premium)."""
    own_session = db is None
    db = db or SessionLocal()
    try:
        config = db.query(BotConfig).first()
        for candidate in (
            (getattr(config, "orders_notify_chat_id", None) if config else None),
            (getattr(config, "force_join_group", None) if config else None),
            os.getenv("ORDERS_NOTIFY_CHAT_ID"),
            os.getenv("GROUP_NOTIFY_CHAT_ID"),
        ):
            chat_id = _normalize_channel_chat_id(candidate)
            if chat_id:
                return chat_id
        return None
    finally:
        if own_session:
            db.close()


# Back-compat aliases
def _sales_notify_channel_id(db=None) -> str | None:
    return _notify_channel_id(db)


def _buy_relay_group_id(db=None) -> str | None:
    return _notify_group_id(db)


def _orders_notify_chat_id(db=None) -> str | None:
    return _notify_group_id(db) or _notify_channel_id(db)


async def _custom_emoji_alt_map(bot: Bot, emoji_ids: list[str]) -> dict[str, str]:
    """Map custom_emoji_id → official sticker alt glyph (required by Telegram)."""
    ids = [str(eid) for eid in emoji_ids if eid]
    if not ids:
        return {}
    try:
        stickers = await bot.get_custom_emoji_stickers(custom_emoji_ids=ids)
    except Exception as exc:  # noqa: BLE001
        logger.debug("getCustomEmojiStickers failed: %s", exc)
        return {}
    out: dict[str, str] = {}
    for sticker in stickers or []:
        cid = str(getattr(sticker, "custom_emoji_id", "") or "")
        alt = (getattr(sticker, "emoji", None) or "").strip()
        if cid and alt:
            out[cid] = alt
    return out


def _apply_custom_emoji_alts(
    text: str,
    entities: list[tuple[int, int, str]],
    alt_map: dict[str, str],
) -> tuple[str, list[tuple[int, int, str]]]:
    """Replace entity glyphs with Telegram alts and recompute UTF-16 offsets."""
    if not entities:
        return text, []
    encoded = text.encode("utf-16-le")
    parts: list[str] = []
    new_entities: list[tuple[int, int, str]] = []
    cursor = 0
    for offset, length, emoji_id in sorted(entities, key=lambda item: item[0]):
        parts.append(encoded[cursor * 2 : offset * 2].decode("utf-16-le"))
        old_glyph = encoded[offset * 2 : (offset + length) * 2].decode("utf-16-le")
        alt = alt_map.get(str(emoji_id), old_glyph) or old_glyph
        start = sum(_utf16_len(part) for part in parts)
        parts.append(alt)
        new_entities.append((start, _utf16_len(alt), emoji_id))
        cursor = offset + length
    parts.append(encoded[cursor * 2 :].decode("utf-16-le"))
    return "".join(parts), new_entities


async def _chat_type_value(bot: Bot, chat_id: str | int) -> str:
    try:
        chat = await bot.get_chat(chat_id)
        chat_type = getattr(chat, "type", None)
        return str(getattr(chat_type, "value", chat_type) or "").lower()
    except TelegramAPIError as exc:
        logger.debug("getChat type failed for %s: %s", chat_id, exc)
        return ""


async def notify_channel_order_completed(order, service=None, db=None) -> bool:
    """Classic buy notify to Notify Group only (never the channel):

    SMF SHOP          ← clickable t.me link (opens /products)
    🛍 Someone just bought 1x 🖤 CapCut Pro Team 1M!
    [ Open SMF SHOP ]
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
    from utils.menu_commands import get_command_map

    own_session = db is None
    db = db or SessionLocal()
    try:
        group_id = _notify_group_id(db)
        if not group_id:
            logger.warning("Buy notify skipped: no Notify Group configured")
            return False

        service = service or getattr(order, "service", None)
        if service is None and getattr(order, "service_id", None):
            from database.models import Service

            service = db.get(Service, order.service_id)
        if not service:
            return False

        shop_cmd = get_command_map(db).get("shop")
        shop_display, shop_emoji_id = _resolve_icon_parts(
            db,
            shop_cmd.icon if shop_cmd else None,
            ("shop", "buy"),
            "🛍",
            upgrade_plain_to_preset=True,
        )
        product_text, product_entities = _product_title_parts(service, db)
        qty = int(getattr(order, "quantity", 1) or 1)

        shop_icon_html = _resolve_icon_html(
            db,
            shop_cmd.icon if shop_cmd else None,
            ("shop", "buy"),
            "🛍",
            upgrade_plain_to_preset=True,
        )
        product_html = _product_title_html(service, db)
        html_buy = f"{shop_icon_html} Someone just bought {qty}x {product_html}!"
    finally:
        if own_session:
            db.close()

    token = _bot_token()
    if not token:
        logger.warning("Buy notify skipped: BOT_TOKEN missing")
        return False

    bot = Bot(token=token)
    chat_id = group_id
    try:
        resolved, err = await resolve_telegram_chat_ref(
            group_id, bot=bot, expect_types=("group", "supergroup")
        )
        if err:
            logger.warning("Buy notify group resolve: %s", err)
        if resolved:
            chat_id = resolved
        elif _is_private_invite_link(group_id):
            logger.warning("Buy notify group is a private invite link — need -100… id")
            return False

        emoji_ids: list[str] = []
        if shop_emoji_id:
            emoji_ids.append(shop_emoji_id)
        emoji_ids.extend(eid for _, _, eid in product_entities)
        alt_map = await _custom_emoji_alt_map(bot, emoji_ids)

        if shop_emoji_id and shop_emoji_id in alt_map:
            shop_display = alt_map[shop_emoji_id]
        product_text, product_entities = _apply_custom_emoji_alts(
            product_text, product_entities, alt_map
        )

        buy_prefix = f"{shop_display} Someone just bought {qty}x "
        buy_body = f"{buy_prefix}{product_text}!"
        buy_entities: list[tuple[int, int, str]] = []
        if shop_emoji_id:
            buy_entities.append((0, _utf16_len(shop_display), shop_emoji_id))
        buy_prefix_len = _utf16_len(buy_prefix)
        for offset, length, emoji_id in product_entities:
            buy_entities.append((buy_prefix_len + offset, length, emoji_id))

        if not buy_entities:
            logger.warning(
                "Buy notify has no custom_emoji entities — set Commands → Shop "
                "premium icon and/or product premium emoji (Icon Presets)."
            )

        me = await bot.get_me()
        bot_username = (me.username or "").strip()
        bot_label = (me.first_name or "").strip() or bot_username or "SMF SHOP"
        bot_url = f"https://t.me/{bot_username}?start=products" if bot_username else None

        if bot_url:
            lead = f"{bot_label}\n"
            text = f"{lead}{buy_body}"
            entities: list[MessageEntity] = [
                MessageEntity(
                    type="text_link",
                    offset=0,
                    length=_utf16_len(bot_label),
                    url=bot_url,
                )
            ]
            lead_len = _utf16_len(lead)
            for offset, length, emoji_id in buy_entities:
                entities.append(
                    MessageEntity(
                        type="custom_emoji",
                        offset=lead_len + offset,
                        length=length,
                        custom_emoji_id=emoji_id,
                    )
                )
            html_text = (
                f'<a href="{html.escape(bot_url)}"><b>{html.escape(bot_label)}</b></a>\n'
                f"{html_buy}"
            )
            reply_markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=f"Open {bot_label}", url=bot_url)]
                ]
            )
        else:
            text = buy_body
            entities = [
                MessageEntity(
                    type="custom_emoji",
                    offset=offset,
                    length=length,
                    custom_emoji_id=emoji_id,
                )
                for offset, length, emoji_id in buy_entities
            ]
            html_text = html_buy
            reply_markup = None

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                entities=entities or None,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except TelegramAPIError as exc:
            logger.warning(
                "Entity send failed for buy notify to %s (%s); retrying HTML",
                chat_id,
                exc,
            )
            await bot.send_message(
                chat_id=chat_id,
                text=html_text,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        return True
    except TelegramAPIError as exc:
        logger.warning("Failed to post buy notification to group %s: %s", chat_id, exc)
        return False
    finally:
        await bot.session.close()
