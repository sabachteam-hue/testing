"""Shared payment credit safety checks.

Rules enforced across PayFast + TXID deposits:
1. Credit only the user who owns that checkout/transaction row.
2. Never reuse the same payment reference / TXID on another deposit.
3. Never credit wallet / fulfill an order without a verified successful payment
   (or an explicit admin confirmation that still passes duplicate checks).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from database.models import PaymentVerification, Transaction

logger = logging.getLogger(__name__)

# After this many failed verification attempts in the window below, stop
# calling the blockchain verifier again for this user and tell them plainly
# to contact admin instead of a soft "pending, we'll notify you" - that message
# invited repeated guesses (e.g. trying case variants or made-up hashes hoping
# one slips through).
MAX_FAILED_VERIFICATIONS = 3
FAILED_VERIFICATION_WINDOW_MINUTES = 30


def recent_failed_verification_count(db: Session, user_id: int, *, minutes: int = FAILED_VERIFICATION_WINDOW_MINUTES) -> int:
    """How many payment verification attempts by this user have failed recently."""
    cutoff = datetime.utcnow() - timedelta(minutes=minutes)
    return (
        db.query(PaymentVerification)
        .join(Transaction, Transaction.id == PaymentVerification.transaction_id)
        .filter(
            Transaction.user_id == user_id,
            PaymentVerification.verification_status.notin_(("verified", "confirmed")),
            PaymentVerification.created_at >= cutoff,
        )
        .count()
    )

_PAYFAST_BASKET = re.compile(r"^SMFSHOP(\d+)$", re.IGNORECASE)


def normalize_payment_ref(value: str | None) -> str:
    """Canonicalize a TXID / payment reference.

    On-chain tx hash lookups (BEP20/TRC20) are case-insensitive and tolerant of
    an optional '0x' prefix - so without normalizing here, the SAME real
    transaction resubmitted with different case (or with/without '0x') looks
    like a brand-new reference to the duplicate check, gets re-verified against
    the chain, and gets credited to the wallet again. Canonicalizing to one
    lowercase '0x'-prefixed form for 32-byte hex hashes closes that hole.
    """
    ref = (value or "").strip()
    if not ref:
        return ref
    lowered = ref.lower()
    hex_part = lowered[2:] if lowered.startswith("0x") else lowered
    if len(hex_part) == 64 and all(c in "0123456789abcdef" for c in hex_part):
        return "0x" + hex_part
    return lowered


def payment_ref_already_used(
    db: Session,
    ref: str,
    *,
    exclude_transaction_id: int | None = None,
) -> Transaction | None:
    """Return an existing deposit that already used this TXID/reference.

    Looks at Transaction.tx_hash and PaymentVerification.tx_hash for any
    non-rejected/non-expired row so the same on-chain / PayFast id cannot
    credit two different users (or the same user twice).
    """
    ref = normalize_payment_ref(ref)
    if not ref:
        return None

    blocked_statuses = ("rejected", "expired", "failed")
    q = (
        db.query(Transaction)
        .filter(
            Transaction.tx_type == "deposit",
            Transaction.tx_hash == ref,
            Transaction.status.notin_(blocked_statuses),
        )
    )
    if exclude_transaction_id is not None:
        q = q.filter(Transaction.id != exclude_transaction_id)
    hit = q.first()
    if hit:
        return hit

    vq = (
        db.query(PaymentVerification)
        .join(Transaction, Transaction.id == PaymentVerification.transaction_id)
        .filter(
            PaymentVerification.tx_hash == ref,
            Transaction.tx_type == "deposit",
            Transaction.status.notin_(blocked_statuses),
            PaymentVerification.verification_status.in_(("verified", "pending", "confirmed")),
        )
    )
    if exclude_transaction_id is not None:
        vq = vq.filter(Transaction.id != exclude_transaction_id)
    verification = vq.first()
    if verification:
        return db.get(Transaction, verification.transaction_id)
    return None


def expected_payfast_basket_id(tx_id: int) -> str:
    return f"SMFSHOP{tx_id}"


def payfast_callback_matches_tx(
    tx: Transaction,
    *,
    order_id: str,
    basket_id: str,
) -> tuple[bool, str]:
    """Ensure the PayFast callback is bound to THIS checkout row only."""
    if not order_id or not str(order_id).isdigit() or int(order_id) != tx.id:
        return False, "order_id does not match transaction"
    expected = expected_payfast_basket_id(tx.id)
    if (basket_id or "").strip().upper() != expected.upper():
        return False, f"basket_id mismatch (got {basket_id!r}, expected {expected!r})"
    match = _PAYFAST_BASKET.match((basket_id or "").strip())
    if not match or int(match.group(1)) != tx.id:
        return False, "basket_id does not encode this transaction id"
    return True, ""


def can_apply_payfast_success(tx: Transaction) -> tuple[bool, str]:
    """Only unpaid PayFast checkouts may be credited/fulfilled from a success callback."""
    if tx.tx_type != "deposit":
        return False, "not a deposit"
    if tx.verified_at is not None:
        return False, "already credited"
    if tx.status == "confirmed":
        return False, "already confirmed"
    if tx.status not in {"pending", "expired"}:
        return False, f"status {tx.status} cannot receive payment credit"
    return True, ""


def assert_order_owned_by_tx(order, tx: Transaction) -> tuple[bool, str]:
    if order is None:
        return False, "order missing"
    if int(order.user_id) != int(tx.user_id):
        logger.error(
            "[PAYMENT-SECURITY] Order user mismatch order=%s order_user=%s tx=%s tx_user=%s",
            getattr(order, "id", None),
            order.user_id,
            tx.id,
            tx.user_id,
        )
        return False, "order does not belong to paying user"
    return True, ""
