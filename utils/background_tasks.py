import asyncio
import logging
from datetime import datetime

from database.models import (
    BotConfig,
    Order,
    PaymentVerification,
    ProductSale,
    Provider,
    ReferralCode,
    ReferralEarning,
    Service,
    SessionLocal,
    Transaction,
    Webhook,
)
from utils.helpers import get_referral_settings, is_self_referral
from utils.notifications import notify_channel_order_completed, notify_referrer_earning, notify_user_order_completed, send_user_message
from utils.payment_verify import verify_payment
from utils.provider_api import ProviderApiError, get_status
from utils.provider_delivery import (
    extract_provider_delivery_items,
    extract_provider_status,
    provider_response_has_delivery,
    provider_status_is_completed,
    provider_status_is_failed,
)
from utils.checkout_expire import expire_unpaid_checkouts_once
from utils.preorder_manager import check_expired_preorders_job
from utils.stock_manager import complete_reserved_stock, release_stock
from utils.webhook_dispatcher import dispatch_webhook

logger = logging.getLogger(__name__)

# Har kitni der baad provider APIs se stock + price dobara fetch ho:
# 10 minutes rakha hai taake stock/price zyada der tak purana na rahe,
# lekin providers ke server par bhi zyada load na pade.
PROVIDER_SYNC_INTERVAL_SECONDS = 600
# Unpaid PayFast checkout expiry poll — idle window is 20 minutes by default.
CHECKOUT_EXPIRE_INTERVAL_SECONDS = 30
# Timed product sales (flash etc.) — restore original price when ends_at passes.
SALE_EXPIRE_INTERVAL_SECONDS = 60


async def sync_provider_stock_job() -> None:
    """Har active API provider se pehle se imported products ka price + stock
    refresh karta hai (name/description overwrite NAHI). Naye provider products
    auto-create NAHI hote — admin Import Products se Import dabaye.
    sell_price = cost + % markup + fixed USDT, rounded UP to 2 decimals (skipped when manual_sell_price)."""
    while True:
        await sync_all_providers_once()
        await asyncio.sleep(PROVIDER_SYNC_INTERVAL_SECONDS)


async def sync_all_providers_once() -> None:
    # Local import to avoid a circular import (admin.routes imports from
    # utils.provider_api, and importing it at module load time up top would
    # create a cycle since admin.routes also depends on database.models).
    from admin.routes import sync_provider_products

    db = SessionLocal()
    try:
        providers = db.query(Provider).filter(Provider.type == "api", Provider.is_active.is_(True)).all()
    finally:
        db.close()

    for provider in providers:
        db = SessionLocal()
        try:
            provider = db.get(Provider, provider.id)
            if not provider:
                continue
            created, updated, balance, balance_err = await sync_provider_products(db, provider)
            logger.info(
                "[AUTO-SYNC] provider=%s created=%s updated=%s balance=%s err=%s",
                provider.name,
                created,
                updated,
                balance,
                balance_err,
            )
        except Exception as exc:  # noqa: BLE001 - one provider failing must not stop the others
            logger.warning("[AUTO-SYNC] provider=%s failed: %s", provider.name, exc)
        finally:
            db.close()


async def check_order_status_job() -> None:
    while True:
        await check_processing_orders_once()
        await asyncio.sleep(120)


async def verify_transactions_job() -> None:
    while True:
        await verify_pending_transactions_once()
        await asyncio.sleep(600)


async def process_referral_payouts_job() -> None:
    while True:
        process_referral_payouts_once()
        await asyncio.sleep(3600)


async def expire_unpaid_checkouts_job() -> None:
    """Auto-expire unpaid PayFast checkouts (order + payment link) and free stock."""
    while True:
        try:
            result = expire_unpaid_checkouts_once()
            for telegram_id, text in result.get("notifications") or []:
                try:
                    await send_user_message(telegram_id, text, parse_mode="HTML")
                except Exception:  # noqa: BLE001
                    logger.exception("[CHECKOUT-EXPIRE] Failed to notify user %s", telegram_id)
        except Exception:  # noqa: BLE001
            logger.exception("[CHECKOUT-EXPIRE] Background job iteration failed")
        await asyncio.sleep(CHECKOUT_EXPIRE_INTERVAL_SECONDS)


def expire_active_sales_once() -> int:
    """Deactivate timed sales past ends_at and restore original sell_price."""
    db = SessionLocal()
    restored = 0
    try:
        now = datetime.utcnow()
        sales = (
            db.query(ProductSale)
            .filter(
                ProductSale.is_active.is_(True),
                ProductSale.ends_at.isnot(None),
                ProductSale.ends_at <= now,
            )
            .all()
        )
        for sale in sales:
            service = db.get(Service, sale.service_id)
            if service and sale.original_price is not None:
                if abs(float(service.sell_price or 0) - float(sale.sale_price or 0)) < 0.0001:
                    service.sell_price = float(sale.original_price)
                    restored += 1
            sale.is_active = False
        if sales:
            db.commit()
            logger.info("[SALE-EXPIRE] deactivated=%s restored_prices=%s", len(sales), restored)
        return len(sales)
    except Exception:  # noqa: BLE001
        logger.exception("[SALE-EXPIRE] Failed to expire sales")
        db.rollback()
        return 0
    finally:
        db.close()


