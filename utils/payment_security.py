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
import secrets
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
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

# ---------------------------------------------------------------------------
# Secure, random, customer-facing PayFast reference (e.g. "SMFSHOP-A7K29Q").
#
# The OLD basket_id ("SMFSHOP<transaction_id>") was just the row's primary
# key, so it was sequential and trivially guessable/enumerable. This new
# reference is generated with `secrets` (CSPRNG), is never derived from the
# row id, and is enforced unique at the database level (see
# migrate_payfast_reference.py). Customers paste this value back to the bot
# to look up a PayFast payment — see `lookup_payfast_reference` below and
# api/payfast.py's `verify_payfast_reference`.
# ---------------------------------------------------------------------------
PAYFAST_REFERENCE_PREFIX = "SMFSHOP-"
# Unambiguous alphabet (no 0/O/1/I) to avoid customer transcription mistakes.
_PAYFAST_REF_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_PAYFAST_REF_LEN = 8
PAYFAST_REFERENCE_RE = re.compile(r"^SMFSHOP-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{6,10}$")


def generate_payfast_reference() -> str:
    """Cryptographically secure random reference — never Math.random()/incremental."""
    body = "".join(secrets.choice(_PAYFAST_REF_ALPHABET) for _ in range(_PAYFAST_REF_LEN))
    return f"{PAYFAST_REFERENCE_PREFIX}{body}"


def normalize_payfast_reference(value: str | None) -> str:
    ref = (value or "").strip().upper()
    # Tolerate customers pasting without the "SMFSHOP-" prefix or with the
    # legacy no-dash spacing; still requires the full random body to match.
    if ref and not ref.startswith(PAYFAST_REFERENCE_PREFIX) and ref.startswith("SMFSHOP"):
        rest = ref[len("SMFSHOP"):].lstrip("-")
        ref = f"{PAYFAST_REFERENCE_PREFIX}{rest}"
    return ref


def assign_payfast_reference(db: Session, tx: Transaction) -> str:
    """Generate + persist a unique payfast_reference for this transaction.

    Idempotent: if the transaction already has one (e.g. customer reloaded
    the checkout page), the existing value is reused instead of issuing a
    new one. Retries on a DB-level unique-constraint collision (belt AND
    braces on top of the CSPRNG's astronomically low collision odds) so two
    concurrent checkouts can never be assigned the same reference.
    """
    if tx.payfast_reference:
        return tx.payfast_reference

    for _ in range(8):
        candidate = generate_payfast_reference()
        tx.payfast_reference = candidate
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            # Re-attach tx to the (rolled-back) session state isn't needed here
            # because the caller commits separately after this returns; just
            # retry with a fresh candidate.
            tx.payfast_reference = None
            continue
        return candidate
    raise RuntimeError("Could not generate a unique PayFast reference after several attempts")


def payfast_basket_id_for_tx(tx: Transaction) -> str:
    """Basket id sent to PayFast — the secure reference, not the row id."""
    if not tx.payfast_reference:
        raise ValueError("Transaction has no payfast_reference assigned yet")
    return tx.payfast_reference


# ---------------------------------------------------------------------------
# Rate limiting for manual "paste your PayFast Order ID" lookups. Kept
# separate from MAX_FAILED_VERIFICATIONS (crypto TXID limiter) since this
# guards a *lookup* (find-by-reference), not a specific transaction's retry
# count — a malicious customer could otherwise try many random-looking
# SMFSHOP-XXXXXX strings hoping to hit someone else's real reference.
# In-memory (per-process) — consistent with the existing ApiKey rate
# limiter in api/__init__.py; fine for this bot's single-instance deploys.
# ---------------------------------------------------------------------------
MAX_PAYFAST_LOOKUPS = 8
PAYFAST_LOOKUP_WINDOW_MINUTES = 10
_payfast_lookup_attempts: dict[int, list[datetime]] = defaultdict(list)


def payfast_lookup_rate_limited(user_telegram_id: int | str) -> bool:
    key = int(user_telegram_id) if str(user_telegram_id).lstrip("-").isdigit() else hash(user_telegram_id)
    cutoff = datetime.utcnow() - timedelta(minutes=PAYFAST_LOOKUP_WINDOW_MINUTES)
    attempts = [ts for ts in _payfast_lookup_attempts[key] if ts >= cutoff]
    _payfast_lookup_attempts[key] = attempts
    if len(attempts) >= MAX_PAYFAST_LOOKUPS:
        return True
    attempts.append(datetime.utcnow())
    return False


