"""Merge duplicate catalog products into one active product.

Moves orders (sold history), stock logins/qty, discounts, and sales onto the
keep product, then soft-deletes the duplicate so it leaves the catalog.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from database.models import Order, ProductSale, Service, Stock, UserProductDiscount
from utils.stock_file_import import merge_login_lines


@dataclass
class MergeResult:
    keep_id: int
    merge_id: int
    orders_moved: int
    discounts_moved: int
    discounts_skipped: int
    sales_moved: int
    sales_deactivated: int
    stock_lines_merged: int
    qty_added: int
    reserved_added: int


def merge_products(db: Session, *, keep_id: int, merge_id: int) -> MergeResult:
    """Merge ``merge_id`` into ``keep_id``. Raises ValueError on bad input."""
    if keep_id == merge_id:
        raise ValueError("Choose two different products")

    keep = db.get(Service, keep_id)
    merge = db.get(Service, merge_id)
    if not keep:
        raise ValueError("Keep product not found")
    if not merge:
        raise ValueError("Merge (duplicate) product not found")
    if keep.is_deleted:
        raise ValueError("Keep product is deleted — restore/choose an active product to keep")
    if merge.is_deleted:
        raise ValueError("Duplicate product is already deleted/hidden")

    # ── Orders (sold history) ─────────────────────────────────────────────
    orders = db.query(Order).filter(Order.service_id == merge.id).all()
    for order in orders:
        order.service_id = keep.id
    orders_moved = len(orders)

    # ── User discounts ────────────────────────────────────────────────────
    discounts_moved = 0
    discounts_skipped = 0
    for disc in db.query(UserProductDiscount).filter(UserProductDiscount.service_id == merge.id).all():
        existing = (
            db.query(UserProductDiscount)
            .filter(
                UserProductDiscount.user_id == disc.user_id,
                UserProductDiscount.service_id == keep.id,
                UserProductDiscount.is_active.is_(True),
            )
            .first()
        )
        if existing:
            disc.is_active = False
            discounts_skipped += 1
        else:
            disc.service_id = keep.id
            discounts_moved += 1

    # ── Product sales / flash offers ──────────────────────────────────────
    sales_moved = 0
    sales_deactivated = 0
    keep_has_active_sale = (
        db.query(ProductSale)
        .filter(ProductSale.service_id == keep.id, ProductSale.is_active.is_(True))
        .first()
        is not None
    )
    for sale in db.query(ProductSale).filter(ProductSale.service_id == merge.id).all():
        if sale.is_active and keep_has_active_sale:
            sale.is_active = False
            sales_deactivated += 1
        sale.service_id = keep.id
        sales_moved += 1
        if sale.is_active:
            keep_has_active_sale = True

    # ── Stock / login inventory ───────────────────────────────────────────
    stock_lines_merged = 0
    qty_added = 0
    reserved_added = 0
    merge_stock = merge.stock
    keep_stock = keep.stock
    if merge_stock:
        if not keep_stock:
            keep_stock = Stock(service_id=keep.id, quantity=0, reserved_qty=0)
            db.add(keep_stock)
            db.flush()

        src_lines = [line.strip() for line in (merge_stock.login_details or "").splitlines() if line.strip()]
        if src_lines or (keep_stock.login_details or "").strip():
            merged_lines = merge_login_lines(keep_stock.login_details or "", merge_stock.login_details or "")
            # Dedupe while preserving order
            seen: set[str] = set()
            unique_lines: list[str] = []
            for line in merged_lines:
                key = line.lower()
                if key in seen:
                    continue
                seen.add(key)
                unique_lines.append(line)
            keep_stock.login_details = "\n".join(unique_lines) if unique_lines else None
            stock_lines_merged = len(src_lines)
            if unique_lines:
                # Login lines drive stock for stock-fulfillment products.
                keep_stock.quantity = len(unique_lines)
                keep_stock.reserved_qty = min(int(keep_stock.reserved_qty or 0) + reserved_from_merge, keep_stock.quantity)

        qty_added = max(int(merge_stock.quantity or 0), 0)
        reserved_added = max(int(merge_stock.reserved_qty or 0), 0)
        if not src_lines:
            # No login inventory — add numeric stock from the duplicate.
            keep_stock.quantity = max(int(keep_stock.quantity or 0), 0) + qty_added
            keep_stock.reserved_qty = max(int(keep_stock.reserved_qty or 0), 0) + reserved_added
        if merge_stock.notes and not keep_stock.notes:
            keep_stock.notes = merge_stock.notes
        elif merge_stock.notes and keep_stock.notes and merge_stock.notes.strip() not in keep_stock.notes:
            keep_stock.notes = f"{keep_stock.notes.rstrip()}\n---\n{merge_stock.notes.strip()}"
        keep_stock.last_updated = datetime.utcnow()

        db.delete(merge_stock)

    # ── Soft-delete duplicate (free provider link so keep can use it later) ─
    merge.is_active = False
    merge.is_deleted = True
    merge.provider_id = None
    merge.provider_service_id = None
    # Keep SKU unique; mark clearly so admins can spot merged rows in DB/tools.
    if not merge.sku.endswith("_merged"):
        base = (merge.sku or f"svc_{merge.id}")[:110]
        candidate = f"{base}_merged"
        n = 1
        while db.query(Service).filter(Service.sku == candidate, Service.id != merge.id).first():
            candidate = f"{base}_merged{n}"[:120]
            n += 1
        merge.sku = candidate

    # Ensure keep stays sellable.
    keep.is_deleted = False
    keep.is_active = True

    db.flush()
    return MergeResult(
        keep_id=keep.id,
        merge_id=merge.id,
        orders_moved=orders_moved,
        discounts_moved=discounts_moved,
        discounts_skipped=discounts_skipped,
        sales_moved=sales_moved,
        sales_deactivated=sales_deactivated,
        stock_lines_merged=stock_lines_merged,
        qty_added=qty_added,
        reserved_added=reserved_added,
    )
