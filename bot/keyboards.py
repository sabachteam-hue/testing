from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton

from utils.helpers import icon_button
from utils.menu_commands import CommandView, get_command_map
from utils.ui_icons import wallet_method_icon
from utils.stock_display import (
    category_available_qty,
    effective_available_qty,
    stock_button_style,
)

import re

_TG_EMOJI_TAG = re.compile(r"</?tg-emoji[^>]*>", re.IGNORECASE)


def _cmd(commands: dict[str, CommandView], key: str) -> CommandView:
    return commands.get(key) or CommandView(key, key.title(), key.title(), "✨", 0)


def _button_plain_name(name: str | None, max_len: int = 28) -> str:
    """Button text cannot include HTML; strip tg-emoji tags and trim length."""
    text = _TG_EMOJI_TAG.sub("", name or "")
    text = " ".join(text.split()).strip() or "Product"
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


def _plain(icon_value: str | None, fallback: str) -> str:
    """Reply keyboards can't render custom emoji IDs — use fallback side of ID|emoji."""
    if not icon_value:
        return fallback
    if "|" in icon_value:
        emoji_id, _, fb = icon_value.partition("|")
        return (fb.strip() or fallback) if emoji_id.strip().isdigit() else icon_value
    return icon_value


def _mini_app_url() -> str | None:
    from utils.helpers import get_mini_app_url

    return get_mini_app_url()