def claim_payfast_user_notification(db: Session, tx_id: int) -> bool:
    """Atomically claim the single right to send the user a Telegram message
    about this transaction's outcome.

    Two independent code paths can both end up wanting to tell the customer
    "your payment is confirmed" for the SAME transaction: the authenticated
    PayFast webhook's own push notification, and the bot's "paste your Order
    ID" status check / background poll loop reporting the same already-
    resolved state. Both call this first — whichever call lands first sets
    `user_notified_at` and gets `True` (send your message); the other sees it
    already set and gets `False` (stay silent), so the customer only ever
    sees one message no matter which side wins the race.

    A plain conditional UPDATE (not a row lock + read-modify-write) so it's
    safe under real concurrent access from two separate requests/tasks.
    """
    from sqlalchemy import text as _text

    result = db.execute(
        _text(
            "UPDATE transactions SET user_notified_at = :now "
            "WHERE id = :tx_id AND user_notified_at IS NULL"
        ),
        {"now": datetime.utcnow(), "tx_id": tx_id},
    )
    db.commit()
    return result.rowcount > 0


# In-flight "I Have Paid" Checking... bubble (chat_id, message_id), keyed by
# telegram_id. The PayFast webhook takes this so it can edit that same
# message into the confirmation (or delete it) the instant payment lands —
# otherwise the poll loop would leave Checking... on screen until it wakes.
_payfast_checking_messages: dict[str, tuple[int, int]] = {}


def register_payfast_checking_message(telegram_id: str | int, chat_id: int, message_id: int) -> None:
    _payfast_checking_messages[str(telegram_id)] = (int(chat_id), int(message_id))


def take_payfast_checking_message(telegram_id: str | int) -> tuple[int, int] | None:
    """Pop the in-memory Checking... bubble so only one side edits or deletes it."""
    return _payfast_checking_messages.pop(str(telegram_id), None)


def save_payfast_checking_on_tx(tx, *, chat_id: int, message_id: int) -> None:
    tx.payfast_check_chat_id = str(chat_id)
    tx.payfast_check_message_id = int(message_id)


def take_payfast_checking_from_tx(tx) -> tuple[int, int] | None:
    """Pop the Checking... bubble stored on the checkout row (survives workers)."""
    chat_id = getattr(tx, "payfast_check_chat_id", None)
    message_id = getattr(tx, "payfast_check_message_id", None)
    if not chat_id or not message_id:
        return None
    tx.payfast_check_chat_id = None
    tx.payfast_check_message_id = None
    try:
        return int(chat_id), int(message_id)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Fallback lookup key: PayFast's own Transaction ID (shown on PayFast's page
# only AFTER a payment completes), as an alternative to our own Order No.
# for customers who didn't copy the Order No. beforehand. Read-only, same as
# the Order No. lookup — never used to credit/confirm anything by itself.
# ---------------------------------------------------------------------------
PAYFAST_TID_RE = re.compile(r"^[a-z0-9._-]{4,40}$")


def looks_like_payfast_tid(normalized_value: str) -> bool:
    return bool(normalized_value) and bool(PAYFAST_TID_RE.match(normalized_value))


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
    """Ensure the PayFast callback is bound to THIS checkout row only.

    order_id is our own internal correlation param (not customer-supplied —
    it's embedded in the checkout URL we generate), so tying it to tx.id is
    fine. basket_id is the customer-facing reference and must match the
    secure random value we assigned at checkout time — NOT be recomputed
    from tx.id (that old "SMFSHOP<id>" scheme was sequential/guessable).
    """
    if not order_id or not str(order_id).isdigit() or int(order_id) != tx.id:
        return False, "order_id does not match transaction"
    expected = tx.payfast_reference or expected_payfast_basket_id(tx.id)
    if (basket_id or "").strip().upper() != expected.upper():
        return False, f"basket_id mismatch (got {basket_id!r}, expected {expected!r})"
    return True, ""


def can_apply_payfast_success(tx: Transaction) -> tuple[bool, str]:
    """Only unpaid PayFast checkouts may be credited/fulfilled from a success callback.

    "rejected" is included alongside "pending"/"expired" on purpose: PayFast
    sometimes fires an early failure ping (e.g. the customer's session/page
    timing out) BEFORE the real, authenticated success IPN lands a moment
    later for the same payment. If we only ever recover from "expired", that
    genuine later success would be silently dropped once the tx was marked
    rejected. verified_at is the actual double-credit guard here (it is only
    ever set once, by the branch that applies a verified success) — status
    alone is just where a checkout currently sits, not proof money was never
    actually received.
    """
    if tx.tx_type != "deposit":
        return False, "not a deposit"
    if tx.verified_at is not None:
        return False, "already credited"
    if tx.status == "confirmed":
        return False, "already confirmed"
    if tx.status not in {"pending", "expired", "rejected"}:
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
