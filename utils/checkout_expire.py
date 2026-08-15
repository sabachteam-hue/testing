"""PayFast unpaid checkout expiry.

Rules:
- While the customer stays on the PayFast payment screen, the link stays valid
  until they pay, or until the idle window (default 20 minutes) passes.
- If they open any other bot command/menu, pending PayFast checkouts expire
  immediately and silently (no Telegram notice). Opening the old link later
  shows "session closed — go to Products".
- Idle timeout expiry is also silent; the customer only sees the session-closed
  message when they reopen the old payment link / session.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta

from database.models import Order, SessionLocal, Transaction, User
from utils.stock_manager import release_stock

logger = logging.getLogger(__name__)

_PAYFAST_ORDER_NOTE = re.compile(r"^payfast_order:(\d+)$")
_IDLE_EXPIRE_REASON = "Expired: payment not completed within {minutes} minutes."
_LEAVE_EXPIRE_REASON = "Expired: customer left the payment screen."

SESSION_CLOSED_HTML = (
    "<h3>Session closed</h3>"
    "<p>This payment session has ended. Please go back to Telegram, "
    "open <strong>Products</strong>, and place a new order to buy again.</p>"
)


def unpaid_checkout_expire_minutes() -> int:
    """Used by non-PayFast order FSM / TXID session timeouts (unchanged default 10)."""
    raw = (os.getenv("UNPAID_CHECKOUT_EXPIRE_MINUTES") or "10").strip()
    try:
        minutes = int(raw)
    except ValueError:
        minutes = 10
    return max(minutes, 1)


def payfast_checkout_expire_minutes() -> int:
    """Idle timeout while the customer stays on PayFast payment (default 20)."""
    raw = (os.getenv("PAYFAST_CHECKOUT_EXPIRE_MINUTES") or "20").strip()
    try:
        minutes = int(raw)
    except ValueError:
        minutes = 20
    return max(minutes, 1)


def unpaid_checkout_cutoff(now: datetime | None = None) -> datetime:
    now = now or datetime.utcnow()
    return now - timedelta(minutes=unpaid_checkout_expire_minutes())


def payfast_checkout_cutoff(now: datetime | None = None) -> datetime:
    now = now or datetime.utcnow()
    return now - timedelta(minutes=payfast_checkout_expire_minutes())


def is_payfast_checkout_tx(tx: Transaction) -> bool:
    """True for PayFast hosted-checkout deposits (order purchase or wallet top-up)."""
    if tx.tx_type != "deposit":
        return False
    note = (tx.note or "").strip()
    lower = note.lower()
    if lower.startswith("payfast_order:") or lower in {"payfast_deposit", "payfast"}:
        return True
    if "payfast" in lower:
        return True
    # Legacy wallet top-ups: pending deposit with no TX hash and no verification yet.
    if tx.status == "pending" and not tx.tx_hash and tx.verification is None:
        return True
    return False


def linked_order_id_from_tx(tx: Transaction) -> int | None:
    match = _PAYFAST_ORDER_NOTE.match((tx.note or "").strip())
    if not match:
        return None
    return int(match.group(1))


def silence_pending_checkouts_for_telegram(telegram_id: str) -> int:
    """Back-compat: leaving the payment screen now expires immediately (silent)."""
    return expire_pending_checkouts_silently_for_telegram(telegram_id)


def silence_pending_checkouts_for_user(db, user_id: int) -> int:
    """Back-compat alias — prefer expire_pending_checkouts_silently_for_user."""
    return expire_pending_checkouts_silently_for_user(db, user_id)


def expire_pending_checkouts_silently_for_telegram(telegram_id: str) -> int:
    """Customer left PayFast payment for another bot command — expire now, no DM."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == str(telegram_id)).first()
        if not user:
            return 0
        return expire_pending_checkouts_silently_for_user(db, user.id)
    finally:
        db.close()


def expire_pending_checkouts_silently_for_user(db, user_id: int) -> int:
    """Expire all pending PayFast checkouts for this user immediately (silent)."""
    changed = 0
    orders = (
        db.query(Order)
        .filter(
            Order.user_id == user_id,
            Order.status == "pending",
            Order.note.ilike("%Awaiting PayFast%"),
        )
        .all()
    )
    order_ids = {order.id for order in orders}
    for order in orders:
        _expire_order(db, order, reason=_LEAVE_EXPIRE_REASON)
        changed += 1

    txs = (
        db.query(Transaction)
        .filter(
            Transaction.user_id == user_id,
            Transaction.tx_type == "deposit",
            Transaction.status == "pending",
        )
        .all()
    )
    for tx in txs:
        if not is_payfast_checkout_tx(tx):
            continue
        # Linked order already handled above; still expire the tx row.
        order_id = linked_order_id_from_tx(tx)
        if order_id and order_id not in order_ids:
            order = db.get(Order, order_id)
            if order and order.status == "pending" and order.user_id == user_id:
                _expire_order(db, order, reason=_LEAVE_EXPIRE_REASON)
                changed += 1
        if tx.status == "pending":
            _expire_transaction(tx, reason=_LEAVE_EXPIRE_REASON)
            changed += 1

    if changed:
        db.commit()
        logger.info(
            "[CHECKOUT-EXPIRE] Silent leave-expire user_id=%s rows=%s",
            user_id,
            changed,
        )
    return changed