def main_menu_keyboard(
    commands: dict[str, CommandView] | None = None,
    *,
    show_admin: bool = False,
    mini_app_url: str | None = None,
) -> ReplyKeyboardMarkup:
    """Persistent bottom quick-menu (reply keyboard — no colored styles / premium
    icon_custom_emoji_id support; use inline start menu for those).
    """
    commands = commands or get_command_map(None)
    shop = _cmd(commands, "shop")
    catalog = _cmd(commands, "catalog")
    wallet = _cmd(commands, "wallet")
    orders = _cmd(commands, "orders")
    api = _cmd(commands, "api")
    support = _cmd(commands, "support")
    rows: list[list[KeyboardButton]] = []
    rows.extend(
        [
            [
                KeyboardButton(text=f"{_plain(catalog.icon, '🗂')} {catalog.reply_name}"),
                KeyboardButton(text=f"{_plain(shop.icon, '🛍')} {shop.reply_name}"),
            ],
            [
                KeyboardButton(text=f"{_plain(wallet.icon, '👛')} {wallet.reply_name}"),
                KeyboardButton(text=f"{_plain(orders.icon, '📦')} {orders.reply_name}"),
            ],
            [
                KeyboardButton(text=f"{_plain(api.icon, '🔗')} {api.reply_name}"),
                KeyboardButton(text=f"{_plain(support.icon, '💬')} {support.reply_name}"),
            ],
        ]
    )
    if show_admin:
        rows.append([KeyboardButton(text="🔐 Admin Panel")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
    )


def force_join_keyboard(
    *,
    channel_url: str | None = None,
    group_url: str | None = None,
    db=None,
) -> InlineKeyboardMarkup:
    """Membership Required buttons — Join Channel / Join Group + green Tick confirm."""
    from utils.stock_display import preset_icon_value

    announce_icon = preset_icon_value(
        db,
        ("announcement", "announce", "announcements", "speaker"),
        "📢",
    )
    group_icon = preset_icon_value(
        db,
        ("group", "members", "people", "community"),
        "👥",
    )
    tick_icon = preset_icon_value(
        db,
        ("tick", "check", "active", "success"),
        "✅",
    )
    rows: list[list[InlineKeyboardButton]] = []
    if channel_url:
        rows.append(
            [
                icon_button(
                    "Join Channel",
                    icon_value=announce_icon,
                    icon_fallback="📢",
                    url=channel_url,
                )
            ]
        )
    if group_url:
        rows.append(
            [
                icon_button(
                    "Join Group",
                    icon_value=group_icon,
                    icon_fallback="👥",
                    url=group_url,
                )
            ]
        )
    rows.append(
        [
            icon_button(
                "I have joined",
                icon_value=tick_icon,
                icon_fallback="✅",
                callback_data="forcejoin:check",
                style="success",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def start_menu_keyboard(
    commands: dict[str, CommandView] | None = None,
    *,
    show_admin: bool = False,
    premium: bool = True,
    mini_app_url: str | None = None,
) -> InlineKeyboardMarkup:
    """Inline menu under /start — Admin → Commands icons.

    premium=True: use custom emoji IDs from Commands (no colored `style`, which
    combined with bad IDs made Telegram reject the whole /start message).
    premium=False: plain unicode emoji text (safe fallback).
    Callers should try premium first, then retry with premium=False on error.
    """
    commands = commands or get_command_map(None)
    shop = _cmd(commands, "shop")
    topup = _cmd(commands, "topup")
    wallet = _cmd(commands, "wallet")
    settings = _cmd(commands, "settings")
    profile = _cmd(commands, "profile")
    support = _cmd(commands, "support")
    orders = _cmd(commands, "orders")
    refer = _cmd(commands, "refer")
    catalog = _cmd(commands, "catalog")
    api = _cmd(commands, "api")
    language = _cmd(commands, "language")
    url = mini_app_url if mini_app_url is not None else _mini_app_url()

    def _plain_btn(label: str, icon_value: str | None, fallback: str, callback_data: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=f"{_plain(icon_value, fallback)} {label}",
            callback_data=callback_data,
        )

    def _btn(label: str, icon_value: str | None, fallback: str, callback_data: str) -> InlineKeyboardButton:
        if not premium:
            return _plain_btn(label, icon_value, fallback, callback_data)
        # No style=… here — keeps premium icons without the crashy combo.
        return icon_button(
            label,
            icon_value=icon_value,
            icon_fallback=fallback,
            callback_data=callback_data,
        )

    rows: list[list[InlineKeyboardButton]] = []
    if url:
        # Use a normal https link, not web_app. Telegram rejects the whole
        # /start message when the Mini App domain is not allowed in BotFather,
        # which made /start and /menu go silent while /products still worked.
        rows.append(
            [InlineKeyboardButton(text="🛍 Open Mini Shop", url=url)]
        )
    rows.extend(
        [
            [_btn(shop.name, shop.icon, "🛍", "menu:shopall")],
            [
                _btn(catalog.name, catalog.icon, "🗂", "menu:catalog"),
                _btn(topup.name, topup.icon or wallet.icon, "💰", "menu:wallet"),
            ],
            [
                _btn(orders.name, orders.icon, "📦", "menu:orders"),
                _btn(refer.name, refer.icon, "⭐", "menu:refer"),
            ],
            [
                _btn(api.name, api.icon, "🔗", "menu:api"),
                _btn(support.name, support.icon, "💬", "menu:support"),
            ],
            [
                _btn(language.name, language.icon, "🌐", "menu:language"),
                _btn(settings.name, settings.icon or profile.icon, "🔑", "menu:profile"),
            ],
        ]
    )
    if show_admin:
        rows.append([_btn("Admin Panel", None, "🔐", "menu:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def language_keyboard(languages: list, current_code: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for language in languages:
        mark = "✅ " if language.code == current_code else ""
        row.append(
            InlineKeyboardButton(
                text=f"{mark}{language.flag} {language.name}",
                callback_data=f"lang:{language.code}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def products_keyboard(
    categories: list,
    per_row: int = 3,
    commands: dict[str, CommandView] | None = None,
    icons: dict | None = None,
) -> InlineKeyboardMarkup:
    """Category picker — button color follows aggregate stock in that category."""
    commands = commands or get_command_map(None)
    back = _cmd(commands, "back")
    back_icon = (icons or {}).get("back") or back.icon
    rows = []
    row = []
    for category in categories:
        available = category_available_qty(category)
        icon_value = getattr(category, "display_icon", None) or category.emoji
        style = stock_button_style(available)
        row.append(
            icon_button(
                _button_plain_name(getattr(category, "name", None), max_len=40),
                icon_value=icon_value,
                icon_fallback="📦",
                callback_data=f"cat:{category.id}",
                style=style,
            )
        )
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            icon_button(
                "Search",
                icon_value=(icons or {}).get("search") or "🔍",
                icon_fallback="🔍",
                callback_data="catalog:search",
            )
        ]
    )
    rows.append(
        [
            icon_button(
                back.name,
                icon_value=back_icon,
                icon_fallback="◀️",
                callback_data="menu:back",
                style="danger",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def services_keyboard(
    services: list,
    per_row: int = 1,
    commands: dict[str, CommandView] | None = None,
    icons: dict | None = None,
    prices: dict[int, float] | None = None,
) -> InlineKeyboardMarkup:
    """Product list — Aurex-style: Name | $price | 📦 stock (one button per row)."""
    commands = commands or get_command_map(None)
    refresh = _cmd(commands, "refresh")
    back = _cmd(commands, "back")
    refresh_icon = (icons or {}).get("refresh") or refresh.icon
    back_icon = (icons or {}).get("back") or back.icon
    rows = []
    row = []
    for service in services:
        available = effective_available_qty(service)
        style = stock_button_style(available)
        name = _button_plain_name(getattr(service, "name", None))
        if prices and int(service.id) in prices:
            price = float(prices[int(service.id)])
        else:
            price = float(getattr(service, "sell_price", 0) or 0)
        label = f"{name} | ${price:.2f} | 📦 {available}"
        if available <= 0:
            icon_value = None
            icon_fallback = "❌"
        else:
            icon_value = getattr(service, "emoji", None)
            icon_fallback = "🛍"
        row.append(
            icon_button(
                label,
                icon_value=icon_value,
                icon_fallback=icon_fallback,
                callback_data=f"svc:{service.id}",
                style=style,
            )
        )
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            icon_button(
                "Search",
                icon_value=(icons or {}).get("search") or "🔍",
                icon_fallback="🔍",
                callback_data="catalog:search",
            )
        ]
    )
    rows.append(
        [
            icon_button(
                refresh.name,
                icon_value=refresh_icon,
                icon_fallback="🔄",
                callback_data="menu:shopall",
                style="success",
            ),
            icon_button(
                back.name,
                icon_value=back_icon,
                icon_fallback="◀️",
                callback_data="menu:back",
                style="danger",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def order_payment_keyboard(total: float, methods: list) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"💰 Pay with Wallet (${total:.2f})",
                callback_data="orderpay:WALLET",
                style="success",
            )
        ]
    ]
    row = []
    for method in methods:
        row.append(
            icon_button(
                f"Pay with {method.name}",
                icon_value=method.icon,
                icon_fallback="💳",
                callback_data=f"orderpay:{method.code}",
                style="primary",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def orders_list_keyboard(orders: list) -> InlineKeyboardMarkup:
    rows = []
    for order in orders:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📦 {order.order_code} — {order.service.name}",
                    callback_data=f"orderview:{order.id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def post_order_actions_keyboard(
    order_id: int,
    commands: dict[str, CommandView] | None = None,
    icons: dict | None = None,
) -> InlineKeyboardMarkup:
    """Shown under a just-delivered order: My Orders / Report a problem / Main menu."""
    commands = commands or get_command_map(None)
    icons = icons or {}
    shop = _cmd(commands, "shop")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                icon_button(
                    "My Orders",
                    icon_value=icons.get("orders"),
                    icon_fallback="📦",
                    callback_data="menu:orders",
                )
            ],
            [
                icon_button(
                    "Report a problem",
                    icon_value=icons.get("report"),
                    icon_fallback="🛡",
                    callback_data=f"reportissue:{order_id}",
                )
            ],
            [
                icon_button(
                    "Main menu",
                    icon_value=shop.icon,
                    icon_fallback="🛍",
                    callback_data="menu:back",
                )
            ],
        ]
    )


def report_issue_keyboard(order_id: int, icons: dict | None = None) -> InlineKeyboardMarkup:
    """Shown on an order-history detail view: only Report a problem."""
    icons = icons or {}
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                icon_button(
                    "Report a problem",
                    icon_value=icons.get("report"),
                    icon_fallback="🛡",
                    callback_data=f"reportissue:{order_id}",
                )
            ],
        ]
    )


def wallet_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Top up / Deposit", callback_data="wallet:deposit", style="primary")],
            [InlineKeyboardButton(text="📜 Transaction History", callback_data="wallet:history")],
        ]
    )


