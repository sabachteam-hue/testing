"""Pre-order manager: FIFO queue fulfillment, 24-hour expiry, and wallet auto-refunds."""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy.orm import Session, joinedload

from database.models import Order, RefundLog, Service, SessionLocal, Stock, Transaction, User
from utils.notifications import send_user_message
from utils.ui_icons import label_icons

logger = logging.getLogger(__name__)


def register_paid_preorder(order: Order) -> None:
    """Set pre-order fields when a customer completes payment for a pre-order item."""
    order.is_preorder = True
    order.preorder_fee = 0.30
    order.status = "preorder_waiting"
    order.preorder_status = "waiting"
    order.preorder_paid_at = datetime.utcnow()


def process_waiting_preorders(db: Session, service_id: int) -> List[Order]:
    """Fulfill waiting pre-orders for a service in FIFO order when stock arrives.

    Protected against race conditions using row-level locking when supported.
    """
    from utils.stock_display import effective_available_qty
    from utils.stock_manager import complete_reserved_stock, consume_stock_account

    service = db.get(Service, service_id)
    if not service:
        return []

    query = (
        db.query(Order)
        .options(joinedload(Order.user), joinedload(Order.service).joinedload(Service.stock))
        .filter(
            Order.service_id == service_id,
            Order.is_preorder.is_(True),
            Order.status == "preorder_waiting",
        )
        .order_by(
            Order.preorder_paid_at.asc(),
            Order.created_at.asc(),
            Order.id.asc(),
        )
    )
    if db.bind and db.bind.dialect.name.startswith("postgresql"):
        query = query.with_for_update(of=Order)

    waiting_orders = query.all()
    if not waiting_orders:
        return []

    fulfilled_orders: List[Order] = []
    for order in waiting_orders:
        # Re-check status inside the transaction
        if order.status != "preorder_waiting":
            continue

        available = effective_available_qty(service)
        if available < order.quantity:
            # Not enough stock for this waiting pre-order; stop so earlier pre-orders
            # maintain FIFO priority over later orders.
            break

        from utils.stock_manager import release_stock, reserve_stock
        try:
            reserve_stock(db, service.id, order.quantity)
        except Exception:
            break

        fulfillment_type = getattr(service, "fulfillment_type", "auto")
        if fulfillment_type == "stock":
            delivered = consume_stock_account(db, service.id, order.quantity)
            if not delivered:
                # Not enough individual account lines ready yet
                release_stock(db, service.id, order.quantity)
                break
            delivered_text = "\n".join(delivered)
            order.delivered_info = delivered_text
            order.status = "completed"
            order.completed_at = datetime.utcnow()
            order.preorder_status = "fulfilled"
            order.note = "Pre-order fulfilled automatically from stock."
            complete_reserved_stock(db, service.id, order.quantity)
        else:
            # Quantity / Auto delivery / Manual with stock
            complete_reserved_stock(db, service.id, order.quantity)
            order.status = "completed"
            order.completed_at = datetime.utcnow()
            order.preorder_status = "fulfilled"
            order.note = "Pre-order fulfilled automatically upon restock."

        if order.delivered_info:
            try:
                from utils.granted_accounts import sync_granted_accounts_for_order
                sync_granted_accounts_for_order(db, order)
            except Exception as exc:
                logger.warning("[PREORDER-FULFILL] Granted accounts sync error: %s", exc)

        db.commit()
        try:
            db.refresh(service)
            if service.stock:
                db.refresh(service.stock)
        except Exception:
            pass
        fulfilled_orders.append(order)

        # Notify customer of successful delivery
        if order.user and order.user.telegram_id and order.status == "completed":
            try:
                from utils.notifications import notify_user_order_completed

                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(notify_user_order_completed(order, service))
                except RuntimeError:
                    # No active event loop in thread
                    pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to notify user for fulfilled pre-order %s: %s", order.order_code, exc)

    return fulfilled_orders


def check_expired_preorders_once(db: Session | None = None) -> int:
    """Find pre-orders waiting > 24 hours. Cancel, refund full paid amount to bot wallet, and notify."""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=24)

        query = (
            db.query(Order)
            .options(joinedload(Order.user), joinedload(Order.service))
            .filter(
                Order.is_preorder.is_(True),
                Order.status == "preorder_waiting",
            )
        )
        if db.bind and db.bind.dialect.name.startswith("postgresql"):
            query = query.with_for_update(of=Order)

        candidates = query.all()
        expired_count = 0

        for order in candidates:
            paid_at = order.preorder_paid_at or order.created_at
            if not paid_at:
                continue
            if paid_at.tzinfo is not None:
                paid_at = paid_at.replace(tzinfo=None)
            if paid_at > cutoff:
                continue

            # Concurrency check: must still be waiting
            if order.status != "preorder_waiting":
                continue

            # Full paid amount refunded to customer's bot wallet
            refund_amount = float(order.amount_usdt or 0.0)
            user = order.user
            new_wallet_balance = 0.0
            if user:
                before = float(user.wallet_usdt or 0.0)
                after = round(before + refund_amount, 6)
                user.wallet_usdt = after
                new_wallet_balance = after

                tx = Transaction(
                    user_id=user.id,
                    amount=refund_amount,
                    tx_type="refund",
                    status="confirmed",
                    blockchain_status="confirmed",
                    note=f"Auto-refund expired pre-order {order.order_code}",
                )
                db.add(tx)

            order.status = "refunded"
            order.refund_method = "wallet"
            order.refund_amount = refund_amount
            order.refunded_at = datetime.utcnow()
            order.preorder_status = "cancelled_refunded"

            log = RefundLog(
                order_id=order.id,
                order_code=order.order_code,
                admin_name="system_auto_expire",
                refund_amount=refund_amount,
                refund_method="wallet",
                days_total=0,
                days_used=0,
                days_remaining=0,
                note="Pre-order cancelled: product did not become available within 24 hours.",
            )
            db.add(log)
            db.commit()
            expired_count += 1

            # Send Telegram notification (Part 15)
            if user and user.telegram_id:
                try:
                    icons = label_icons()
                    text = (
                        f"⚠️ <b>Pre-order Cancelled</b>\n\n"
                        f"{icons.get('order', '📦')} Order ID: {html.escape(order.order_code)}\n\n"
                        f"The product did not become available within 24 hours.\n\n"
                        f"{icons.get('price', '💰')} Refund Amount: ${refund_amount:.2f}\n\n"
                        f"The full amount has been returned to your bot wallet.\n"
                        f"{icons.get('wallet', '💳')} New Wallet Balance: ${new_wallet_balance:.2f}"
                    )
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(send_user_message(user.telegram_id, text, parse_mode="HTML"))
                    except RuntimeError:
                        try:
                            asyncio.run(send_user_message(user.telegram_id, text, parse_mode="HTML"))
                        except Exception:
                            pass
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to send auto-refund notification for order %s: %s", order.order_code, exc)

        return expired_count
    finally:
        if close_db:
            db.close()


async def check_expired_preorders_job() -> None:
    """Periodic background worker checking for 24h expired pre-orders."""
    while True:
        try:
            check_expired_preorders_once()
        except Exception as exc:  # noqa: BLE001
            logger.exception("[PREORDER-EXPIRE] Background job error: %s", exc)
        await asyncio.sleep(60)