async def expire_active_sales_job() -> None:
    """Background loop: end flash/timed sales and restore prices."""
    while True:
        try:
            expire_active_sales_once()
        except Exception:  # noqa: BLE001
            logger.exception("[SALE-EXPIRE] Background job iteration failed")
        await asyncio.sleep(SALE_EXPIRE_INTERVAL_SECONDS)


async def check_processing_orders_once() -> None:
    db = SessionLocal()
    referral_notifications: list[dict] = []
    completed_orders: list[tuple] = []
    try:
        orders = db.query(Order).filter(Order.status == "processing").limit(50).all()
        for order in orders:
            provider = order.service.provider
            if not provider or provider.type != "api" or not order.provider_order_id:
                continue
            try:
                status_response = await get_status(provider, order.provider_order_id)
            except ProviderApiError as exc:
                order.provider_status = str(exc)
                continue

            provider_status = extract_provider_status(status_response)
            order.provider_status = provider_status
            delivered_items = extract_provider_delivery_items(status_response)
            if provider_status_is_completed(provider_status) or provider_response_has_delivery(status_response):
                order.status = "completed"
                if delivered_items:
                    order.delivered_info = "\n".join(delivered_items)
                order.note = "Auto-delivered to customer via provider API."
                order.completed_at = datetime.utcnow()
                complete_reserved_stock(db, order.service_id, order.quantity)
                referral_notifications += credit_referral_for_order(db, order)
                referral_notifications += credit_referral_join_bonus(db, order.user)
                await trigger_order_webhooks(db, order, "order_completed")
                if order.delivered_info:
                    try:
                        from utils.granted_accounts import sync_granted_accounts_for_order
                        sync_granted_accounts_for_order(db, order)
                    except Exception:
                        pass
                completed_orders.append((order.id, order.service_id))
            elif provider_status_is_failed(provider_status):
                order.status = "failed"
                release_stock(db, order.service_id, order.quantity)
                order.user.wallet_usdt += order.amount_usdt
                db.add(Transaction(user_id=order.user_id, amount=order.amount_usdt, tx_type="refund", status="confirmed", blockchain_status="confirmed", note=f"Refund for {order.order_code}"))
                await trigger_order_webhooks(db, order, "order_failed")
        db.commit()
    finally:
        db.close()
    for payload in referral_notifications:
        await notify_referrer_earning(**payload)
    if completed_orders:
        notify_db = SessionLocal()
        try:
            for order_id, service_id in completed_orders:
                order = notify_db.get(Order, order_id)
                service = order.service if order else None
                if order and service:
                    await notify_user_order_completed(order, service)
                    await notify_channel_order_completed(order, service, notify_db)
        finally:
            notify_db.close()


async def verify_pending_transactions_once() -> None:
    db = SessionLocal()
    referral_notifications: list[dict] = []
    try:
        config = db.query(BotConfig).first()
        if not config or not config.auto_verify_enabled:
            return
        if config.usdt_network not in {"BINANCE", "BYBIT"} and not config.usdt_address:
            return
        from utils.payment_security import payment_ref_already_used

        txs = db.query(Transaction).filter(Transaction.tx_type == "deposit", Transaction.blockchain_status == "pending").limit(25).all()
        for tx in txs:
            if not tx.tx_hash or tx.verified_at is not None:
                continue
            # PayFast is confirmed only by signed callback — never by this poller.
            note = (tx.note or "").lower()
            if "payfast" in note:
                continue
            if payment_ref_already_used(db, tx.tx_hash, exclude_transaction_id=tx.id):
                tx.status = "rejected"
                tx.blockchain_status = "failed"
                if tx.verification:
                    tx.verification.verification_status = "failed"
                    tx.verification.reason = "Duplicate TXID already credited on another deposit"
                continue
            result = await verify_payment(config.usdt_network, tx.tx_hash, tx.amount, config.usdt_address)
            verification = tx.verification or PaymentVerification(
                transaction_id=tx.id,
                tx_hash=tx.tx_hash,
                blockchain=config.usdt_network,
            )
            verification.contract_address = result.contract_address
            verification.from_address = result.from_address
            verification.to_address = result.to_address
            verification.amount_verified = result.amount
            verification.verification_status = result.status
            verification.api_response = result.raw_json()
            if result.verified:
                # Re-check duplicate immediately before credit (race safety).
                if payment_ref_already_used(db, tx.tx_hash, exclude_transaction_id=tx.id):
                    verification.verification_status = "failed"
                    verification.reason = "Duplicate TXID already credited on another deposit"
                    tx.status = "rejected"
                    tx.blockchain_status = "failed"
                else:
                    verification.verified_at = datetime.utcnow()
                    tx.verified_at = verification.verified_at
                    tx.blockchain_status = "confirmed"
                    tx.status = "confirmed"
                    # Credit only the owner of this transaction row.
                    tx.user.wallet_usdt += tx.amount
                    referral_notifications += credit_referral_join_bonus(db, tx.user)
                    await trigger_user_webhooks(db, tx.user_id, "deposit_confirmed", {"amount": tx.amount, "tx_hash": tx.tx_hash})
            elif result.status == "failed":
                tx.blockchain_status = "failed"
                tx.status = "rejected"
            db.add(verification)
        db.commit()
    finally:
        db.close()
    for payload in referral_notifications:
        await notify_referrer_earning(**payload)