def wallet_full_keyboard(
    methods: list,
    commands: dict[str, CommandView] | None = None,
    icons: dict | None = None,
) -> InlineKeyboardMarkup:
    commands = commands or get_command_map(None)
    refresh = _cmd(commands, "refresh")
    back = _cmd(commands, "back")
    orders = _cmd(commands, "orders")
    icons = icons or {}
    refresh_icon = icons.get("refresh") or refresh.icon
    back_icon = icons.get("back") or back.icon
    orders_icon = icons.get("orders") or orders.icon
    rows = []
    for method in methods:
        rows.append(
            [
                icon_button(
                    f"Top up with {method.name}",
                    icon_value=wallet_method_icon(method, icons),
                    icon_fallback="💳",
                    callback_data=f"wallet:topup:{method.code}",
                    style="primary",
                )
            ]
        )
    rows.append(
        [
            icon_button(
                "Transaction History",
                icon_value=orders_icon,
                icon_fallback="📜",
                callback_data="wallet:history",
            )
        ]
    )
    rows.append(
        [
            icon_button(
                "Refresh balance",
                icon_value=refresh_icon,
                icon_fallback="🔄",
                callback_data="wallet:refresh",
                style="success",
            ),
            icon_button(
                "Back to main menu",
                icon_value=back_icon,
                icon_fallback="◀️",
                callback_data="menu:back",
                style="danger",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wallet_deposit_methods_keyboard(methods: list) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for method in methods:
        row.append(
            icon_button(
                method.name,
                icon_value=method.icon,
                icon_fallback="💳",
                callback_data=f"wallet:topup:{method.code}",
                style="primary",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="📜 Transaction History", callback_data="wallet:history")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_quick_reply_menu(target, commands=None, *, show_admin: bool = False) -> None:
    """Re-attach the persistent bottom Quick menu (Catalog / Shop / Wallet…).

    Maintenance mode strips this with ReplyKeyboardRemove for clients only;
    admins never hit that path, so they keep seeing Quick while users lose it.
    Call this after /start, /menu, force-join unlock, and maintenance-off broadcast.
    """
    commands = commands or get_command_map(None)
    try:
        await target.answer(
            "Quick menu ready below 👇",
            reply_markup=main_menu_keyboard(commands, show_admin=show_admin),
        )
    except Exception:
        await target.answer("Quick menu ready below 👇")


async def answer_with_start_menu(target, welcome: str, commands, *, show_admin: bool = False) -> None:
    """Send start inline menu — never fail silent if Telegram rejects markup."""
    text = f"🚀 {welcome}\n\nPlease choose a menu:"
    attempts = (
        {"premium": True},
        {"premium": False},
        {"premium": False, "mini_app_url": ""},
    )
    last_error: Exception | None = None
    for kwargs in attempts:
        try:
            await target.answer(
                text,
                reply_markup=start_menu_keyboard(commands, show_admin=show_admin, **kwargs),
            )
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    try:
        await target.answer(text)
    except Exception:  # noqa: BLE001
        if last_error:
            raise last_error
