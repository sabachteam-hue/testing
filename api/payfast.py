import logging
import re
from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from database.models import BotConfig, Order, PaymentVerification, SessionLocal, Transaction
from utils.background_tasks import credit_referral_join_bonus
from utils.checkout_expire import (
    SESSION_CLOSED_HTML,
    expire_unpaid_checkout_tx,
    linked_order_id_from_tx,
)
from utils.notifications import (
    maybe_send_delivery_file,
    notify_admin_new_order,
    notify_channel_order_completed,
    notify_referrer_earning,
    stock_note_text,
)
from utils.payfast import PayFastConfig, build_checkout_html, get_payfast_token, validate_callback_hash
from utils.payment_security import (
    assert_order_owned_by_tx,
    assign_payfast_reference,
    can_apply_payfast_success,
    claim_payfast_user_notification,
    looks_like_payfast_tid,
    normalize_payfast_reference,
    normalize_payment_ref,
    payfast_callback_matches_tx,
    payfast_lookup_rate_limited,
    payment_ref_already_used,
    take_payfast_checking_message,
)
from utils.stock_manager import release_stock

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pay/payfast", tags=["payfast"])


def _get_payfast_config(config: BotConfig) -> PayFastConfig | None:
    if not config or not config.payfast_merchant_id or not config.payfast_secured_key:
        return None
    return PayFastConfig(
        merchant_id=config.payfast_merchant_id,
        secured_key=config.payfast_secured_key,
        base_url=config.payfast_base_url or "https://ipg2.apps.net.pk",
        store_id=config.payfast_store_id or "",
    )


def _public_base_url(request: Request) -> str:
    import os

    base_url = os.getenv("WEBHOOK_URL") or os.getenv("RAILWAY_PUBLIC_DOMAIN")
    if base_url:
        if not base_url.startswith("http"):
            base_url = f"https://{base_url}"
        return base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


@router.get("/checkout/{transaction_id}", response_class=HTMLResponse)
async def payfast_checkout(transaction_id: int, request: Request) -> HTMLResponse:
    db = SessionLocal()
    try:
        tx = db.get(Transaction, transaction_id)
        if not tx:
            return HTMLResponse("<h3>This payment link is no longer valid.</h3>", status_code=400)

        # Lazy expire: if the customer opens the link after the expire window,
        # free stock immediately instead of waiting for the background job.
        if tx.status == "pending" and expire_unpaid_checkout_tx(db, tx):
            db.commit()

        if tx.status == "expired":
            return HTMLResponse(SESSION_CLOSED_HTML, status_code=410)
        if tx.status != "pending":
            return HTMLResponse("<h3>This payment link is no longer valid.</h3>", status_code=400)

        config = db.query(BotConfig).first()
        pf_config = _get_payfast_config(config)
        if not pf_config:
            return HTMLResponse("<h3>PayFast is not configured. Please contact support.</h3>", status_code=500)

        pkr_amount = round(tx.amount * (config.usd_to_pkr_rate or 280.0), 2)
        # Secure, random, DB-unique reference — NOT derived from tx.id (that
        # old scheme let anyone enumerate "SMFSHOP1", "SMFSHOP2", ...).
        # Idempotent: reused as-is if the customer reloads this checkout page.
        basket_id = assign_payfast_reference(db, tx)
        db.commit()
        order_date = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        try:
            token = await get_payfast_token(pf_config, pkr_amount, basket_id)
        except Exception:  # noqa: BLE001
            logger.exception("[PAYFAST] Unexpected error while getting access token for tx=%s", tx.id)
            token = None

        if not token:
            return HTMLResponse(
                "<h3>Could not connect to PayFast right now. Please try again shortly, "
                "or contact support if this keeps happening.</h3>",
                status_code=502,
            )

        base_url = _public_base_url(request)
        success_url = f"{base_url}/pay/payfast/callback?redirect=Y&order_id={tx.id}"
        failure_url = success_url
        callback_url = f"{base_url}/pay/payfast/callback?order_id={tx.id}"

        linked_id = linked_order_id_from_tx(tx)
        linked_order = db.get(Order, linked_id) if linked_id else None

        # PayFast's own dashboard only ever shows what we send it here — until
        # now that was just "Order SMM-XXXXXXXX" / "Wallet top-up #123" with
        # no name, username, or email attached. That made it impossible to
        # tell which Telegram client a given expired/failed PayFast payment
        # belonged to without pulling up this bot's DB. Put the Telegram
        # username (or full name / telegram id as a fallback) directly in the
        # description PayFast displays, and pass along a real customer email
        # when the order collected one, so admin can match a PayFast entry to
        # a client at a glance.
        customer = tx.user
        if customer and customer.username:
            customer_ref = f"@{customer.username}"
        elif customer and customer.full_name:
            customer_ref = customer.full_name
        elif customer:
            customer_ref = f"tg:{customer.telegram_id}"
        else:
            customer_ref = None

        description = f"Order {linked_order.order_code}" if linked_order else f"Wallet top-up #{tx.id}"
        if customer_ref:
            description = f"{description} | {customer_ref}"
        # PayFast's TXNDESC has a practical length limit — keep it short and safe.
        description = description[:100]

        customer_email = (linked_order.customer_email if linked_order else None) or ""

        try:
            html_page = build_checkout_html(
                pf_config,
                token=token,
                amount=pkr_amount,
                basket_id=basket_id,
                order_id=str(tx.id),
                order_date=order_date,
                store_name="SMF SHOP",
                description=description,
                customer_mobile="",
                customer_email=customer_email,
                success_url=success_url,
                failure_url=failure_url,
                callback_url=callback_url,
            )
        except Exception:  # noqa: BLE001
            logger.exception("[PAYFAST] Unexpected error while building checkout HTML for tx=%s", tx.id)
            return HTMLResponse("<h3>Something went wrong preparing your payment. Please try again.</h3>", status_code=500)

        return HTMLResponse(html_page)
    finally:
        db.close()


