"""Admin refund calculator helpers."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy.orm import Session

from database.models import Order, RefundLog, Transaction, User
from utils.helpers import strip_html_tags
from utils.notifications import send_user_message

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def _clean_text(value: Optional[str]) -> str:
    return strip_html_tags(value or "").strip()


def parse_subscription_days(warranty: Optional[str]) -> Optional[int]:
    """Parse subscription length from the dedicated warranty field text."""
    if not warranty:
        return None
    text = _clean_text(warranty).lower()
    if not text or text in {"—", "-", "n/a", "na"}:
        return None

    # Ranges like "25-30 days" / "6-7 days" → use the upper bound
    m = re.search(r"(\d+)\s*[-–—to]+\s*(\d+)\s*(?:days?|d)\b", text)
    if m:
        days = max(int(m.group(1)), int(m.group(2)))
        return days if days > 0 else None

    m = re.search(r"(\d+)\s*(?:days?|d)\b", text)
    if m:
        days = int(m.group(1))
        return days if days > 0 else None

    # Hours (e.g. "24 Hours") → round up to whole days for refund math
    m = re.search(r"(\d+)\s*(?:hours?|hrs?|h)\b", text)
    if m:
        hours = int(m.group(1))
        if hours <= 0:
            return None
        return max(1, (hours + 23) // 24)

    # 1 month / 3 months
    m = re.search(r"(\d+)\s*(?:-|)?\s*(?:months?|mos?|mo)\b", text)
    if m:
        months = int(m.group(1))
        return months * 30 if months > 0 else None

    m = re.search(r"(\d+)\s*(?:-|)?\s*(?:years?|yrs?)\b", text)
    if m:
        years = int(m.group(1))
        return years * 365 if years > 0 else None

    if any(token in text for token in ("lifetime", "forever", "permanent")):
        return None

    # Bare number in warranty field (e.g. "6") = days
    m = re.fullmatch(r"(\d+)", text)
    if m:
        days = int(m.group(1))
        return days if days > 0 else None

    return None


def resolve_subscription_source(order: Order) -> tuple[Optional[int], Optional[str], bool]:
    """
    Subscription days from the product Warranty field only
    (not product name, description, or notes).
    """
    service = getattr(order, "service", None)
    warranty = _clean_text(getattr(service, "warranty", None) if service else None)
    if not warranty or warranty in {"—", "-"}:
        return None, None, False
    days = parse_subscription_days(warranty)
    return days, warranty, days is not None


def purchase_date_for_refund(order: Order) -> datetime:
    """Prefer completed_at, else created_at."""
    dt = order.completed_at or order.created_at
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def days_used_since(purchase_dt: datetime, now: Optional[datetime] = None) -> int:
    """Whole calendar days used since purchase."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if purchase_dt.tzinfo is None:
        purchase_dt = purchase_dt.replace(tzinfo=timezone.utc)
    delta = now - purchase_dt
    if delta.total_seconds() <= 0:
        return 0
    full_days = int(delta.total_seconds() // 86400)
    # Any elapsed time on day 0 counts as 1 day used.
    return max(full_days, 1) if full_days == 0 else full_days


@dataclass
class RefundBreakdown:
    order_id: int
    order_code: str
    total_price: Decimal
    subscription_days: int
    days_used: int
    remaining_days: int
    daily_rate: Decimal
    refund_amount: Decimal
    already_refunded: bool
    message: Optional[str] = None
    warranty_text: Optional[str] = None
    purchase_date: Optional[datetime] = None
    parsed_from_warranty: bool = False

    @property
    def has_refund(self) -> bool:
        return (not self.already_refunded) and self.refund_amount > ZERO and not self.message


def calculate_refund(
    order: Order,
    subscription_days: Optional[int] = None,
    now: Optional[datetime] = None,
) -> RefundBreakdown:
    total = money(order.amount_usdt)
    auto_days, source_text, parsed = resolve_subscription_source(order)
    warranty_text = source_text

    if subscription_days is None:
        subscription_days = auto_days
    else:
        # Manual override — still keep discovered source text for display
        parsed = True

    already = bool(getattr(order, "refund_method", None)) or str(order.status or "") == "refunded"

    if already:
        return RefundBreakdown(
            order_id=order.id,
            order_code=order.order_code,
            total_price=total,
            subscription_days=int(subscription_days or 0),
            days_used=0,
            remaining_days=0,
            daily_rate=ZERO,
            refund_amount=money(getattr(order, "refund_amount", 0) or 0),
            already_refunded=True,
            message="Already refunded (wallet/manual).",
            warranty_text=warranty_text,
            purchase_date=purchase_date_for_refund(order),
            parsed_from_warranty=parsed,
        )

    if not subscription_days or subscription_days <= 0:
        return RefundBreakdown(
            order_id=order.id,
            order_code=order.order_code,
            total_price=total,
            subscription_days=0,
            days_used=0,
            remaining_days=0,
            daily_rate=ZERO,
            refund_amount=ZERO,
            already_refunded=False,
            message="Enter subscription days to calculate refund.",
            warranty_text=warranty_text,
            purchase_date=purchase_date_for_refund(order),
            parsed_from_warranty=False,
        )

    purchase = purchase_date_for_refund(order)
    used = days_used_since(purchase, now=now)
    remaining = max(0, int(subscription_days) - used)
    daily = (total / Decimal(int(subscription_days))).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    refund = money(daily * Decimal(remaining)) if remaining > 0 else ZERO

    message = None
    if used >= int(subscription_days) or remaining <= 0 or refund <= ZERO:
        message = "Already complete / no refund found"
        refund = ZERO
        remaining = 0

    return RefundBreakdown(
        order_id=order.id,
        order_code=order.order_code,
        total_price=total,
        subscription_days=int(subscription_days),
        days_used=used,
        remaining_days=remaining,
        daily_rate=daily.quantize(CENT, rounding=ROUND_HALF_UP),
        refund_amount=refund,
        already_refunded=False,
        message=message,
        warranty_text=warranty_text,
        purchase_date=purchase,
        parsed_from_warranty=parsed,
    )


def credit_wallet_refund(
    db: Session,
    *,
    order: Order,
    user: User,
    amount: Decimal,
    breakdown: RefundBreakdown,
    admin_actor: str = "admin",
    note: Optional[str] = None,
) -> tuple[float, Transaction]:
    """Credit user wallet and write refund transaction + log. Caller commits."""
    amount = money(amount)
    if amount <= ZERO:
        raise ValueError("Refund amount must be greater than 0")
    if getattr(order, "refund_method", None) or order.status == "refunded":
        raise ValueError("Order already refunded")

    before = float(money(user.wallet_usdt))
    after = float(money(before + float(amount)))
    user.wallet_usdt = after

    tx = Transaction(
        user_id=user.id,
        amount=float(amount),
        tx_type="refund",
        status="confirmed",
        blockchain_status="confirmed",
        note=note or f"Refund for {order.order_code}",
    )
    db.add(tx)

    order.status = "refunded"
    order.refund_method = "wallet"
    order.refund_amount = float(amount)
    order.refunded_at = datetime.utcnow()

    log = RefundLog(
        order_id=order.id,
        order_code=order.order_code,
        admin_name=admin_actor or "admin",
        refund_amount=float(amount),
        refund_method="wallet",
        days_total=breakdown.subscription_days,
        days_used=breakdown.days_used,
        days_remaining=breakdown.remaining_days,
        note=note or f"Wallet refund for {order.order_code}",
    )
    db.add(log)
    db.flush()
    return after, tx


def mark_manual_refund(
    db: Session,
    *,
    order: Order,
    amount: Decimal,
    breakdown: RefundBreakdown,
    admin_actor: str = "admin",
    note: Optional[str] = None,
) -> RefundLog:
    """Mark order refunded manually (no wallet credit). Caller commits."""
    amount = money(amount)
    if amount <= ZERO:
        raise ValueError("Refund amount must be greater than 0")
    if getattr(order, "refund_method", None) or order.status == "refunded":
        raise ValueError("Order already refunded")

    order.status = "refunded"
    order.refund_method = "manual"
    order.refund_amount = float(amount)
    order.refunded_at = datetime.utcnow()

    log = RefundLog(
        order_id=order.id,
        order_code=order.order_code,
        admin_name=admin_actor or "admin",
        refund_amount=float(amount),
        refund_method="manual",
        days_total=breakdown.subscription_days,
        days_used=breakdown.days_used,
        days_remaining=breakdown.remaining_days,
        note=(note or "").strip() or f"Manual refund for {order.order_code}",
    )
    db.add(log)
    db.flush()
    return log


async def notify_wallet_refund(
    telegram_id: str,
    order_code: str,
    amount: Decimal | float,
    new_balance: Decimal | float,
    db: Session | None = None,
) -> None:
    """Telegram: order id + refund amount + new balance (premium tick icon)."""
    if not telegram_id:
        return
    from database.models import SessionLocal
    from utils.ui_icons import label_icons

    own_session = db is None
    session = db or SessionLocal()
    try:
        icons = label_icons(session)
    finally:
        if own_session:
            session.close()

    text = (
        f"{icons['tick']} Refund credited\n\n"
        f"{icons['order']} Order: {order_code}\n"
        f"{icons['price']} Refund: {float(money(amount)):.2f} USDT\n"
        f"{icons['wallet']} New wallet balance: {float(money(new_balance)):.2f} USDT"
    )
    await send_user_message(telegram_id, text, parse_mode="HTML")
