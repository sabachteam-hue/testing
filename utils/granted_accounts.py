"""Granted accounts and subscription lifecycle tracking utilities.

Provides:
- Credential parsing from delivered text
- Subscription duration resolution (from product duration_days or warranty field)
- Live lifecycle metrics (days remaining, days used, progress %)
- Authoritative status determination (active, expired, refunded, frozen)
- Idempotent order-to-subscription auto-creation and legacy order sync
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from sqlalchemy.orm import Session, joinedload

from database.models import GrantedAccount, Order, Service, Transaction, User
from utils.helpers import parse_icon, strip_html_tags
from utils.refund_tool import parse_subscription_days

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def _format_media_url(path: str | None, request: Any = None) -> str | None:
    if not path:
        return None
    val = str(path).strip()
    if not val:
        return None
    if val.startswith(("http://", "https://")):
        return val
    if request is not None:
        try:
            base = str(request.base_url).rstrip("/")
            if not val.startswith("/"):
                val = f"/{val}"
            return f"{base}{val}"
        except Exception:
            pass
    return val


def parse_delivery_credentials(delivered_info: str | None) -> list[dict]:
    """Parse delivered credentials text into a list of structured account dictionaries.

    Supports:
    - Multi-account blocks delimited by 'Account 1:', 'Account 2:', etc.
    - Double-newline separated account blocks with key-value pairs.
    - Line-by-line format: 'email:password', 'user:pass:pin', or 'user|pass'.
    - Single free-form credential keys/tokens.
    """
    raw = (delivered_info or "").strip()
    if not raw:
        return []

    # 1. Multi-account split: "Account 1:", "Account 2:", etc.
    acc_splits = re.split(r"(?i)(?:^|\n)\s*account\s+\d+\s*:\s*\n?", raw)
    if len(acc_splits) > 1 and any(s.strip() for s in acc_splits[1:]):
        results = []
        for blk in acc_splits:
            cleaned = blk.strip()
            if cleaned:
                results.extend(_parse_single_block_or_lines(cleaned))
        if results:
            return results

    # 2. Double-newline blocks where each block has email/password indicators
    double_nl_blocks = [b.strip() for b in re.split(r"\n\s*\n+", raw) if b.strip()]
    if len(double_nl_blocks) > 1 and any(
        re.search(r"(?i)(password|pass|email|username|login)", b) for b in double_nl_blocks
    ):
        results = []
        for blk in double_nl_blocks:
            results.extend(_parse_single_block_or_lines(blk))
        if results:
            return results

    return _parse_single_block_or_lines(raw)


def _parse_single_block_or_lines(block: str) -> list[dict]:
    lines = [l.strip() for l in block.splitlines() if l.strip()]
    if not lines:
        return []

    # Check if this block contains key-value lines (e.g. "Email: ...", "Password: ...")
    has_kv = any(
        re.match(r"(?i)^(email|user|username|login|password|pass|pin|profile|recovery|note|instruction)s?\s*:", l)
        for l in lines
    )
    if has_kv:
        acc: dict[str, Optional[str]] = {
            "login_email": None,
            "login_password": None,
            "profile_pin": None,
            "recovery_info": None,
            "custom_instructions": None,
            "account_note": None,
            "raw_credentials": block,
        }
        for l in lines:
            m = re.match(r"(?i)^([^:]+):\s*(.*)$", l)
            if m:
                k = m.group(1).strip().lower()
                v = m.group(2).strip()
                if any(w in k for w in ("email", "username", "user", "login")) and not any(
                    w in k for w in ("recovery", "verify")
                ):
                    acc["login_email"] = v
                elif any(w in k for w in ("password", "pass")):
                    acc["login_password"] = v
                elif "pin" in k or "profile" in k:
                    acc["profile_pin"] = v
                elif any(w in k for w in ("recovery", "verify")):
                    acc["recovery_info"] = v
                elif any(w in k for w in ("note", "info")):
                    acc["account_note"] = v
                elif any(w in k for w in ("instruction", "how to")):
                    acc["custom_instructions"] = v
        if acc["login_email"] or acc["login_password"]:
            return [acc]

    # Otherwise parse line-by-line (e.g. user:pass per line for bulk orders)
    accounts = []
    for l in lines:
        # Skip header lines
        if re.match(r"(?i)^(product|order|quantity|price|warranty|your delivery details):", l):
            continue

        if ":" in l:
            parts = [p.strip() for p in l.split(":")]
            if len(parts) >= 3:
                accounts.append(
                    {
                        "login_email": parts[0],
                        "login_password": parts[1],
                        "profile_pin": ":".join(parts[2:]),
                        "recovery_info": None,
                        "custom_instructions": None,
                        "account_note": None,
                        "raw_credentials": l,
                    }
                )
            else:
                accounts.append(
                    {
                        "login_email": parts[0],
                        "login_password": parts[1],
                        "profile_pin": None,
                        "recovery_info": None,
                        "custom_instructions": None,
                        "account_note": None,
                        "raw_credentials": l,
                    }
                )
        elif "|" in l:
            parts = [p.strip() for p in l.split("|")]
            accounts.append(
                {
                    "login_email": parts[0],
                    "login_password": parts[1] if len(parts) > 1 else parts[0],
                    "profile_pin": parts[2] if len(parts) > 2 else None,
                    "recovery_info": None,
                    "custom_instructions": None,
                    "account_note": None,
                    "raw_credentials": l,
                }
            )
        else:
            accounts.append(
                {
                    "login_email": "Access Key / License",
                    "login_password": l,
                    "profile_pin": None,
                    "recovery_info": None,
                    "custom_instructions": None,
                    "account_note": None,
                    "raw_credentials": l,
                }
            )

    return accounts if accounts else [_fallback_account(block)]


def _fallback_account(text: str) -> dict:
    return {
        "login_email": "Customer Account",
        "login_password": text,
        "profile_pin": None,
        "recovery_info": None,
        "custom_instructions": None,
        "account_note": None,
        "raw_credentials": text,
    }


def resolve_service_duration_days(service: Service | None) -> int:
    """Determine subscription duration in days for a service.

    Priority:
    1. Service.duration_days (if explicitly set and > 0)
    2. Parsed from Service.warranty text (e.g. '30 days', '1 month', '1 year')
    3. Safe default: 30 days
    """
    if service is not None:
        if getattr(service, "duration_days", None) and int(service.duration_days) > 0:
            return int(service.duration_days)
        warranty_str = getattr(service, "warranty", None)
        if warranty_str:
            parsed = parse_subscription_days(warranty_str)
            if parsed and parsed > 0:
                return parsed

    return 30


def compute_account_lifecycle(
    account: GrantedAccount,
    order: Order | None = None,
    now: datetime | None = None,
) -> dict:
    """Calculate authoritative subscription metrics for a granted account.

    Returns:
    - effective_status: 'active' | 'expired' | 'refunded' | 'frozen'
    - status_label: 'Active' | 'Expired' | 'Refunded' | 'Frozen'
    - status_badge: 'completed' (green) | 'expired' | 'refunded' | 'frozen'
    - total_days: integer subscription duration
    - days_used: integer days elapsed since start
    - days_remaining: integer days remaining (clamped >= 0)
    - progress_percent: float 0.0 to 100.0
    - is_active, is_expired, is_refunded, is_frozen
    """
    if now is None:
        now = datetime.utcnow()
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)

    total_days = max(1, int(account.duration_days or 30))
    start_at = account.subscription_start_at or account.created_at or now
    if start_at and start_at.tzinfo is not None:
        start_at = start_at.replace(tzinfo=None)
    expires_at = account.subscription_expires_at
    if expires_at and expires_at.tzinfo is not None:
        expires_at = expires_at.replace(tzinfo=None)

    # Check order refund state
    order_refunded = False
    if order is not None:
        order_refunded = (order.status or "").lower() == "refunded" or bool(getattr(order, "refunded_at", None))
    account_refunded = (account.status or "").lower() == "refunded" or order_refunded

    account_frozen = (account.status or "").lower() == "frozen"

    if now >= expires_at:
        is_expired = True
        days_remaining = 0
        days_used = total_days
        progress_percent = 100.0
    else:
        is_expired = False
        diff_sec = (expires_at - now).total_seconds()
        # Ceil to whole remaining days
        days_remaining = max(0, int((diff_sec + 86399) // 86400))
        days_used = max(0, total_days - days_remaining)
        progress_percent = min(100.0, max(0.0, round((days_used / total_days) * 100.0, 1)))

    if account_refunded:
        effective_status = "refunded"
        status_label = "Refunded"
        status_badge = "refunded"
    elif account_frozen:
        effective_status = "frozen"
        status_label = "Frozen (Claim in Review)"
        status_badge = "frozen"
    elif is_expired or (account.status or "").lower() == "expired":
        effective_status = "expired"
        status_label = "Expired"
        status_badge = "expired"
        days_remaining = 0
        progress_percent = 100.0
    else:
        effective_status = "active"
        status_label = "Active"
        status_badge = "completed"

    return {
        "effective_status": effective_status,
        "status_label": status_label,
        "status_badge": status_badge,
        "total_days": total_days,
        "days_used": days_used,
        "days_remaining": days_remaining,
        "progress_percent": progress_percent,
        "is_active": effective_status == "active",
        "is_expired": effective_status == "expired",
        "is_refunded": effective_status == "refunded",
        "is_frozen": effective_status == "frozen",
    }


def calculate_account_refund_estimate(
    account: GrantedAccount,
    order: Order | None = None,
    now: datetime | None = None,
) -> dict:
    """Calculate authoritative pro-rata refund breakdown for a granted account.

    Formula:
      Refund Value = Paid Amount / Total Subscription Days * Eligible Days Remaining
    Deterministic decimal arithmetic with ROUND_HALF_UP.
    """
    if now is None:
        now = datetime.utcnow()
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)

    ord_obj = order or getattr(account, "order", None)
    lifecycle = compute_account_lifecycle(account, order=ord_obj, now=now)

    total_order_amount = Decimal(str(getattr(ord_obj, "amount_usdt", 0.0) or 0.0))
    qty = max(1, int(getattr(ord_obj, "quantity", 1) or 1))
    unit_paid = (total_order_amount / Decimal(str(qty))).quantize(CENT, rounding=ROUND_HALF_UP)

    total_days = max(1, lifecycle["total_days"])
    days_used = lifecycle["days_used"]
    days_remaining = lifecycle["days_remaining"]

    service_name = "Subscription Account"
    if account.service:
        service_name = getattr(account.service, "name", "Subscription Account")

    # Check order/account refunded
    if lifecycle["is_refunded"]:
        hist_amount = float(getattr(ord_obj, "refund_amount", 0.0) or 0.0)
        hist_method = getattr(ord_obj, "refund_method", "wallet") or "wallet"
        hist_dt = getattr(ord_obj, "refunded_at", None)
        hist_dt_iso = hist_dt.isoformat() if hist_dt else None

        return {
            "account_id": account.id,
            "order_id": account.order_id,
            "order_code": ord_obj.order_code if ord_obj else None,
            "product_name": service_name,
            "currency": "USDT",
            "amount_paid": float(unit_paid),
            "total_order_amount": float(total_order_amount),
            "order_quantity": qty,
            "subscription_start_at": (account.subscription_start_at or account.created_at).isoformat() if (account.subscription_start_at or account.created_at) else None,
            "current_time": now.isoformat(),
            "subscription_expires_at": account.subscription_expires_at.isoformat() if account.subscription_expires_at else None,
            "total_days": total_days,
            "days_used": total_days,
            "days_remaining": 0,
            "progress_percent": 100.0,
            "daily_rate": 0.0,
            "estimated_refund": 0.0,
            "is_eligible": False,
            "effective_status": "refunded",
            "status_label": "Refunded",
            "already_refunded": True,
            "message": f"This order was already refunded (${hist_amount:.2f} via {hist_method.title()}).",
            "historical_refund": {
                "refund_amount": hist_amount,
                "refund_method": hist_method,
                "refunded_at": hist_dt_iso,
            },
        }

    # Check frozen
    if lifecycle["is_frozen"]:
        return {
            "account_id": account.id,
            "order_id": account.order_id,
            "order_code": ord_obj.order_code if ord_obj else None,
            "product_name": service_name,
            "currency": "USDT",
            "amount_paid": float(unit_paid),
            "total_order_amount": float(total_order_amount),
            "order_quantity": qty,
            "subscription_start_at": (account.subscription_start_at or account.created_at).isoformat() if (account.subscription_start_at or account.created_at) else None,
            "current_time": now.isoformat(),
            "subscription_expires_at": account.subscription_expires_at.isoformat() if account.subscription_expires_at else None,
            "total_days": total_days,
            "days_used": days_used,
            "days_remaining": days_remaining,
            "progress_percent": lifecycle["progress_percent"],
            "daily_rate": 0.0,
            "estimated_refund": 0.0,
            "is_eligible": False,
            "effective_status": "frozen",
            "status_label": "Frozen (Claim in Review)",
            "already_refunded": False,
            "message": "Account is currently frozen under warranty claim review.",
            "historical_refund": None,
        }

    # Check expired
    if lifecycle["is_expired"] or days_remaining <= 0:
        return {
            "account_id": account.id,
            "order_id": account.order_id,
            "order_code": ord_obj.order_code if ord_obj else None,
            "product_name": service_name,
            "currency": "USDT",
            "amount_paid": float(unit_paid),
            "total_order_amount": float(total_order_amount),
            "order_quantity": qty,
            "subscription_start_at": (account.subscription_start_at or account.created_at).isoformat() if (account.subscription_start_at or account.created_at) else None,
            "current_time": now.isoformat(),
            "subscription_expires_at": account.subscription_expires_at.isoformat() if account.subscription_expires_at else None,
            "total_days": total_days,
            "days_used": total_days,
            "days_remaining": 0,
            "progress_percent": 100.0,
            "daily_rate": 0.0,
            "estimated_refund": 0.0,
            "is_eligible": False,
            "effective_status": "expired",
            "status_label": "Expired",
            "already_refunded": False,
            "message": "Subscription has expired. No eligible days remaining for refund.",
            "historical_refund": None,
        }

    # Active & eligible
    daily_rate = (unit_paid / Decimal(str(total_days))).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    refund_amount = (daily_rate * Decimal(str(days_remaining))).quantize(CENT, rounding=ROUND_HALF_UP)
    refund_amount = min(unit_paid, max(ZERO, refund_amount))

    return {
        "account_id": account.id,
        "order_id": account.order_id,
        "order_code": ord_obj.order_code if ord_obj else None,
        "product_name": service_name,
        "currency": "USDT",
        "amount_paid": float(unit_paid),
        "total_order_amount": float(total_order_amount),
        "order_quantity": qty,
        "subscription_start_at": (account.subscription_start_at or account.created_at).isoformat() if (account.subscription_start_at or account.created_at) else None,
        "current_time": now.isoformat(),
        "subscription_expires_at": account.subscription_expires_at.isoformat() if account.subscription_expires_at else None,
        "total_days": total_days,
        "days_used": days_used,
        "days_remaining": days_remaining,
        "progress_percent": lifecycle["progress_percent"],
        "daily_rate": float(daily_rate),
        "estimated_refund": float(refund_amount),
        "is_eligible": True,
        "effective_status": "active",
        "status_label": "Active",
        "already_refunded": False,
        "message": None,
        "historical_refund": None,
    }


def format_customer_transaction(tx: Transaction) -> dict:
    """Format transaction for customer-safe wallet ledger."""
    raw_type = (tx.tx_type or "").lower().strip()
    raw_status = (tx.status or "confirmed").lower().strip()

    # Direction & Labels
    if raw_type == "refund":
        type_label = "Refund Credit"
        direction = "credit"
        sign = "+"
    elif raw_type in ("admin_credit", "credit"):
        type_label = "Admin Credit"
        direction = "credit"
        sign = "+"
    elif raw_type == "deposit":
        type_label = "Wallet Deposit"
        direction = "credit"
        sign = "+"
    elif raw_type == "admin_debit":
        type_label = "Admin Debit"
        direction = "debit"
        sign = "-"
    elif raw_type in ("deduct", "purchase", "order_payment"):
        type_label = "Purchase Debit"
        direction = "debit"
        sign = "-"
    else:
        type_label = raw_type.replace("_", " ").title() or "Adjustment"
        direction = "credit" if raw_type.startswith("credit") else "debit"
        sign = "+" if direction == "credit" else "-"

    # Status label & badge
    if raw_status in ("confirmed", "completed", "success"):
        status_label = "Confirmed"
        status_badge = "completed"
    elif raw_status in ("pending", "waiting"):
        status_label = "Pending"
        status_badge = "pending"
    elif raw_status in ("failed", "rejected", "cancelled", "canceled"):
        status_label = "Failed"
        status_badge = "cancelled"
    else:
        status_label = raw_status.title()
        status_badge = "processing"

    amt = float(tx.amount or 0.0)
    amt_formatted = f"{sign}${amt:.2f}"

    # Customer-safe reference
    ref = tx.payfast_reference or None
    if not ref and tx.tx_hash and not tx.tx_hash.startswith("internal_"):
        ref = tx.tx_hash[:16] + "..." if len(tx.tx_hash) > 20 else tx.tx_hash

    # Clean description: sanitize admin/internal notes
    desc = tx.note or ""
    order_code = None
    m = re.search(r"\b(SMF-[A-Z0-9]+)\b", desc, re.IGNORECASE)
    if m:
        order_code = m.group(1).upper()
        if not ref:
            ref = order_code

    if not desc or "secret" in desc.lower() or "token" in desc.lower():
        desc = f"{type_label}" + (f" ({order_code})" if order_code else "")
    else:
        desc = desc.split("\n")[0].strip()
        if len(desc) > 80:
            desc = desc[:77] + "..."

    return {
        "id": tx.id,
        "created_at": tx.created_at.isoformat() if tx.created_at else None,
        "created_date_formatted": tx.created_at.strftime("%b %d, %Y %I:%M %p") if tx.created_at else "—",
        "tx_type": raw_type,
        "type_label": type_label,
        "direction": direction,
        "amount": amt,
        "amount_formatted": amt_formatted,
        "status": raw_status,
        "status_label": status_label,
        "status_badge": status_badge,
        "reference": ref,
        "order_code": order_code,
        "description": desc,
    }


def sync_granted_accounts_for_order(db: Session, order: Order) -> list[GrantedAccount]:
    """Ensure GrantedAccount records exist for an eligible fulfilled or refunded order.

    Idempotent:
    - Never creates duplicate records on repeated calls.
    - Synchronizes refund status if order was refunded.
    - Updates missing fields if necessary.
    """
    if not order or not order.user_id:
        return []

    st = (order.status or "").lower()
    if st not in ("completed", "delivered", "refunded"):
        return []

    delivered_info = getattr(order, "delivered_info", None)
    if not delivered_info or not delivered_info.strip():
        return []

    service = order.service
    duration = resolve_service_duration_days(service)
    start_date = order.completed_at or order.created_at or datetime.utcnow()
    expires_date = start_date + timedelta(days=duration)

    is_order_refunded = st == "refunded" or bool(getattr(order, "refunded_at", None))
    initial_status = "refunded" if is_order_refunded else "active"

    parsed_accounts = parse_delivery_credentials(delivered_info)
    if not parsed_accounts:
        return []

    synced_records = []
    for idx, item in enumerate(parsed_accounts):
        existing = (
            db.query(GrantedAccount)
            .filter(
                GrantedAccount.order_id == order.id,
                GrantedAccount.account_index == idx,
            )
            .first()
        )

        if existing:
            # Idempotent update: update refund status if changed
            if is_order_refunded and existing.status != "refunded":
                existing.status = "refunded"
            # If service duration was updated, adjust expiry
            if existing.duration_days != duration and duration > 0:
                existing.duration_days = duration
                existing.subscription_expires_at = (existing.subscription_start_at or start_date) + timedelta(days=duration)
            synced_records.append(existing)
        else:
            new_acc = GrantedAccount(
                order_id=order.id,
                user_id=order.user_id,
                service_id=order.service_id,
                account_index=idx,
                login_email=item.get("login_email"),
                login_password=item.get("login_password"),
                raw_credentials=item.get("raw_credentials"),
                profile_pin=item.get("profile_pin"),
                recovery_info=item.get("recovery_info"),
                custom_instructions=item.get("custom_instructions"),
                account_note=item.get("account_note"),
                status=initial_status,
                duration_days=duration,
                subscription_start_at=start_date,
                subscription_expires_at=expires_date,
            )
            db.add(new_acc)
            synced_records.append(new_acc)

    db.flush()
    return synced_records


def sync_user_granted_accounts(db: Session, user_id: int) -> None:
    """Sync all completed/refunded orders for a user to guarantee historical records are registered."""
    orders = (
        db.query(Order)
        .options(joinedload(Order.service))
        .filter(
            Order.user_id == user_id,
            Order.status.in_(["completed", "delivered", "refunded"]),
            Order.delivered_info.isnot(None),
        )
        .all()
    )
    for ord_row in orders:
        sync_granted_accounts_for_order(db, ord_row)
    db.commit()


def format_granted_account_payload(
    account: GrantedAccount,
    order: Order | None = None,
    service: Service | None = None,
    request: Any = None,
) -> dict:
    """Serialize GrantedAccount into a customer-safe dictionary."""
    ord_obj = order or getattr(account, "order", None)
    svc_obj = service or getattr(account, "service", None)
    if not svc_obj and ord_obj:
        svc_obj = getattr(ord_obj, "service", None)

    lifecycle = compute_account_lifecycle(account, ord_obj)

    # Safe display values
    name = strip_html_tags(svc_obj.name) if svc_obj else "Subscription Account"
    sku = svc_obj.sku if svc_obj else None
    warranty = strip_html_tags(getattr(svc_obj, "warranty", None) or "") or f"{account.duration_days} Days"

    # Emoji parsing
    emoji_val = "🛍️"
    if svc_obj and getattr(svc_obj, "emoji", None):
        val = str(svc_obj.emoji).strip()
        if "|" in val:
            val = val.split("|", 1)[1].strip()
        emoji_val = val or "🛍️"

    img_url = None
    if svc_obj and getattr(svc_obj, "image_path", None):
        img_url = _format_media_url(svc_obj.image_path, request)

    start_dt = account.subscription_start_at
    expires_dt = account.subscription_expires_at

    return {
        "id": account.id,
        "order_id": account.order_id,
        "order_code": ord_obj.order_code if ord_obj else None,
        "service_id": account.service_id,
        "account_index": int(account.account_index or 0),
        "product_name": name,
        "product_sku": sku,
        "emoji": emoji_val,
        "image_url": img_url,
        "warranty_text": warranty,
        "login_email": account.login_email or "Customer Account",
        "login_password": account.login_password or "",
        "profile_pin": account.profile_pin,
        "recovery_info": account.recovery_info,
        "custom_instructions": account.custom_instructions,
        "account_note": account.account_note,
        "status": lifecycle["effective_status"],
        "status_label": lifecycle["status_label"],
        "status_badge": lifecycle["status_badge"],
        "total_days": lifecycle["total_days"],
        "days_used": lifecycle["days_used"],
        "days_remaining": lifecycle["days_remaining"],
        "progress_percent": lifecycle["progress_percent"],
        "is_active": lifecycle["is_active"],
        "is_expired": lifecycle["is_expired"],
        "is_refunded": lifecycle["is_refunded"],
        "is_frozen": lifecycle["is_frozen"],
        "start_date": start_dt.strftime("%b %d, %Y") if start_dt else "—",
        "start_date_iso": start_dt.isoformat() if start_dt else None,
        "expiry_date": expires_dt.strftime("%b %d, %Y") if expires_dt else "—",
        "expiry_date_iso": expires_dt.isoformat() if expires_dt else None,
        "created_at": account.created_at.strftime("%b %d, %Y %I:%M %p") if getattr(account, "created_at", None) else "—",
        "refund_estimate": calculate_account_refund_estimate(account, ord_obj),
    }