async def _collect_payfast_params(request: Request) -> dict[str, str]:
    """PayFast sends two very different kinds of callback here:

    1. The browser's redirect after checkout (GET, our own SUCCESS_URL/
       FAILURE_URL) — params arrive as normal URL query string.
    2. The real server-to-server IPN webhook (POST) — this is the one that
       actually confirms/rejects the payment, and PayFast posts it as an
       `application/x-www-form-urlencoded` BODY, using the SAME uppercase
       field names as their hosted-checkout form (BASKET_ID, ERR_CODE,
       VALIDATION_HASH, TRANSACTION_ID, ...), not lowercase query params.

    Declaring the route params as FastAPI `Query(...)` only ever reads the
    URL query string, so the POST webhook's form body was silently ignored
    — every field came through empty, the hash never matched, and PayFast
    got 401 back for every real payment. Reading BOTH sources here, merged
    case-insensitively, fixes that for good regardless of which casing
    PayFast uses on a given call.
    """
    merged: dict[str, str] = {}
    for key, value in request.query_params.items():
        merged[key.lower()] = value
    if request.method == "POST":
        try:
            form = await request.form()
        except Exception:  # noqa: BLE001 - not form-encoded, nothing to add
            form = {}
        for key, value in form.items():
            merged[key.lower()] = str(value)
    return merged