def _expire_transaction(tx: Transaction, *, reason: str) -> None:
    if tx.status != "pending":
        return
    tx.status = "expired"
    tx.blockchain_status = "expired"
    tx.expire_notify = False
    # Keep the original payfast_order:/payfast_deposit tag so Method still resolves,
    # and append the expire reason for admin visibility.
    if tx.note and "Expired:" not in tx.note:
        tx.note = f"{tx.note} | {reason}"
    elif not tx.note:
        tx.note = reason


def _expire_order(db, order: Order, *, reason: str) -> None:
    if order.status != "pending":
        return
    previous = order.status
    order.status = "expired"
    order.note = reason
    order.expire_notify = False
    if previous == "pending":
        try:
            release_stock(db, order.service_id, order.quantity)
        except Exception:  # noqa: BLE001
            logger.exception(
                "[CHECKOUT-EXPIRE] Failed to release stock for order=%s",
                order.order_code,
            )


def expire_unpaid_checkout_tx(db, tx: Transaction, *, now: datetime | None = None) -> bool:
    """Expire one pending PayFast checkout transaction (and linked order if any).

    Returns True when something was expired (idle window only).
    """
    now = now or datetime.utcnow()
    minutes = payfast_checkout_expire_minutes()
    cutoff = now - timedelta(minutes=minutes)
    reason = _IDLE_EXPIRE_REASON.format(minutes=minutes)

    if tx.status != "pending" or not is_payfast_checkout_tx(tx):
        return False
    if not tx.created_at or tx.created_at > cutoff:
        return False

    order_id = linked_order_id_from_tx(tx)
    order = db.get(Order, order_id) if order_id else None
    if order and order.status == "pending":
        _expire_order(db, order, reason=reason)
    _expire_transaction(tx, reason=reason)
    logger.info(
        "[CHECKOUT-EXPIRE] tx=%s order=%s amount=%s idle-expired after %s minutes",
        tx.id,
        order.order_code if order else None,
        tx.amount,
        minutes,
    )
    return True


def expire_unpaid_checkouts_once(db=None) -> dict:
    """Scan and idle-expire stale unpaid PayFast checkouts. Always silent (no DMs)."""
    owns_session = db is None
    if owns_session:
        db = SessionLocal()
    expired_txs = 0
    expired_orders = 0
    try:
        minutes = payfast_checkout_expire_minutes()
        cutoff = payfast_checkout_cutoff()
        reason = _IDLE_EXPIRE_REASON.format(minutes=minutes)

        # 1) Pending PayFast order checkouts (stock holders).
        pending_orders = (
            db.query(Order)
            .filter(
                Order.status == "pending",
                Order.created_at <= cutoff,
                Order.note.ilike("%Awaiting PayFast%"),
            )
            .limit(100)
            .all()
        )
        for order in pending_orders:
            _expire_order(db, order, reason=reason)
            expired_orders += 1
            linked_txs = (
                db.query(Transaction)
                .filter(
                    Transaction.status == "pending",
                    Transaction.note == f"payfast_order:{order.id}",
                )
                .all()
            )
            for tx in linked_txs:
                _expire_transaction(tx, reason=reason)
                expired_txs += 1

        # 2) Any remaining pending PayFast txs (wallet top-up or orphaned order links).
        pending_txs = (
            db.query(Transaction)
            .filter(
                Transaction.tx_type == "deposit",
                Transaction.status == "pending",
                Transaction.created_at <= cutoff,
            )
            .limit(100)
            .all()
        )
        for tx in pending_txs:
            if not is_payfast_checkout_tx(tx):
                continue
            order_id = linked_order_id_from_tx(tx)
            order = db.get(Order, order_id) if order_id else None
            if order and order.status == "pending":
                _expire_order(db, order, reason=reason)
                expired_orders += 1
            if tx.status == "pending":
                _expire_transaction(tx, reason=reason)
                expired_txs += 1

        if expired_txs or expired_orders:
            db.commit()
            logger.info(
                "[CHECKOUT-EXPIRE] Idle scan expired_orders=%s expired_txs=%s",
                expired_orders,
                expired_txs,
            )
        return {
            "expired_orders": expired_orders,
            "expired_txs": expired_txs,
            "notifications": [],
        }
    except Exception:  # noqa: BLE001
        logger.exception("[CHECKOUT-EXPIRE] Scan failed")
        if owns_session:
            db.rollback()
        raise
    finally:
        if owns_session:
            db.close()
