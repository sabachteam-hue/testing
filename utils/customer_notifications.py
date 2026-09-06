"""Customer In-App Notifications and Messages Management Service.

Phase 7:
- Persistent CustomerNotification creation and querying
- Read / unread status tracking and badge counts
- Safe event integration (orders, deliveries, claims, refunds, wallet, security)
- Respects customer notification preference toggles without suppressing critical transactional notices
- Failsafe execution: notification logging failures never roll back core business transactions
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from database.models import CustomerNotification, Order, IssueReport, GrantedAccount, Transaction, User

logger = logging.getLogger(__name__)

# Standard notification category types
TYPE_ORDER = "order"
TYPE_CLAIM = "claim"
TYPE_REFUND = "refund"
TYPE_REPLACEMENT = "replacement"
TYPE_WALLET = "wallet"
TYPE_SECURITY = "security"
TYPE_ANNOUNCEMENT = "announcement"

# Critical transactional types that must never be suppressed by preference toggles
CRITICAL_TYPES = {
    TYPE_REFUND,
    TYPE_REPLACEMENT,
    TYPE_SECURITY,
}


def serialize_notification(notif: CustomerNotification) -> dict[str, Any]:
    """Serialize CustomerNotification into customer-safe payload."""
    created_dt = notif.created_at
    return {
        "id": notif.id,
        "type": notif.type or "info",
        "title": notif.title,
        "message": notif.message,
        "reference_type": notif.reference_type,
        "reference_id": notif.reference_id,
        "link_url": notif.link_url,
        "is_read": bool(notif.is_read),
        "created_at": created_dt.strftime("%b %d, %Y %I:%M %p") if created_dt else "—",
        "created_at_iso": created_dt.isoformat() if created_dt else None,
    }


def should_create_notification(user: User | None, notif_type: str, force: bool = False) -> bool:
    """Check if customer preference allows receiving this notification type."""
    if force or notif_type in CRITICAL_TYPES:
        return True
    if user is None:
        return True

    if notif_type == TYPE_ORDER:
        return getattr(user, "notify_order_updates", True)
    if notif_type == TYPE_CLAIM:
        return getattr(user, "notify_claim_updates", True)
    if notif_type == TYPE_ANNOUNCEMENT:
        return getattr(user, "notify_promotions", True)

    return True


def create_customer_notification(
    db: Session,
    *,
    user_id: int,
    type: str,
    title: str,
    message: str,
    reference_type: str | None = None,
    reference_id: str | None = None,
    link_url: str | None = None,
    force: bool = False,
    auto_commit: bool = True,
) -> CustomerNotification | None:
    """Create and persist an in-app customer notification safely.

    Never crashes calling transactions if an unexpected DB or network error occurs.
    """
    try:
        user = db.get(User, user_id)
        if not should_create_notification(user, type, force=force):
            logger.info("Notification %s for user %s suppressed by preferences", type, user_id)
            return None

        if reference_type and reference_id and type in (TYPE_ORDER, TYPE_CLAIM, TYPE_REFUND, TYPE_REPLACEMENT):
            existing = (
                db.query(CustomerNotification.id)
                .filter(
                    CustomerNotification.user_id == user_id,
                    CustomerNotification.type == type,
                    CustomerNotification.reference_type == reference_type,
                    CustomerNotification.reference_id == reference_id,
                    CustomerNotification.title == title[:200],
                )
                .first()
            )
            if existing:
                return None

        notif = CustomerNotification(
            user_id=user_id,
            type=type,
            title=title[:200],
            message=message,
            reference_type=reference_type[:40] if reference_type else None,
            reference_id=reference_id[:100] if reference_id else None,
            link_url=link_url[:255] if link_url else None,
            is_read=False,
            created_at=datetime.utcnow(),
        )
        db.add(notif)
        if auto_commit:
            db.commit()
            db.refresh(notif)
        else:
            db.flush()
        return notif
    except Exception as exc:
        logger.warning("Failed to create customer notification for user %s: %s", user_id, exc)
        return None


def get_customer_notifications(
    db: Session,
    user_id: int,
    *,
    limit: int = 50,
    offset: int = 0,
    unread_only: bool = False,
    type_filter: str | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Return paginated notifications list for authenticated customer, unread count, and total count."""
    base_query = db.query(CustomerNotification).filter(CustomerNotification.user_id == user_id)

    if unread_only:
        base_query = base_query.filter(CustomerNotification.is_read == False)
    if type_filter and type_filter != "all":
        base_query = base_query.filter(CustomerNotification.type == type_filter)

    total_count = base_query.count()

    unread_count = (
        db.query(func.count(CustomerNotification.id))
        .filter(CustomerNotification.user_id == user_id, CustomerNotification.is_read == False)
        .scalar()
        or 0
    )

    records = (
        base_query.order_by(CustomerNotification.id.desc())
        .offset(max(0, offset))
        .limit(min(100, max(1, limit)))
        .all()
    )

    return [serialize_notification(r) for r in records], unread_count, total_count