@router.api_route("/callback", methods=["GET", "POST"])
async def payfast_callback(request: Request):
    params = await _collect_payfast_params(request)
    order_id = params.get("order_id", "")
    redirect = params.get("redirect", "")
    basket_id = params.get("basket_id", "")
    err_code = params.get("err_code", "")
    err_msg = params.get("err_msg", "")
    transaction_id = params.get("transaction_id", "")
    validation_hash = params.get("validation_hash", "")

    db = SessionLocal()
    try:
        config = db.query(BotConfig).first()
        pf_config = _get_payfast_config(config)
        if not pf_config or not validate_callback_hash(basket_id, err_code, validation_hash, pf_config):
            logger.warning("[PAYFAST] Rejected callback with invalid hash: order_id=%s err_code=%s", order_id, err_code)
            if redirect == "Y":
                return HTMLResponse("<h3>This payment could not be verified.</h3>", status_code=400)
            return PlainTextResponse("Unauthorized (invalid hash)", status_code=401)

        # PayFast fires TWO callbacks for the same payment — the browser's
        # redirect (?redirect=Y) AND a separate server-to-server webhook —
        # often within milliseconds of each other. Without a row lock here,
        # both requests can read tx.status == "pending" before either has
        # committed, so both fall through and fulfil the SAME order — which
        # is exactly what sent the same order_code to the provider twice and
        # got "Duplicate external order id" back. with_for_update makes the
        # second callback block until the first commits, so it then sees the
        # already-confirmed status below and exits via the idempotent-replay
        # check instead of fulfilling a second time.
        tx = db.get(Transaction, int(order_id), with_for_update=True) if order_id.isdigit() else None
        if not tx:
            if redirect == "Y":
                return HTMLResponse("<h3>Transaction not found.</h3>", status_code=404)
            return PlainTextResponse("Transaction not found", status_code=404)

        bound_ok, bound_reason = payfast_callback_matches_tx(tx, order_id=order_id, basket_id=basket_id)
        if not bound_ok:
            logger.warning(
                "[PAYFAST] Rejected unbound callback tx=%s order_id=%s basket_id=%s reason=%s",
                tx.id,
                order_id,
                basket_id,
                bound_reason,
            )
            if redirect == "Y":
                return HTMLResponse("<h3>This payment could not be matched to your checkout.</h3>", status_code=400)
            return PlainTextResponse("Basket/order mismatch", status_code=400)

        # Idempotent replay: already credited this checkout — never credit again.
        if tx.status == "confirmed" and tx.verified_at is not None:
            if redirect == "Y":
                return HTMLResponse("<h3>✅ Payment already confirmed. You can return to Telegram.</h3>")
            return PlainTextResponse("OK", status_code=200)

        success = err_code == "000"
        payfast_tid = normalize_payment_ref(transaction_id)

        # Success credits require PayFast's own transaction id (not just basket_id).
        if success and not payfast_tid:
            logger.warning("[PAYFAST] Success callback missing transaction_id for tx=%s", tx.id)
            if redirect == "Y":
                return HTMLResponse("<h3>Payment response incomplete. Contact support with your receipt.</h3>", status_code=400)
            return PlainTextResponse("Missing PayFast transaction_id", status_code=400)

        if success:
            duplicate = payment_ref_already_used(db, payfast_tid, exclude_transaction_id=tx.id)
            if duplicate:
                logger.warning(
                    "[PAYFAST] Rejected reused PayFast TXID=%s on tx=%s (already used by tx=%s user=%s)",
                    payfast_tid,
                    tx.id,
                    duplicate.id,
                    duplicate.user_id,
                )
                if redirect == "Y":
                    return HTMLResponse(
                        "<h3>This PayFast payment was already used on another deposit.</h3>",
                        status_code=409,
                    )
                return PlainTextResponse("Duplicate PayFast transaction_id", status_code=409)

        # Late/recovered payment: credit ONLY this checkout's user wallet in
        # either of two cases where a real success arrives after the fact —
        # 1) "expired": idle timeout passed before payment completed.
        # 2) "rejected": an earlier failure ping (e.g. a premature timeout on
        #    PayFast's own payment screen) already marked this tx rejected,
        #    and PayFast's real, authenticated success IPN is only landing
        #    now for the same payment. Never re-fulfil the original order
        #    here (it may already be cancelled/stock-released) — wallet
        #    credit is the safe, idempotent recovery in both cases.
        if tx.status in {"expired", "rejected"} and success:
            was_rejected = tx.status == "rejected"
            user_message = _credit_verified_payfast_to_wallet(
                db,
                tx,
                payfast_tid=payfast_tid,
                basket_id=basket_id,
                err_msg=err_msg,
                late=True,
            )
            db.commit()
            await _notify_payfast_user(request, db, tx, user_message, show_products_button=True)
            if redirect == "Y":
                if was_rejected:
                    return HTMLResponse(
                        "<h3>✅ Payment received.</h3>"
                        "<p>This checkout was earlier reported as failed, but your payment was "
                        "actually confirmed by PayFast — the amount was credited to "
                        "<strong>your</strong> wallet. Please place a new order from Telegram.</p>"
                    )
                return HTMLResponse(
                    "<h3>✅ Payment received.</h3>"
                    "<p>The original checkout had already expired, so the amount was credited to "
                    "<strong>your</strong> wallet only. Please place a new order from Telegram.</p>"
                )
            return PlainTextResponse("OK", status_code=200)

        user_message = None
        if tx.status == "pending":
            # Expire first if the window already passed (callback race with timer).
            if expire_unpaid_checkout_tx(db, tx):
                db.commit()
                if success:
                    user_message = _credit_verified_payfast_to_wallet(
                        db,
                        tx,
                        payfast_tid=payfast_tid,
                        basket_id=basket_id,
                        err_msg=err_msg,
                        late=True,
                    )
                    db.commit()
                await _notify_payfast_user(request, db, tx, user_message, show_products_button=success)
                if redirect == "Y":
                    if success:
                        return HTMLResponse(
                            "<h3>✅ Payment received.</h3>"
                            "<p>Checkout had expired; amount credited to your wallet only. "
                            "Start a new order in Telegram.</p>"
                        )
                    return HTMLResponse(SESSION_CLOSED_HTML)
                return PlainTextResponse("OK", status_code=200)

            verification = PaymentVerification(
                transaction_id=tx.id,
                tx_hash=payfast_tid or basket_id,
                blockchain="PAYFAST",
                to_address="PAYFAST",
                amount_verified=tx.amount,
                verification_status="verified" if success else "failed",
                reason=err_msg or ("PayFast confirmed payment" if success else "PayFast reported failure"),
                api_response=(
                    f'{{"err_code": "{err_code}", "transaction_id": "{payfast_tid}", '
                    f'"basket_id": "{basket_id}"}}'
                ),
            )
            db.add(verification)

            referral_notifications: list[dict] = []
            wallet_credited = False
            linked_id = linked_order_id_from_tx(tx)
            purchase_order = db.get(Order, linked_id) if linked_id else None

            if success:
                ok, reason = can_apply_payfast_success(tx)
                if not ok:
                    logger.warning("[PAYFAST] Refusing success credit for tx=%s: %s", tx.id, reason)
                    db.rollback()
                    if redirect == "Y":
                        return HTMLResponse("<h3>This payment cannot be applied.</h3>", status_code=409)
                    return PlainTextResponse("Cannot apply payment", status_code=409)

                tx.status = "confirmed"
                tx.blockchain_status = "confirmed"
                tx.tx_hash = payfast_tid
                tx.verified_at = datetime.utcnow()
                verification.verified_at = tx.verified_at

                if purchase_order is not None:
                    owned, ownership_reason = assert_order_owned_by_tx(purchase_order, tx)
                    if not owned:
                        # Never fulfill / credit anyone else — keep funds on the paying user wallet.
                        logger.error("[PAYFAST] %s — falling back to wallet credit for tx=%s", ownership_reason, tx.id)
                        tx.user.wallet_usdt += tx.amount
                        referral_notifications = credit_referral_join_bonus(db, tx.user)
                        user_message = (
                            f"✅ Your PayFast payment of ${tx.amount:.2f} was confirmed and added to your wallet."
                        )
                        wallet_credited = True
                    elif purchase_order.status == "pending":
                        user_message = await _fulfill_payfast_order(db, purchase_order, tx)
                    else:
                        # Order already expired/cancelled — credit only the payer's wallet.
                        tx.user.wallet_usdt += tx.amount
                        referral_notifications = credit_referral_join_bonus(db, tx.user)
                        user_message = (
                            f"✅ PayFast payment of ${tx.amount:.2f} confirmed. "
                            f"Order {purchase_order.order_code} was no longer pending, "
                            f"so the amount was credited to your wallet."
                        )
                        wallet_credited = True
                elif tx.tx_type == "deposit":
                    tx.user.wallet_usdt += tx.amount
                    referral_notifications = credit_referral_join_bonus(db, tx.user)
                    user_message = (
                        f"✅ Your PayFast top-up of ${tx.amount:.2f} was confirmed and added to your wallet."
                    )
                    wallet_credited = True
            else:
                tx.status = "rejected"
                tx.blockchain_status = "failed"
                if purchase_order and purchase_order.status == "pending":
                    owned, _ = assert_order_owned_by_tx(purchase_order, tx)
                    if owned:
                        purchase_order.status = "cancelled"
                        purchase_order.note = f"PayFast payment failed: {err_msg or err_code}"
                        try:
                            release_stock(db, purchase_order.service_id, purchase_order.quantity)
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "[PAYFAST] Failed to release stock for order=%s",
                                purchase_order.order_code,
                            )
                    user_message = (
                        f"❌ PayFast payment for order {purchase_order.order_code} was not successful. "
                        f"{err_msg or 'Please try again from the shop.'}"
                    )
                else:
                    user_message = (
                        f"❌ Your PayFast payment of ${tx.amount:.2f} was not successful. {err_msg or ''}"
                    )

            db.commit()
            for payload in referral_notifications:
                try:
                    await notify_referrer_earning(**payload)
                except Exception:  # noqa: BLE001
                    logger.exception("[PAYFAST] Failed to send referral notification")

            delivery_order = (
                purchase_order
                if purchase_order is not None and purchase_order.status == "completed"
                else None
            )
            await _notify_payfast_user(
                request,
                db,
                tx,
                user_message,
                order=delivery_order,
                service=delivery_order.service if delivery_order else None,
                show_products_button=wallet_credited,
            )

        if redirect == "Y":
            if tx.status == "confirmed":
                return HTMLResponse("<h3>✅ Payment successful! You can return to Telegram now.</h3>")
            if tx.status == "expired":
                return HTMLResponse(SESSION_CLOSED_HTML)
            return HTMLResponse("<h3>❌ Payment was not successful. You can return to Telegram and try again.</h3>")

        return PlainTextResponse("OK", status_code=200)
    finally:
        db.close()


