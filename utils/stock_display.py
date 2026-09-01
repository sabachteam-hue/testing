"""Stock display + API sync rules for the Telegram shop UI.

Catalog UI (categories + products):
  - In stock  (>0) → green button  (Telegram style=success)
  - Out of stock (0) → red button   (style=danger)

Icon Presets (name match is case-insensitive):
  - StockIn / StockOut — catalog stock legend
  - New / Update / Added / Stock / Price — product + stock notifications
  - Buy / PKR / Pay / Refresh / Back / Orders — wallet + buy-now UI
  - API Link / Active / Disabled / USD / Key / New / View — reseller API panel
  - Contact / Admin / WhatsApp / Telegram — support screen
  - Sold / Description / Quantity — product detail screen (in stock)
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy.orm import Session

StockStyle = Literal["success", "danger"]

STOCK_LEGEND = "🟢 In Stock  ·  🔴 Out of stock"


def login_detail_lines(stock) -> list[str]:
    if not stock or not getattr(stock, "login_details", None):
        return []
    return [line for line in stock.login_details.splitlines() if line.strip()]


def has_manual_login_stock(stock) -> bool:
    """True when admin saved login/account lines with the stock — protect from API wipe."""
    return bool(login_detail_lines(stock))


def effective_available_qty(service_or_stock) -> int:
    """Buyable units shown in the bot.

    Prefer Stock on a Service; if login lines exist OR fulfillment is 'stock',
    available is capped by remaining login lines (0 lines ⇒ 0 for stock delivery).
    """
    stock = getattr(service_or_stock, "stock", service_or_stock)
    if stock is None or not hasattr(stock, "available_qty"):
        return 0
    if getattr(stock, "is_unlimited", False):
        return 1_000_000_000
    available = int(stock.available_qty or 0)
    lines = login_detail_lines(stock)
    fulfillment = getattr(service_or_stock, "fulfillment_type", None)
    # Auto-from-stock products: login lines are the real inventory. Empty list = sold out,
    # even if quantity was left stale after accounts were consumed.
    if fulfillment == "stock":
        return max(min(available, len(lines)), 0)
    if lines:
        # Reserved units are already subtracted in available_qty; cap by remaining lines.
        return max(min(available, len(lines)), 0)
    return max(available, 0)


def align_quantity_to_login_lines(stock) -> None:
    """Keep quantity consistent with remaining login accounts + reserved holds."""
    if stock is None:
        return
    if getattr(stock, "stock_type", "account") == "quantity":
        return
    lines = len(login_detail_lines(stock))
    reserved = max(int(stock.reserved_qty or 0), 0)
    stock.quantity = max(lines + reserved, reserved)
    stock.reserved_qty = min(reserved, stock.quantity)


def stock_status(available: int) -> str:
    """Binary availability for catalog UI: 'in' or 'out'."""
    return "out" if available <= 0 else "in"


def stock_button_style(available: int) -> StockStyle:
    return "danger" if available <= 0 else "success"


def stock_dot(available: int) -> str:
    return "🔴" if available <= 0 else "🟢"


def stock_label(available: int) -> str:
    if available <= 0:
        return f"{stock_dot(0)} Out of stock"
    return f"{stock_dot(available)} In Stock"


def _load_icon_presets(db: Session | None) -> dict[str, str]:
    if db is None:
        return {}
    try:
        from database.models import IconPreset

        return {
            (row.name or "").strip().lower(): row.combined_value
            for row in db.query(IconPreset).all()
            if row.name
        }
    except Exception:  # noqa: BLE001
        return {}


def preset_icon_value(db: Session | None, names: tuple[str, ...], fallback: str) -> str:
    """Resolve Icon Preset → ID|fallback (for buttons) or plain fallback emoji."""
    presets = _load_icon_presets(db)
    for name in names:
        value = presets.get(name.lower())
        if value:
            return value
    return fallback


def preset_icon(db: Session | None, names: tuple[str, ...], fallback: str) -> str:
    """Resolve an Icon Preset by name → HTML-ready premium/unicode icon."""
    if db is None:
        return fallback
    try:
        from utils.helpers import render_icon

        presets = _load_icon_presets(db)
        for name in names:
            value = presets.get(name.lower())
            if value:
                return render_icon(value, fallback=fallback, html_mode=True)
    except Exception:  # noqa: BLE001
        return fallback
    return fallback


# Back-compat alias
_preset_icon = preset_icon


def build_stock_legend(db: Session | None = None) -> str:
    """Catalog caption legend — prefers premium StockIn/StockOut Icon Presets."""
    in_dot = preset_icon(db, ("stockin", "instock", "stock_green", "green"), "🟢")
    out_dot = preset_icon(db, ("stockout", "outofstock", "stock_red", "red"), "🔴")
    return f"{in_dot} In Stock  ·  {out_dot} Out of stock"


def apply_provider_stock(stock, available_from_api: int, fulfillment_type: str | None = None) -> bool:
    """Write provider stock onto a Stock row.

    Returns True if quantity was updated from the API, False if skipped because
    the row is protected by login_details.

    For 'auto' (API delivery) products, reserved_qty is reset to 0 so a stale/stuck
    reservation from a past order can never cap the synced stock down to 0 while the
    provider actually has stock. 'stock' / 'manual' products keep the previous
    reserved-qty-aware behavior untouched.
    """
    available_from_api = max(int(available_from_api or 0), 0)
    if has_manual_login_stock(stock):
        # Keep local inventory; align quantity to remaining login lines + reserved.
        align_quantity_to_login_lines(stock)
        return False

    stock.quantity = available_from_api
    if fulfillment_type == "auto":
        stock.reserved_qty = 0
    else:
        stock.reserved_qty = min(int(stock.reserved_qty or 0), available_from_api)
    return True


def category_available_qty(category) -> int:
    """Sum of effective available units across active, non-deleted products in a category."""
    total = 0
    for service in getattr(category, "services", None) or []:
        if getattr(service, "is_deleted", False):
            continue
        if not getattr(service, "is_active", True):
            continue
        total += effective_available_qty(service)
    return total