def get_unread_notification_count(db: Session, user_id: int) -> int:
    """Lightweight query for navigation badge counter."""
    return (
        db.query(func.count(CustomerNotification.id))
        .filter(CustomerNotification.user_id == user_id, CustomerNotification.is_read == False)
        .scalar()
        or 0
    )


def mark_notification_read(db: Session, user_id: int, notification_id: int) -> bool:
    """Mark a specific notification as read. Validates ownership."""
    notif = (
        db.query(CustomerNotification)
        .filter(CustomerNotification.id == notification_id, CustomerNotification.user_id == user_id)
        .first()
    )
    if not notif:
        return False
    if not notif.is_read:
        notif.is_read = True
        db.commit()
    return True


def mark_all_notifications_read(db: Session, user_id: int) -> int:
    """Mark all unread notifications for a customer as read."""
    updated = (
        db.query(CustomerNotification)
        .filter(CustomerNotification.user_id == user_id, CustomerNotification.is_read == False)
        .update({CustomerNotification.is_read: True}, synchronize_session=False)
    )
    db.commit()
    return updated


# ==============================================================================
# Domain Event Notification Helpers
# ==============================================================================

def notify_order_created_inapp(db: Session, order: Order, auto_commit: bool = True) -> CustomerNotification | None:
    """Notification when an order is placed and awaiting payment / processing."""
    svc_name = order.service.name if order.service else "Subscription"
    return create_customer_notification(
        db,
        user_id=order.user_id,
        type=TYPE_ORDER,
        title=f"Order Placed ({order.order_code})",
        message=f"Your order for {svc_name} (x{order.quantity}) has been placed for ${float(order.amount_usdt or 0):.2f}.",
        reference_type="order",
        reference_id=order.order_code,
        link_url=f"#orders",
        auto_commit=auto_commit,
    )


def notify_order_delivered_inapp(db: Session, order: Order, auto_commit: bool = True) -> CustomerNotification | None:
    """Notification when order credentials have been delivered / granted account assigned."""
    svc_name = order.service.name if order.service else "Subscription"
    return create_customer_notification(
        db,
        user_id=order.user_id,
        type=TYPE_ORDER,
        title=f"Credentials Delivered ({order.order_code})",
        message=f"Your subscription credentials for {svc_name} are ready! View them in Granted Accounts.",
        reference_type="order",
        reference_id=order.order_code,
        link_url="#accounts",
        force=True,  # Critical delivery event
        auto_commit=auto_commit,
    )


def notify_claim_submitted_inapp(db: Session, claim: IssueReport, auto_commit: bool = True) -> CustomerNotification | None:
    """Notification when customer submits a warranty claim."""
    claim_code = claim.claim_code or f"CLM-{claim.id}"
    svc_name = claim.service.name if getattr(claim, "service", None) else "Subscription"
    return create_customer_notification(
        db,
        user_id=claim.user_id,
        type=TYPE_CLAIM,
        title=f"Warranty Claim Submitted ({claim_code})",
        message=f"Your claim for {svc_name} has been received and is currently under review by support.",
        reference_type="claim",
        reference_id=claim_code,
        link_url="#claims",
        auto_commit=auto_commit,
    )


def notify_claim_replacement_inapp(
    db: Session,
    claim: IssueReport,
    new_account: GrantedAccount,
    auto_commit: bool = True,
) -> CustomerNotification | None:
    """Critical notification when replacement account credentials are provided."""
    claim_code = claim.claim_code or f"CLM-{claim.id}"
    svc_name = new_account.service.name if new_account.service else "Subscription"
    return create_customer_notification(
        db,
        user_id=claim.user_id,
        type=TYPE_REPLACEMENT,
        title=f"Replacement Credentials Issued ({claim_code})",
        message=f"Your warranty claim was approved. Fresh credentials for {svc_name} have been issued in Granted Accounts.",
        reference_type="granted_account",
        reference_id=str(new_account.id),
        link_url="#accounts",
        force=True,
        auto_commit=auto_commit,
    )