async def _notify_payfast_user(
    request: Request,
    db,
    tx: Transaction,
    user_message: str | None,
    *,
    order: Order | None = None,
    service=None,
    show_products_button: bool = False,
) -> None:
    if not user_message:
        return
    # Single-notify guard: if the bot's own "paste your Order ID" status
    # check/poll loop already showed the user this same resolved outcome
    # (race with this webhook call), don't send a second, duplicate message.
    if not claim_payfast_user_notification(db, tx.id):
        logger.info("[PAYFAST] Skipping webhook notify for tx=%s — already notified elsewhere", tx.id)
        return
    try:
        bot = request.app.state.bot
        if bot and tx.user:
            reply_markup = None
            if show_products_button:
                from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

                reply_markup = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🛍 Products", callback_data="open_products")]]
                )
            telegram_id = str(tx.user.telegram_id)
            checking = take_payfast_checking_message(telegram_id)
            reused = False
            if checking:
                chat_id, message_id = checking
                try:
                    await bot.edit_message_text(
                        text=user_message,
                        chat_id=chat_id,
                        message_id=message_id,
                        parse_mode="HTML",
                        reply_markup=reply_markup,
                    )
                    reused = True
                except Exception:  # noqa: BLE001
                    logger.info(
                        "[PAYFAST] Could not edit Checking... message for tx=%s; sending a new one",
                        tx.id,
                    )
                    try:
                        await bot.delete_message(chat_id=chat_id, message_id=message_id)
                    except Exception:  # noqa: BLE001
                        pass
            if not reused:
                await bot.send_message(
                    telegram_id, user_message, parse_mode="HTML", reply_markup=reply_markup
                )
            if order is not None and service is not None:
                await maybe_send_delivery_file(
                    order=order,
                    service=service,
                    credentials=getattr(order, "delivered_info", None),
                    bot=bot,
                    telegram_id=tx.user.telegram_id,
                )
    except Exception:  # noqa: BLE001
        logger.exception("[PAYFAST] Failed to notify user for tx=%s", tx.id)


