"""Product detail screen formatting for the Telegram shop."""

from __future__ import annotations

import os

from sqlalchemy import func
from sqlalchemy.orm import Session

from database.models import Order
from utils.helpers import format_description_block, render_icon, render_rich_html
from utils.stock_display import preset_icon

# Telegram text messages allow 4096 chars; keep a safe margin for HTML tags.
_TEXT_SAFE_LIMIT = 3900


def product_image_file(service):
    """Return FSInputFile for a product logo when the file exists on disk."""
    from aiogram.types import FSInputFile

    path = (getattr(service, "image_path", None) or "").strip()
    if not path:
        return None
    local_path = path.lstrip("/")
    if not os.path.isfile(local_path):
        return None
    return FSInputFile(local_path)


def service_sold_units(db: Session, service_id: int) -> int:
    """Total units sold (completed orders) for this product."""
    return int(
        db.query(func.coalesce(func.sum(Order.quantity), 0))
        .filter(Order.service_id == service_id, Order.status == "completed")
        .scalar()
        or 0
    )


def _product_icons(db: Session | None) -> dict[str, str]:
    return {
        "price": preset_icon(db, ("price",), "💵"),
        "stock": preset_icon(db, ("stock", "added"), "➕"),
        "sold": preset_icon(db, ("sold",), "📈"),
        "description": preset_icon(db, ("description",), "💎"),
        "quantity": preset_icon(db, ("quantity", "enterquantity", "enter quantity", "enter_quantity"), "🖍️"),
        "out": preset_icon(db, ("stockout", "outofstock"), "🔴"),
    }


def _product_title_html(service) -> str:
    name = render_rich_html(service.name)
    if "<tg-emoji" in (service.name or ""):
        return name
    icon = render_icon(getattr(service, "emoji", None), "🛒", html_mode=True)
    return f"{icon} {name}"


def build_product_out_of_stock_text(service, db: Session | None = None) -> str:
    """Out-of-stock products show a short message only."""
    icons = _product_icons(db)
    return f"{_product_title_html(service)}\n\n{icons['out']} <b>Out of stock</b>"


def build_product_in_stock_parts(
    service,
    *,
    available: int,
    sold: int,
    db: Session | None = None,
    unit_price: float | None = None,
    list_price: float | None = None,
    personal_discount: bool = False,
) -> tuple[str, str]:
    """One HTML card (details + description box) + separate quantity prompt."""
    icons = _product_icons(db)
    base = float(list_price if list_price is not None else (getattr(service, "sell_price", 0) or 0))
    price = float(unit_price if unit_price is not None else base)
    stock_label = "account" if available == 1 else "accounts"
    sold_label = "account" if sold == 1 else "accounts"

    if personal_discount and abs(price - base) > 1e-9:
        price_line = (
            f"{icons['price']} <b>Price:</b> <s>${base:.2f}</s> → "
            f"<b>${price:.2f}</b> <i>(Special Price)</i>"
        )
    else:
        price_line = f"{icons['price']} <b>Price:</b> ${price:.2f}"

    header = "\n".join(
        [
            _product_title_html(service),
            price_line,
            f"{icons['stock']} <b>Stock:</b> {available} {stock_label}",
            f"{icons['sold']} <b>Sold:</b> {sold} {sold_label}",
            "",
        ]
    )
    # Box style only — no extra "Description:" header (content is the template body).
    description = format_description_block(getattr(service, "description", None))
    desc_limit = max(_TEXT_SAFE_LIMIT - len(header), 200)
    if len(description) > desc_limit:
        # Truncate inside the box body only — never mid closing tag.
        open_tag = "<blockquote>"
        close_tag = "</blockquote>"
        inner = description[len(open_tag) : -len(close_tag)] if description.endswith(close_tag) else description
        keep = max(desc_limit - len(open_tag) - len(close_tag) - 1, 200)
        description = f"{open_tag}{inner[:keep]}…{close_tag}"

    card = f"{header}{description}"

    min_buy = int(getattr(service, "min_qty", 1) or 1)
    max_buy = min(int(getattr(service, "max_qty", 1) or 1), available)
    if max_buy < min_buy:
        max_buy = min_buy
    qty_prompt = f"{icons['quantity']} Enter quantity to buy ({min_buy}-{max_buy}):"

    return card, qty_prompt


def build_product_in_stock_text(
    service,
    *,
    available: int,
    sold: int,
    db: Session | None = None,
    unit_price: float | None = None,
    list_price: float | None = None,
    personal_discount: bool = False,
) -> tuple[str, str]:
    """Alias — single product card + quantity prompt."""
    return build_product_in_stock_parts(
        service,
        available=available,
        sold=sold,
        db=db,
        unit_price=unit_price,
        list_price=list_price,
        personal_discount=personal_discount,
    )