def notify_claim_refund_inapp(
    db: Session,
    claim: IssueReport,
    amount: float,
    method: str,
    auto_commit: bool = True,
) -> CustomerNotification | None:
    """Critical notification when pro-rata refund has been credited."""
    claim_code = claim.claim_code or f"CLM-{claim.id}"
    method_title = "Wallet" if method == "wallet" else "Manual Payment"
    link = "#wallet" if method == "wallet" else "#claims"
    return create_customer_notification(
        db,
        user_id=claim.user_id,
        type=TYPE_REFUND,
        title=f"Pro-Rata Refund Credited ({claim_code})",
        message=f"A refund of ${amount:.2f} has been processed via {method_title} for claim {claim_code}.",
        reference_type="claim",
        reference_id=claim_code,
        link_url=link,
        force=True,
        auto_commit=auto_commit,
    )


def notify_claim_support_inapp(
    db: Session,
    claim: IssueReport,
    note: str | None = None,
    auto_commit: bool = True,
) -> CustomerNotification | None:
    """Notification when support fixes issue and unfreezes account."""
    claim_code = claim.claim_code or f"CLM-{claim.id}"
    detail = f" Note: {note}" if note else ""
    return create_customer_notification(
        db,
        user_id=claim.user_id,
        type=TYPE_CLAIM,
        title=f"Support Issue Resolved ({claim_code})",
        message=f"Your claim issue has been resolved by our support team and your account is active again.{detail}",
        reference_type="claim",
        reference_id=claim_code,
        link_url="#accounts",
        force=True,
        auto_commit=auto_commit,
    )


def notify_claim_evidence_requested_inapp(
    db: Session,
    claim: IssueReport,
    note: str | None = None,
    auto_commit: bool = True,
) -> CustomerNotification | None:
    """Notification when admin requests additional evidence from customer."""
    claim_code = claim.claim_code or f"CLM-{claim.id}"
    detail = f" Admin note: {note}" if note else ""
    return create_customer_notification(
        db,
        user_id=claim.user_id,
        type=TYPE_CLAIM,
        title=f"Evidence Requested for Claim ({claim_code})",
        message=f"Support needs additional information or screenshot evidence to process your claim.{detail}",
        reference_type="claim",
        reference_id=claim_code,
        link_url="#claims",
        force=True,
        auto_commit=auto_commit,
    )


def notify_claim_rejected_inapp(
    db: Session,
    claim: IssueReport,
    reason: str,
    auto_commit: bool = True,
) -> CustomerNotification | None:
    """Notification when a claim is rejected."""
    claim_code = claim.claim_code or f"CLM-{claim.id}"
    return create_customer_notification(
        db,
        user_id=claim.user_id,
        type=TYPE_CLAIM,
        title=f"Claim Rejected ({claim_code})",
        message=f"Your claim could not be approved. Reason: {reason}",
        reference_type="claim",
        reference_id=claim_code,
        link_url="#claims",
        force=True,
        auto_commit=auto_commit,
    )


def notify_wallet_transaction_inapp(
    db: Session,
    tx: Transaction,
    auto_commit: bool = True,
) -> CustomerNotification | None:
    """Notification for wallet credit/debit/deposit."""
    raw_type = (tx.tx_type or "").lower()
    amt = float(tx.amount or 0.0)
    if raw_type == "refund":
        title = "Wallet Refund Credit"
        msg = f"Your wallet was credited with +${amt:.2f} (Refund: {tx.note or 'Order adjustment'})."
    elif raw_type in ("deposit", "admin_credit", "credit"):
        title = "Wallet Credit Received"
        msg = f"Your wallet was credited with +${amt:.2f}."
    else:
        title = "Wallet Transaction"
        msg = f"A wallet transaction of ${amt:.2f} was processed ({tx.note or raw_type})."

    return create_customer_notification(
        db,
        user_id=tx.user_id,
        type=TYPE_WALLET,
        title=title,
        message=msg,
        reference_type="transaction",
        reference_id=str(tx.id),
        link_url="#wallet",
        force=True,
        auto_commit=auto_commit,
    )


def notify_password_changed_inapp(
    db: Session,
    user_id: int,
    auto_commit: bool = True,
) -> CustomerNotification | None:
    """Security notification when password has been successfully updated."""
    return create_customer_notification(
        db,
        user_id=user_id,
        type=TYPE_SECURITY,
        title="Password Changed",
        message="Your account password was successfully updated. If you did not make this change, contact support immediately.",
        reference_type="security",
        reference_id="password_change",
        link_url="#settings",
        force=True,
        auto_commit=auto_commit,
    )


def notify_profile_updated_inapp(
    db: Session,
    user_id: int,
    auto_commit: bool = True,
) -> CustomerNotification | None:
    """Security notification when profile information has been modified."""
    return create_customer_notification(
        db,
        user_id=user_id,
        type=TYPE_SECURITY,
        title="Profile Settings Updated",
        message="Your account profile and preferences were updated.",
        reference_type="security",
        reference_id="profile_update",
        link_url="#settings",
        force=False,
        auto_commit=auto_commit,
    )