def _credit_verified_payfast_to_wallet(
    db,
    tx: Transaction,
    *,
    payfast_tid: str,
    basket_id: str,
    err_msg: str,
    late: bool = False,
) -> str:
    """Credit ONLY the checkout owner's wallet after a verified PayFast success.

    Never fulfills an order from this path (used after expire / ownership mismatch).
    """
    ok, reason = can_apply_payfast_success(tx)
    if not ok:
        logger.warning("[PAYFAST] Skip wallet credit for tx=%s: %s", tx.id, reason)
        return ""

    duplicate = payment_ref_already_used(db, payfast_tid, exclude_transaction_id=tx.id)
    if duplicate:
        logger.warning(
            "[PAYFAST] Skip wallet credit — PayFast TXID=%s already used by tx=%s",
            payfast_tid,
            duplicate.id,
        )
        return ""

    if not payfast_tid:
        logger.warning("[PAYFAST] Skip wallet credit — empty PayFast transaction_id for tx=%s", tx.id)
        return ""

    verification = PaymentVerification(
        transaction_id=tx.id,
        tx_hash=payfast_tid,
        blockchain="PAYFAST",
        to_address="PAYFAST",
        amount_verified=tx.amount,
        verification_status="verified",
        reason=err_msg
        or (
            "PayFast paid after checkout expired — credited to payer wallet only"
            if late
            else "PayFast confirmed payment — credited to payer wallet only"
        ),
        api_response=(
            f'{{"err_code": "000", "transaction_id": "{payfast_tid}", '
            f'"basket_id": "{basket_id}", "late": {str(late).lower()}}}'
        ),
        verified_at=datetime.utcnow(),
    )
    db.add(verification)
    tx.status = "confirmed"
    tx.blockchain_status = "confirmed"
    tx.tx_hash = payfast_tid
    tx.verified_at = verification.verified_at
    suffix = "Late PayFast payment credited to payer wallet only" if late else "PayFast credited to payer wallet only"
    if tx.note and suffix not in tx.note:
        tx.note = f"{tx.note} | {suffix}"
    elif not tx.note:
        tx.note = suffix

    # Explicit: only the user_id on this transaction row is credited.
    payer = tx.user
    payer.wallet_usdt += tx.amount

    linked_id = linked_order_id_from_tx(tx)
    order = db.get(Order, linked_id) if linked_id else None
    if order and int(order.user_id) != int(tx.user_id):
        logger.error(
            "[PAYFAST] Linked order user mismatch on wallet credit; ignoring order. order=%s",
            order.id,
        )
        order = None
    order_code = order.order_code if order else None
    return (
        f"✅ PayFast payment of ${tx.amount:.2f} was received"
        + (" after the checkout expired" if late else "")
        + ".\n"
        f"The amount was added to your wallet only"
        + (f" (order {order_code} was already released)." if order_code else ".")
        + "\nPlease place a new order from the shop if you still want the product."
    )