def credit_referral_for_order(db, order: Order) -> list[dict]:
    """Per-purchase commission: pays the referrer a cut of every completed
    order made by someone they referred. Only fires while the admin's
    referral Program is set to 'Per Purchase Earning' — this is the single
    on/off switch that stops the same link double-earning from both modes
    at once, since per_link and per_purchase can never both be active."""
    notifications: list[dict] = []
    user = order.user
    if not user.referrer_id:
        return notifications

    settings = get_referral_settings(db)
    if not settings["enabled"] or settings["program_type"] != "per_purchase":
        return notifications

    if settings["commission_type"] == "fixed":
        amount = round(settings["commission_value"], 6)
    else:
        amount = round(order.amount_usdt * settings["commission_value"] / 100, 6)
    if amount <= 0:
        return notifications

    referrer = db.get(type(user), user.referrer_id)
    if not referrer:
        return notifications

    flagged = is_self_referral(db, referrer, user)
    earning = ReferralEarning(
        referrer_id=referrer.id,
        referred_user_id=user.id,
        order_id=order.id,
        amount_earned=amount,
        earning_type="per_purchase",
        status="voided_self_referral" if flagged else "credited",
        credited_at=None if flagged else datetime.utcnow(),
    )
    db.add(earning)
    if flagged:
        return notifications

    referrer.referral_wallet += amount
    referral_code = db.query(ReferralCode).filter(ReferralCode.user_id == referrer.id, ReferralCode.is_active.is_(True)).first()
    if referral_code:
        referral_code.total_earned += amount

    notifications.append({
        "referrer_telegram_id": referrer.telegram_id,
        "amount": amount,
        "reason": f"Commission from {user.username or user.full_name or user.telegram_id}'s purchase",
    })
    return notifications


def credit_referral_join_bonus(db, user) -> list[dict]:
    """Per-link (per-join) bonus: pays the referrer a flat one-off reward —
    but only once the referred user proves they're a real, active account by
    completing their first confirmed deposit or their first completed order.
    That's the anti-fraud gate the admin asked for: a brand-new account that
    only taps /start never triggers a payout, so farming fake/dummy accounts
    through your own link earns nothing. Only fires while the admin's
    referral Program is set to 'Per Link Earning'."""
    notifications: list[dict] = []
    if not user.referrer_id or user.referral_join_credited:
        return notifications

    settings = get_referral_settings(db)
    if not settings["enabled"] or settings["program_type"] != "per_link":
        return notifications

    referrer = db.get(type(user), user.referrer_id)
    if not referrer:
        return notifications

    # This flag is set exactly once per user, regardless of outcome below —
    # it's what stops the bonus from being evaluated (and potentially paid)
    # again on every future deposit/order from the same referred user.
    user.referral_join_credited = True

    amount = round(settings["commission_value"], 6) if settings["commission_type"] == "fixed" else 0.0
    if amount <= 0:
        return notifications

    flagged = is_self_referral(db, referrer, user)
    earning = ReferralEarning(
        referrer_id=referrer.id,
        referred_user_id=user.id,
        order_id=None,
        amount_earned=amount,
        earning_type="per_link",
        status="voided_self_referral" if flagged else "credited",
        credited_at=None if flagged else datetime.utcnow(),
    )
    db.add(earning)
    if flagged:
        return notifications

    referrer.referral_wallet += amount
    referral_code = db.query(ReferralCode).filter(ReferralCode.user_id == referrer.id, ReferralCode.is_active.is_(True)).first()
    if referral_code:
        referral_code.total_earned += amount

    notifications.append({
        "referrer_telegram_id": referrer.telegram_id,
        "amount": amount,
        "reason": f"Referral joined bonus — {user.username or user.full_name or user.telegram_id} became active",
    })
    return notifications


def process_referral_payouts_once() -> None:
    db = SessionLocal()
    try:
        pending = db.query(ReferralEarning).filter(ReferralEarning.status == "pending").limit(100).all()
        for earning in pending:
            earning.status = "credited"
            earning.credited_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


async def trigger_order_webhooks(db, order: Order, event: str) -> None:
    await trigger_user_webhooks(
        db,
        order.user_id,
        event,
        {
            "order_code": order.order_code,
            "status": order.status,
            "service": order.service.name,
            "quantity": order.quantity,
            "amount": order.amount_usdt,
            "timestamp": datetime.utcnow().isoformat(),
        },
    )


async def trigger_user_webhooks(db, user_id: int, event: str, data: dict) -> None:
    webhooks = db.query(Webhook).filter(Webhook.user_id == user_id, Webhook.event_type == event, Webhook.is_active.is_(True)).all()
    for webhook in webhooks:
        await dispatch_webhook(webhook.webhook_url, event, data)
