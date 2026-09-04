"""Claims, replacement, and refund workflow service.

Handles:
- Customer claim submission with duplicate prevention and account freeze
- Evidence upload handling and validation
- Pro-rata refund processing (Wallet & Manual) with idempotency guards
- Stock-based or custom replacement credential assignment with expiry preservation
- Support resolution and unfreezing
- Customer-facing claim serialization
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, joinedload

from database.models import (
    Claim,
    GrantedAccount,
    IssueReport,
    Order,
    RefundLog,
    Service,
    Transaction,
    User,
)
from utils.granted_accounts import (
    calculate_account_refund_estimate,
    compute_account_lifecycle,
    parse_delivery_credentials,
    _format_media_url,
)
from utils.helpers import strip_html_tags
from utils.stock_manager import consume_stock_account

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def _safe_schedule_notification(coro):
    """Safely schedule an async notification task if an event loop is running, or close the coroutine if not."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        # No running event loop in this thread/context (e.g. sync unit test or background thread)
        coro.close()
    except Exception as exc:
        logger.warning("Could not schedule notification task: %s", exc)


def money(val: Any) -> Decimal:
    return Decimal(str(val or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def generate_claim_code(db: Session) -> str:
    """Generate cryptographically unique claim reference like CLM-A7K29Q."""
    for _ in range(10):
        suffix = secrets.token_hex(3).upper()
        code = f"CLM-{suffix}"
        exists = db.query(IssueReport.id).filter(IssueReport.claim_code == code).first()
        if not exists:
            return code
    return f"CLM-{int(datetime.utcnow().timestamp())}"


def get_open_claim_for_account(db: Session, granted_account_id: int) -> Optional[IssueReport]:
    """Find active/unresolved claim for an account."""
    open_statuses = [
        "pending_review",
        "pending",
        "under_review",
        "awaiting_evidence",
        "approved",
        "replacement_processing",
        "refund_processing",
        "support_in_progress",
    ]
    return (
        db.query(IssueReport)
        .filter(
            IssueReport.granted_account_id == granted_account_id,
            IssueReport.status.in_(open_statuses),
        )
        .order_by(IssueReport.id.desc())
        .first()
    )


def format_claim_payload(claim: IssueReport, request: Any = None) -> dict:
    """Serialize claim into a customer-safe and UI-ready representation."""
    raw_status = (claim.status or "pending_review").lower().strip()

    status_labels = {
        "pending_review": "Pending Review",
        "pending": "Pending Review",
        "under_review": "Under Review",
        "awaiting_evidence": "Awaiting Evidence",
        "approved": "Approved",
        "replacement_processing": "Replacement Processing",
        "refund_processing": "Refund Processing",
        "support_in_progress": "Support in Progress",
        "resolved": "Resolved",
        "rejected": "Rejected",
        "cancelled": "Cancelled",
    }
    status_badges = {
        "pending_review": "warning",
        "pending": "warning",
        "under_review": "info",
        "awaiting_evidence": "warning",
        "approved": "success",
        "replacement_processing": "purple",
        "refund_processing": "purple",
        "support_in_progress": "info",
        "resolved": "success",
        "rejected": "cancelled",
        "cancelled": "cancelled",
    }

    status_label = status_labels.get(raw_status, raw_status.replace("_", " ").title())
    status_badge = status_badges.get(raw_status, "pending")

    pref = (claim.resolution_preference or "replacement").lower()
    pref_labels = {
        "replacement": "Replacement",
        "refund": "Pro-Rata Refund",
        "support": "Support & Fix",
    }
    pref_label = pref_labels.get(pref, pref.title())

    # Linked entities
    service = getattr(claim, "service", None)
    order = getattr(claim, "order", None)
    account = getattr(claim, "granted_account", None)
    replacement = getattr(claim, "replacement_account", None)

    service_name = "Subscription Account"
    emoji_val = "🛍️"
    img_url = None
    if service:
        service_name = strip_html_tags(service.name)
        if getattr(service, "emoji", None):
            e_str = str(service.emoji).strip()
            if "|" in e_str:
                e_str = e_str.split("|", 1)[1].strip()
            emoji_val = e_str or "🛍️"
        if getattr(service, "image_path", None):
            img_url = _format_media_url(service.image_path, request)

    # Format dates
    created_dt = claim.created_at
    stopped_dt = claim.stopped_working_at
    resolved_dt = claim.resolved_at

    evidence_url_val = _format_media_url(claim.evidence_url, request) if claim.evidence_url else None

    # Account credentials preview (masked/safe)
    acc_identifier = None
    if account:
        if account.login_email and account.login_email != "Customer Account":
            acc_identifier = account.login_email
        else:
            acc_identifier = f"Account #{account.account_index + 1}"

    # Resolution outcome text
    outcome_summary = None
    if claim.resolution_type == "replacement":
        outcome_summary = "Replacement subscription credential issued."
    elif claim.resolution_type in ("refund_wallet", "refund_manual"):
        method_str = "Wallet" if "wallet" in (claim.resolution_type or "") else "Manual Refund"
        amt = float(claim.refund_amount or 0.0)
        outcome_summary = f"Pro-rata refund of ${amt:.2f} completed via {method_str}."
    elif claim.resolution_type == "support_fixed":
        outcome_summary = "Issue investigated and fixed by support team."
    elif raw_status == "rejected":
        outcome_summary = f"Claim rejected: {claim.resolution_note or 'Eligibility criteria not met.'}"

    return {
        "id": claim.id,
        "claim_code": claim.claim_code or f"CLM-{claim.id}",
        "order_id": claim.order_id,
        "order_code": claim.order_code,
        "product_name": service_name,
        "product_emoji": emoji_val,
        "product_image": img_url,
        "granted_account_id": claim.granted_account_id,
        "account_identifier": acc_identifier,
        "resolution_preference": pref,
        "resolution_preference_label": pref_label,
        "status": raw_status,
        "status_label": status_label,
        "status_badge": status_badge,
        "message": claim.message,
        "stopped_working_at": stopped_dt.strftime("%b %d, %Y") if stopped_dt else "—",
        "stopped_working_at_iso": stopped_dt.isoformat() if stopped_dt else None,
        "created_at": created_dt.strftime("%b %d, %Y %I:%M %p") if created_dt else "—",
        "created_at_iso": created_dt.isoformat() if created_dt else None,
        "resolved_at": resolved_dt.strftime("%b %d, %Y %I:%M %p") if resolved_dt else None,
        "evidence_url": evidence_url_val,
        "evidence_filename": claim.evidence_filename,
        "admin_note": claim.admin_note if raw_status == "awaiting_evidence" else None,
        "resolution_type": claim.resolution_type,
        "resolution_note": claim.resolution_note,
        "refund_amount": float(claim.refund_amount) if claim.refund_amount is not None else None,
        "refund_method": claim.refund_method,
        "replacement_account_id": claim.replacement_account_id,
        "outcome_summary": outcome_summary,
        "is_open": raw_status in ("pending_review", "pending", "under_review", "awaiting_evidence", "approved", "replacement_processing", "refund_processing", "support_in_progress"),
        "is_resolved": raw_status == "resolved",
        "is_rejected": raw_status == "rejected",
    }


def create_customer_claim(
    db: Session,
    *,
    user: User,
    granted_account: GrantedAccount,
    resolution_preference: str,
    stopped_working_at: datetime,
    problem_description: str,
    evidence_url: str | None = None,
    evidence_filename: str | None = None,
) -> IssueReport:
    """Create a new customer warranty claim and atomically freeze the account."""
    # 1. Authorization check
    if granted_account.user_id != user.id:
        raise PermissionError("You can only file claims for your own accounts.")

    # 2. Status validity check
    lifecycle = compute_account_lifecycle(granted_account)
    if lifecycle["is_expired"]:
        raise ValueError("Cannot file a claim on an expired subscription.")
    if lifecycle["is_refunded"] or (granted_account.status or "").lower() == "refunded":
        raise ValueError("Cannot file a claim on an already refunded account.")
    if (granted_account.status or "").lower() in ("replaced", "replaced_closed"):
        raise ValueError("Cannot file a claim on an account that has already been replaced.")

    # 3. Duplicate open claim guard
    existing_open = get_open_claim_for_account(db, granted_account.id)
    if existing_open:
        code_str = existing_open.claim_code or f"CLM-{existing_open.id}"
        raise ValueError(f"A claim is already in progress for this account ({code_str}).")

    # 4. Date validation
    now_utc = datetime.utcnow()
    # Ensure stopped_working_at is timezone-naive for DB comparison
    if stopped_working_at.tzinfo is not None:
        stopped_working_at = stopped_working_at.astimezone(timezone.utc).replace(tzinfo=None)

    if stopped_working_at > now_utc:
        raise ValueError("Date account stopped working cannot be in the future.")

    sub_start = granted_account.subscription_start_at or granted_account.created_at
    if sub_start:
        if sub_start.tzinfo is not None:
            sub_start = sub_start.astimezone(timezone.utc).replace(tzinfo=None)
        # Allow 2 minutes leeway for slight clock drift
        if stopped_working_at < (sub_start - timedelta(minutes=2)):
            raise ValueError("Date account stopped working cannot be before the subscription start date.")

    # 5. Problem description validation
    clean_desc = (problem_description or "").strip()
    if len(clean_desc) < 10:
        raise ValueError("Problem description must be at least 10 characters.")
    if len(clean_desc) > 2000:
        raise ValueError("Problem description cannot exceed 2000 characters.")

    # 6. Resolution preference
    valid_prefs = {"replacement", "refund", "support"}
    clean_pref = (resolution_preference or "replacement").lower().strip()
    if clean_pref not in valid_prefs:
        clean_pref = "replacement"

    # 7. Create claim
    claim_code = generate_claim_code(db)
    claim = IssueReport(
        claim_code=claim_code,
        order_id=granted_account.order_id,
        order_code=granted_account.order.order_code if granted_account.order else "ORD-UNKNOWN",
        user_id=user.id,
        granted_account_id=granted_account.id,
        service_id=granted_account.service_id,
        resolution_preference=clean_pref,
        stopped_working_at=stopped_working_at,
        message=clean_desc,
        evidence_url=evidence_url,
        evidence_filename=evidence_filename,
        status="pending_review",
        created_at=now_utc,
        updated_at=now_utc,
    )
    db.add(claim)

    # 8. Freeze account
    granted_account.status = "frozen"

    db.flush()
    db.commit()
    db.refresh(claim)
    db.refresh(granted_account)

    # 9. Send Telegram notification if user has telegram_id
    if user.telegram_id:
        try:
            from utils.notifications import notify_claim_submitted
            svc_name = granted_account.service.name if granted_account.service else "Subscription"
            _safe_schedule_notification(
                notify_claim_submitted(
                    user.telegram_id,
                    claim.claim_code,
                    svc_name,
                    claim.resolution_preference,
                )
            )
        except Exception as exc:
            logger.warning("Could not schedule claim submitted notification: %s", exc)

    return claim


def unfreeze_account(granted_account: GrantedAccount) -> str:
    """Safely restore account from frozen state back to active or expired based on lifecycle."""
    now = datetime.utcnow()
    expires_at = granted_account.subscription_expires_at
    if expires_at and expires_at > now:
        granted_account.status = "active"
    else:
        granted_account.status = "expired"
    return granted_account.status


def resolve_claim_with_replacement(
    db: Session,
    *,
    claim: IssueReport,
    replacement_credentials: str | None = None,
    auto_from_stock: bool = True,
    admin_actor: str = "admin",
    admin_note: str | None = None,
) -> tuple[GrantedAccount, str]:
    """Approve replacement: assigns credentials, preserves expiry date, closes claim."""
    if claim.status == "resolved":
        raise ValueError("Claim is already resolved.")

    old_account = claim.granted_account
    if not old_account:
        old_account = db.get(GrantedAccount, claim.granted_account_id)
    if not old_account:
        raise ValueError("Original granted account record not found.")

    # 1. Obtain replacement credentials
    cred_blocks: list[dict] = []
    source_method = "manual"

    if auto_from_stock and old_account.service_id:
        stock_lines = consume_stock_account(db, old_account.service_id, 1)
        if stock_lines and stock_lines[0]:
            cred_blocks = parse_delivery_credentials(stock_lines[0])
            source_method = "stock"

    if not cred_blocks and replacement_credentials and replacement_credentials.strip():
        cred_blocks = parse_delivery_credentials(replacement_credentials.strip())
        source_method = "manual_override"

    if not cred_blocks:
        raise ValueError("No replacement credentials available. Stock is empty and no manual credentials provided.")

    cred = cred_blocks[0]

    # 2. Preserve original subscription expiry date (Requirement 14)
    # Default to preserving the original subscription expiry date rather than giving a brand-new full subscription.
    now = datetime.utcnow()
    original_expiry = old_account.subscription_expires_at
    if original_expiry < now:
        # Fallback: if already past original expiry, give at least 1 day or remaining
        new_expiry = now + (original_expiry - (old_account.subscription_start_at or now))
        if new_expiry <= now:
            new_expiry = now + timedelta(days=old_account.duration_days or 30)
    else:
        new_expiry = original_expiry

    # 3. Create new GrantedAccount
    new_account = GrantedAccount(
        order_id=old_account.order_id,
        user_id=old_account.user_id,
        service_id=old_account.service_id,
        account_index=old_account.account_index,
        login_email=cred.get("login_email") or old_account.login_email,
        login_password=cred.get("login_password") or old_account.login_password,
        raw_credentials=cred.get("raw_credentials"),
        profile_pin=cred.get("profile_pin") or old_account.profile_pin,
        recovery_info=cred.get("recovery_info") or old_account.recovery_info,
        custom_instructions=cred.get("custom_instructions") or old_account.custom_instructions,
        account_note=f"Replacement for Account #{old_account.id} (Claim {claim.claim_code})",
        status="active",
        duration_days=old_account.duration_days,
        subscription_start_at=now,
        subscription_expires_at=new_expiry,
        replacement_for_account_id=old_account.id,
    )
    db.add(new_account)
    db.flush()

    # 4. Mark old account as replaced
    old_account.status = "replaced"
    old_account.replaced_by_account_id = new_account.id

    # 5. Resolve claim
    claim.status = "resolved"
    claim.resolution_type = "replacement"
    claim.replacement_account_id = new_account.id
    claim.resolved_at = now
    claim.updated_at = now
    note_msg = (admin_note or "").strip() or f"Replacement account credentials issued from {source_method}."
    claim.resolution_note = note_msg

    db.commit()
    db.refresh(claim)
    db.refresh(new_account)
    db.refresh(old_account)

    # 6. Telegram notification
    user = claim.user or db.get(User, claim.user_id)
    if user and user.telegram_id:
        try:
            from utils.notifications import notify_claim_approved_replacement
            svc_name = old_account.service.name if old_account.service else "Subscription"
            _safe_schedule_notification(
                notify_claim_approved_replacement(
                    user.telegram_id,
                    claim.claim_code,
                    svc_name,
                )
            )
        except Exception as exc:
            logger.warning("Could not send replacement notification: %s", exc)

    return new_account, source_method


def resolve_claim_with_refund(
    db: Session,
    *,
    claim: IssueReport,
    refund_method: str = "wallet",
    amount_override: float | None = None,
    admin_actor: str = "admin",
    admin_note: str | None = None,
) -> dict:
    """Process pro-rata refund for a claim. Double-refund guarded & atomic."""
    # 1. Idempotency guard: never refund twice
    if claim.status == "resolved" or claim.refund_amount is not None:
        raise ValueError("Claim has already been resolved / refunded.")

    account = claim.granted_account or db.get(GrantedAccount, claim.granted_account_id)
    if not account:
        raise ValueError("Granted account for claim not found.")
    if (account.status or "").lower() == "refunded":
        raise ValueError("Account has already been refunded.")

    order = claim.order or db.get(Order, claim.order_id)
    if not order:
        raise ValueError("Order record not found.")

    user = claim.user or db.get(User, claim.user_id)
    if not user:
        raise ValueError("User record not found.")

    # 2. Authoritative server-side refund calculation (Phase 4)
    estimate = calculate_account_refund_estimate(account, order)
    if amount_override is not None:
        amt = money(amount_override)
        if amt <= ZERO:
            raise ValueError("Refund amount must be greater than zero.")
    else:
        amt = money(estimate.get("estimated_refund", 0.0))
        if amt <= ZERO:
            # If 0 days remaining, refund minimal or error
            amt = money(estimate.get("amount_paid", 0.0))

    clean_method = "wallet" if refund_method == "wallet" else "manual"
    now = datetime.utcnow()

    # 3. Double-credit guard on Wallet
    if clean_method == "wallet":
        existing_tx = (
            db.query(Transaction.id)
            .filter(
                Transaction.user_id == user.id,
                Transaction.tx_type == "refund",
                Transaction.note.like(f"%{claim.claim_code}%"),
            )
            .first()
        )
        if existing_tx:
            raise ValueError("A wallet refund transaction has already been recorded for this claim.")

        before = float(money(user.wallet_usdt))
        after = float(money(before + float(amt)))
        user.wallet_usdt = after

        tx = Transaction(
            user_id=user.id,
            amount=float(amt),
            tx_type="refund",
            status="confirmed",
            blockchain_status="confirmed",
            note=f"Pro-rata refund for claim {claim.claim_code} ({order.order_code})",
        )
        db.add(tx)

    # 4. Mark account and order refunded
    account.status = "refunded"
    order.status = "refunded"
    order.refund_method = clean_method
    order.refund_amount = float(money((order.refund_amount or 0.0) + float(amt)))
    order.refunded_at = now

    # 5. Write audit RefundLog
    total_days = estimate.get("total_days", account.duration_days or 30)
    days_used = estimate.get("days_used", 0)
    days_remaining = estimate.get("days_remaining", 0)

    log = RefundLog(
        order_id=order.id,
        order_code=order.order_code,
        admin_name=admin_actor or "admin",
        refund_amount=float(amt),
        refund_method=clean_method,
        days_total=int(total_days),
        days_used=int(days_used),
        days_remaining=int(days_remaining),
        note=admin_note or f"Claim {claim.claim_code} refund",
    )
    db.add(log)

    # 6. Mark claim resolved
    claim.status = "resolved"
    claim.resolution_type = f"refund_{clean_method}"
    claim.refund_amount = float(amt)
    claim.refund_method = clean_method
    claim.resolved_at = now
    claim.updated_at = now
    claim.resolution_note = (admin_note or "").strip() or f"Pro-rata refund of ${float(amt):.2f} processed via {clean_method.title()}."

    db.commit()
    db.refresh(claim)
    db.refresh(account)
    db.refresh(order)
    db.refresh(user)

    # 7. Telegram notification
    if user.telegram_id:
        try:
            from utils.notifications import notify_claim_refunded
            _safe_schedule_notification(
                notify_claim_refunded(
                    user.telegram_id,
                    claim.claim_code,
                    order.order_code,
                    float(amt),
                    clean_method,
                    new_wallet_balance=float(user.wallet_usdt) if clean_method == "wallet" else None,
                    note=claim.resolution_note,
                )
            )
        except Exception as exc:
            logger.warning("Could not send refund notification: %s", exc)

    return {
        "ok": True,
        "refund_amount": float(amt),
        "refund_method": clean_method,
        "new_wallet_balance": float(user.wallet_usdt),
        "claim_code": claim.claim_code,
    }


def resolve_claim_with_support_fix(
    db: Session,
    *,
    claim: IssueReport,
    resolution_note: str | None = None,
    admin_actor: str = "admin",
) -> IssueReport:
    """Mark support issue resolved and unfreeze the original account."""
    if claim.status == "resolved":
        raise ValueError("Claim is already resolved.")

    account = claim.granted_account or db.get(GrantedAccount, claim.granted_account_id)
    if account:
        unfreeze_account(account)

    now = datetime.utcnow()
    claim.status = "resolved"
    claim.resolution_type = "support_fixed"
    claim.resolved_at = now
    claim.updated_at = now
    note_msg = (resolution_note or "").strip() or "Issue investigated and fixed by support team."
    claim.resolution_note = note_msg

    db.commit()
    db.refresh(claim)
    if account:
        db.refresh(account)

    user = claim.user or db.get(User, claim.user_id)
    if user and user.telegram_id:
        try:
            from utils.notifications import notify_claim_resolved_support
            svc_name = account.service.name if account and account.service else "Subscription"
            _safe_schedule_notification(
                notify_claim_resolved_support(
                    user.telegram_id,
                    claim.claim_code,
                    svc_name,
                    note=note_msg,
                )
            )
        except Exception as exc:
            logger.warning("Could not send support resolved notification: %s", exc)

    return claim


def reject_claim(
    db: Session,
    *,
    claim: IssueReport,
    reason: str,
    admin_actor: str = "admin",
) -> IssueReport:
    """Reject claim, unfreeze account, and record reason."""
    if claim.status == "resolved":
        raise ValueError("Claim is already resolved.")

    account = claim.granted_account or db.get(GrantedAccount, claim.granted_account_id)
    if account:
        unfreeze_account(account)

    now = datetime.utcnow()
    claim.status = "rejected"
    claim.resolved_at = now
    claim.updated_at = now
    note_msg = (reason or "").strip() or "Claim did not meet replacement or refund policy criteria."
    claim.resolution_note = note_msg

    db.commit()
    db.refresh(claim)
    if account:
        db.refresh(account)

    user = claim.user or db.get(User, claim.user_id)
    if user and user.telegram_id:
        try:
            from utils.notifications import notify_claim_rejected
            svc_name = account.service.name if account and account.service else "Subscription"
            _safe_schedule_notification(
                notify_claim_rejected(
                    user.telegram_id,
                    claim.claim_code,
                    svc_name,
                    reason=note_msg,
                )
            )
        except Exception as exc:
            logger.warning("Could not send rejection notification: %s", exc)

    return claim


def request_claim_evidence(
    db: Session,
    *,
    claim: IssueReport,
    note: str,
    admin_actor: str = "admin",
) -> IssueReport:
    """Mark claim as awaiting evidence and notify customer."""
    claim.status = "awaiting_evidence"
    claim.admin_note = (note or "").strip()
    claim.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(claim)

    user = claim.user or db.get(User, claim.user_id)
    if user and user.telegram_id:
        try:
            from utils.notifications import notify_claim_evidence_requested
            account = claim.granted_account
            svc_name = account.service.name if account and account.service else "Subscription"
            _safe_schedule_notification(
                notify_claim_evidence_requested(
                    user.telegram_id,
                    claim.claim_code,
                    svc_name,
                    note=claim.admin_note,
                )
            )
        except Exception as exc:
            logger.warning("Could not send evidence requested notification: %s", exc)

    return claim