async def _fulfill_payfast_order(db, order: Order, tx: Transaction) -> str:
    """Mark order paid via PayFast and run the same fulfillment as wallet checkout."""
    from bot.handlers.products import fulfill_provider_order

    owned, reason = assert_order_owned_by_tx(order, tx)
    if not owned:
        raise RuntimeError(reason)

    service = order.service
    order.status = "manual_pending"
    order.payment_method = order.payment_method or "PAYFAST"
    order.note = f"Paid via PayFast. TX: {tx.tx_hash or tx.id}"
    fulfillment = await fulfill_provider_order(db, order, service)
    await notify_admin_new_order(order, order.user, service)
    if order.status == "completed":
        await notify_channel_order_completed(order, service, db)

    note = stock_note_text(service) if order.status == "completed" else None
    from utils.ui_icons import label_icons

    icons = label_icons(db)
    body = (
        f"{icons['tick']} PayFast payment confirmed!\n"
        f"{icons['order']} Order: {order.order_code}"
        f"{fulfillment}"
    )
    if note:
        body = f"{body}\n\n{icons['note']} Note:\n{note}"
    return body


@dataclass
class PayfastReferenceOutcome:
    """Result of a customer pasting their PayFast Order ID back to the bot.

    `code` is for backend/audit logging only — Telegram never sees it, only
    `message` (which is deliberately generic for wrong_owner/wrong_order so
    we never reveal whose reference it actually is).
    """

    code: str
    message: str
    order: Order | None = None
    delivered: bool = False
    tx_id: int | None = None


