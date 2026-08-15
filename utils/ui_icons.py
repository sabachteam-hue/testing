"""Premium / 3D custom emoji helpers for menu buttons.

Prefer Admin → Commands for menu button names + icons.
Icon Presets remain a shared library for categories / products / payments,
and are still used as defaults when a Commands row copies a matching preset name.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from utils.menu_commands import menu_icons
from utils.stock_display import preset_icon, preset_icon_value

# Back-compat re-exports — handlers historically imported these from ui_icons.
MENU_ICON_DEFAULTS: dict[str, str] = {
    "shop": "🛍",
    "catalog": "🗂",
    "wallet": "👛",
    "topup": "💰",
    "profile": "👤",
    "settings": "🔑",
    "orders": "📦",
    "api": "🔗",
    "support": "💬",
    "language": "🌐",
    "refer": "⭐",
    "refresh": "🔄",
    "back": "◀️",
    "channel": "📢",
}


def label_icons(db: Session | None = None) -> dict[str, str]:
    """HTML-ready Icon Presets for common bot message labels.

    Wherever the bot shows words like Price / Quantity / Referral / Order,
    use these so Admin → Icon Presets control the emoji everywhere.
    """
    from database.models import SessionLocal

    own = db is None
    db = db or SessionLocal()
    try:
        return {
            "order": preset_icon(db, ("order", "ordersummary", "order summary"), "🧾"),
            "orders": preset_icon(
                db,
                ("orders", "orderhistory", "orders history", "order_history", "purchasehistory"),
                "📦",
            ),
            "product": preset_icon(db, ("product", "shop", "buy"), "🛍"),
            "quantity": preset_icon(
                db,
                ("quantity", "enterquantity", "enter quantity", "enter_quantity", "qty"),
                "📦",
            ),
            "price": preset_icon(db, ("price",), "💵"),
            "total": preset_icon(db, ("total", "price", "pay"), "💵"),
            "note": preset_icon(db, ("note", "notes"), "📝"),
            "tick": preset_icon(db, ("tick", "check", "active", "success"), "✅"),
            "referral": preset_icon(
                db,
                ("referral", "refer", "referralebalance", "referralbalance", "referral balance"),
                "💎",
            ),
            "announce": preset_icon(
                db,
                ("announcement", "announce", "announcements", "speaker"),
                "📢",
            ),
            "email": preset_icon(db, ("email", "mail", "gmail"), "📧"),
            "wallet": preset_icon(
                db,
                ("yourwallet", "your wallet", "your_wallet", "wallet"),
                "👛",
            ),
            "member": preset_icon(db, ("member", "members", "membership", "user", "profile"), "👤"),
            "link": preset_icon(db, ("link", "referral", "api", "key"), "🔗"),
            "users": preset_icon(db, ("users", "members", "member", "referred"), "👥"),
            "star": preset_icon(db, ("refer", "referral", "star"), "⭐"),
            "party": preset_icon(db, ("referral", "new", "party", "celebrate", "active"), "🎉"),
            "status": preset_icon(db, ("status", "active", "tick"), "📌"),
            "time": preset_icon(db, ("time", "clock", "duration"), "🕒"),
            "added": preset_icon(db, ("added",), "➕"),
            "active": preset_icon(db, ("active", "tick", "check"), "✅"),
            "details": preset_icon(db, ("orders", "order", "view", "details"), "📋"),
            "report": preset_icon(
                db,
                ("report", "reportproblem", "report a problem", "reportissue", "report_issue", "issue", "warranty"),
                "🛡",
            ),
        }
    finally:
        if own:
            db.close()


def build_ui_icons(db: Session | None = None) -> dict[str, str]:
    """Named Icon Presets for wallet / navigation / API UI (ID|fallback or plain emoji)."""
    cmds = menu_icons(db) if db is not None else {}

    def pick(preset_names: tuple[str, ...], cmd_key: str, fallback: str) -> str:
        value = preset_icon_value(db, preset_names, "")
        return value or cmds.get(cmd_key, fallback)

    return {
        "refresh": pick(("refresh",), "refresh", "🔄"),
        "back": pick(("back",), "back", "◀️"),
        "orders": pick(("orders", "orderhistory", "orders history", "order_history"), "orders", "📜"),
        "buy": preset_icon_value(db, ("buy",), "🛒"),
        "pkr": preset_icon_value(db, ("pkr", "payfast"), "🟢"),
        "pay": preset_icon_value(db, ("pay",), "💳"),
        "price": preset_icon_value(db, ("price",), "💵"),
        "api": pick(("api", "apilink", "api link", "api_link"), "api", "🔗"),
        "active": preset_icon_value(db, ("active",), "✅"),
        "disabled": preset_icon_value(db, ("disabled",), "🛑"),
        "usd": preset_icon_value(db, ("usd",), "💸"),
        "key": preset_icon_value(db, ("key",), "🔑"),
        "new": preset_icon_value(db, ("new",), "🆕"),
        "view": preset_icon_value(db, ("view",), "📖"),
        "stock": preset_icon_value(db, ("stock",), "📦"),
        "contact": preset_icon_value(db, ("contact", "support"), "📞"),
        "admin": preset_icon_value(db, ("admin",), "👤"),
        "telegram": preset_icon_value(db, ("telegram", "tg"), "✈️"),
        "whatsapp": preset_icon_value(db, ("whatsapp", "wa"), "💬"),
        "wallet": preset_icon_value(db, ("yourwallet", "your wallet", "your_wallet", "wallet"), "👛"),
        "payment_methods": preset_icon_value(
            db,
            ("paymentmethod", "payment method", "paymentmethods", "payment_method", "pay"),
            "💳",
        ),
        "referral": preset_icon_value(
            db,
            ("referral", "refer", "referralebalance", "referralbalance"),
            "💎",
        ),
        "order": preset_icon_value(db, ("order", "ordersummary"), "🧾"),
        "product": preset_icon_value(db, ("product", "shop", "buy"), "🛍"),
        "quantity": preset_icon_value(
            db,
            ("quantity", "enterquantity", "enter quantity", "enter_quantity"),
            "📦",
        ),
        "note": preset_icon_value(db, ("note", "notes"), "📝"),
        "tick": preset_icon_value(db, ("tick", "check", "active", "success"), "✅"),
        "report": preset_icon_value(
            db,
            ("report", "reportproblem", "report a problem", "reportissue", "report_issue", "issue", "warranty"),
            "🛡",
        ),
    }


def wallet_method_icon(method, icons: dict[str, str]) -> str | None:
    """Top-up button icon — PayFast/PKR methods prefer the PKR preset."""
    if not method:
        return None
    code = (getattr(method, "code", None) or "").upper()
    name = (getattr(method, "name", None) or "").lower()
    if code == "PAYFAST" or "pkr" in name:
        return icons.get("pkr") or getattr(method, "icon", None)
    return getattr(method, "icon", None)


def wallet_screen_copy(db: Session | None, *, wallet_usdt: float, referral_wallet: float) -> tuple[str, str]:
    """Payment Methods title + Your wallet caption using Icon Presets."""
    from utils.helpers import format_usdt

    icons = label_icons(db)
    pay_icon = preset_icon(
        db,
        ("paymentmethod", "payment method", "paymentmethods", "payment_method", "pay"),
        "💳",
    )
    title = f"{pay_icon} Payment Methods"
    caption = (
        f"{icons['wallet']} Your wallet\n\n"
        f"USD/USDT: {format_usdt(wallet_usdt)}\n"
        f"{icons['referral']} Referral balance: {format_usdt(referral_wallet)}"
    )
    return title, caption


def menu_icon_value(db: Session | None, key: str) -> str:
    return menu_icons(db).get(key.lower(), MENU_ICON_DEFAULTS.get(key, "✨"))


__all__ = [
    "MENU_ICON_DEFAULTS",
    "build_ui_icons",
    "label_icons",
    "menu_icon_value",
    "menu_icons",
    "wallet_method_icon",
    "wallet_screen_copy",
]