async def verify_payfast_reference(db, *, telegram_id: str, raw_reference: str) -> PayfastReferenceOutcome:
    """Manual "paste your PayFast Order ID" lookup/recovery path.

    Mirrors the Binance/BEP20 paste-and-verify UX, but a pasted reference is
    NEVER treated as proof of payment by itself. It is only a lookup key into
    our own trusted payment record. The actual PayFast-confirmed/failed
    status on that record is set exclusively by the authenticated PayFast
    callback in `payfast_callback` above (validated via
    `validate_callback_hash`, which uses the merchant secured_key that is
    never exposed to customers) — this function never marks a payment
    confirmed on its own, and never fulfils/credits anything outside the
    same canonical, idempotent `_fulfill_payfast_order` /
    `_credit_verified_payfast_to_wallet` helpers the callback itself uses.

    Note: PayFast's ipg2.apps.net.pk gateway does not expose a documented
    standalone "query transaction status" API separate from the callback/IPN,
    so this cannot make a fresh live call to PayFast on every paste. Instead
    it reports the current, already-authenticated status of the matching
    payment record. This keeps the system fail-closed: a payment is only
    ever recognised as successful once PayFast's own signed callback has
    said so.
    """
    ref, tid = payfast_manual_lookup_keys(raw_reference)
    if ref is None and tid is None:
        return PayfastReferenceOutcome(
            "invalid_format",
            "❌ Please send a valid PayFast Order ID or Transaction ID, e.g. SMFSHOP-A7K29Q.",
        )

    if payfast_lookup_rate_limited(telegram_id):
        logger.warning("[PAYFAST] Reference lookup rate-limited for telegram_id=%s", telegram_id)
        return PayfastReferenceOutcome(
            "rate_limited",
            "⚠️ Too many attempts. Please wait a few minutes and try again, or contact support.",
        )

    return lookup_payfast_reference_status(db, telegram_id=telegram_id, ref=ref, tid=tid)


def payfast_manual_lookup_keys(raw_reference: str) -> tuple[str | None, str | None]:
    """Split a pasted value into (order_no, tid) for status lookup.

    Exactly one side is set for a well-formed paste. Both None means the
    value is not a PayFast Order No. or Transaction ID.
    """
    ref = normalize_payfast_reference(raw_reference)
    # Loose sanity check only (length/charset) — NOT the strict new-format
    # regex, so a reference issued just before this feature was deployed
    # (old numeric "SMFSHOP123" style) can still be looked up and reported
    # on; it simply will never be found among payfast_reference values
    # assigned going forward, which correctly falls through to
    # "invalid_reference" in lookup_payfast_reference_status.
    is_order_no = bool(ref) and len(ref) <= 40 and re.match(r"^SMFSHOP-?[A-Z0-9]{1,20}$", ref)
    if is_order_no:
        return ref, None
    candidate_tid = normalize_payment_ref(raw_reference)
    if looks_like_payfast_tid(candidate_tid):
        return None, candidate_tid
    return None, None


def lookup_payfast_reference_status(
    db, *, telegram_id: str, ref: str | None = None, tid: str | None = None
) -> "PayfastReferenceOutcome":
    """Core status lookup, split out of `verify_payfast_reference` so an
    automatic background poll (bot repeatedly re-checking the same
    already-validated reference while showing a live progress bar) can call
    this directly without burning through `payfast_lookup_rate_limited`'s
    budget, which is meant to limit distinct user-submitted references, not
    our own internal re-checks of the one reference the user already gave us.
    `ref` must already be normalized (see `normalize_payfast_reference`).
    """
    from database.models import User

    user = db.query(User).filter(User.telegram_id == str(telegram_id)).first()
    if not user:
        return PayfastReferenceOutcome(
            "invalid_reference",
            "❌ We could not verify this PayFast Order No. / Transaction ID. Please check it and try again.",
        )

    # Row-locked: serializes against a concurrent callback for the same tx so
    # we always report the final, post-callback state rather than a stale one.
    query = db.query(Transaction)
    if ref:
        query = query.filter(Transaction.payfast_reference == ref)
    else:
        # TID is written to tx_hash only after PayFast's authenticated
        # callback confirms the payment. A just-paid paste can miss until
        # that write lands; the caller keeps polling while this is pending.
        query = query.filter(Transaction.tx_hash == tid)
    tx = query.with_for_update().first()
    if not tx:
        if tid:
            # TID is only written to tx_hash after PayFast's authenticated
            # callback lands — a just-paid customer can paste it before that
            # write. Treat as still-pending so the bot keeps the loading bar
            # and re-checks instead of showing "not found" while the webhook
            # is still in flight.
            logger.info("[PAYFAST] TID lookup miss tid=%s telegram_id=%s", tid, telegram_id)
            return PayfastReferenceOutcome(
                "pending",
                "⏳ Your PayFast payment is still being confirmed.",
            )
        logger.info("[PAYFAST] Reference lookup miss ref=%s telegram_id=%s", ref, telegram_id)
        return PayfastReferenceOutcome(
            "invalid_reference",
            "❌ We could not verify this PayFast Order No. / Transaction ID. Please check it and try again.",
        )

    if int(tx.user_id) != int(user.id):
        logger.warning(
            "[PAYFAST] Reference lookup wrong_owner ref=%s tx_user=%s requester=%s",
            ref, tx.user_id, user.id,
        )
        return PayfastReferenceOutcome(
            "wrong_owner",
            "❌ This PayFast Order No. / Transaction ID is not valid for your current order.",
        )

    linked_id = linked_order_id_from_tx(tx)
    order = db.get(Order, linked_id) if linked_id else None
    if order is not None:
        owned, reason = assert_order_owned_by_tx(order, tx)
        if not owned:
            logger.error(
                "[PAYFAST] Reference lookup wrong_order ref=%s tx=%s order=%s reason=%s",
                ref, tx.id, order.id, reason,
            )
            return PayfastReferenceOutcome(
                "wrong_order",
                "❌ This PayFast Order No. / Transaction ID is not valid for your current order.",
            )

    # Already fully processed — never fulfil/credit twice, whether that
    # happened via the callback earlier or is happening concurrently right
    # now (the row lock above waits for it, then we land here).
    if tx.verified_at is not None:
        if order is not None and order.status == "completed":
            return PayfastReferenceOutcome(
                "already_delivered",
                "✅ This order has already been completed.",
                order=order,
                delivered=True,
                tx_id=tx.id,
            )
        if order is None or order.status not in {"pending", "manual_pending"}:
            # Confirmed by PayFast's callback, but not (or no longer) fulfilling
            # an order that's pending or awaiting manual delivery — this is
            # exactly the wallet-only credit path (late payment, or order
            # already released/cancelled by the time it landed). Report the
            # real outcome instead of a generic "already used" — same wording
            # whether the customer is seeing this for the first time via the
            # poll or on a later resubmit. ("completed" is excluded here —
            # it's already handled above.)
            order_code = order.order_code if order is not None else None
            return PayfastReferenceOutcome(
                "wallet_credited",
                f"✅ PayFast payment of ${tx.amount:.2f} was confirmed and added to your wallet"
                + (f" (order {order_code} was already released)." if order_code else ".")
                + "\nPlease place a new order from the shop if you still want the product.",
                order=order,
                tx_id=tx.id,
            )
        return PayfastReferenceOutcome(
            "already_used",
            "⚠️ This PayFast payment has already been used for an order.\n\n"
            "Please use the PayFast Order No. / Transaction ID from your current payment.",
            order=order,
            tx_id=tx.id,
        )

    if tx.status == "rejected":
        return PayfastReferenceOutcome(
            "failed",
            "❌ This PayFast payment was not successful. Please try again from the shop or contact support.",
            order=order,
            tx_id=tx.id,
        )

    if tx.status in {"pending", "expired"}:
        # Not yet confirmed by PayFast's own authenticated callback. Covers
        # both "still on the payment screen" and "checkout idle-expired but
        # a delayed Raast/Wallet approval may still land" — the customer can
        # safely resubmit the same reference; nothing is destroyed by time.
        return PayfastReferenceOutcome(
            "pending",
            "⏳ Your PayFast payment is still being confirmed. Please wait a little and submit the same Order ID again.",
            order=order,
        )

    logger.warning("[PAYFAST] Reference lookup unexpected tx status=%s ref=%s tx=%s", tx.status, ref, tx.id)
    return PayfastReferenceOutcome(
        "pending",
        "⚠️ We could not verify your payment right now. Please try again shortly.",
        order=order,
    )
