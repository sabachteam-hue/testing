import asyncio
from datetime import datetime
import html
import logging
import os

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.keyboards import wallet_full_keyboard
from database.models import (
    BotConfig,
    Order,
    PaymentMethod,
    PaymentVerification,
    Service,
    SessionLocal,
    Transaction,
    get_active_payment_methods,
)
from utils.helpers import format_usdt, generate_order_code, get_or_create_user, render_icon
from utils.catalog_image import send_catalog_photo, payment_method_catalog_item
from utils.notifications import send_admin_message
from utils.payment_verify import verify_payment
from utils.menu_commands import MenuCommandFilter, get_command_map, text_matches_any_menu
from utils.stock_display import preset_icon
from utils.ui_icons import build_ui_icons, wallet_method_icon, wallet_screen_copy


router = Router()

USER_DEPOSIT_PENDING = (
    "⏳ Deposit submitted. We're verifying your payment — you'll get a confirmation shortly.\n"
    "If it takes too long, please contact support."
)
USER_DEPOSIT_REVIEW = "⏳ Deposit submitted for admin review. You'll be notified once it's confirmed."


class DepositFlow(StatesGroup):
    waiting_amount = State()
    waiting_reference = State()


class PayFastReferenceFlow(StatesGroup):
    """"Paste your PayFast Order ID" recovery/status-check flow — mirrors the
    Binance/BEP20 TXID paste UX. See api/payfast.py::verify_payfast_reference
    for why this only ever reports status and never fulfils/credits by
    itself (that stays exclusively the authenticated callback's job)."""

    waiting_reference = State()


async def _bypass_deposit_menu_press(message: Message, state: FSMContext) -> bool:
    """Reply-keyboard menu presses must not be treated as amount/TXID."""
    text = (message.text or "").strip()
    if not text:
        return False
    db = SessionLocal()
    try:
        is_menu = text_matches_any_menu(text, db)
    finally:
        db.close()
    if is_menu:
        await state.clear()
        await message.answer(
            "↩️ Deposit cancelled — you moved to another menu.\n"
            "Please tap the option you need again."
        )
        return True
    return False


def _looks_like_txid(text: str) -> bool:
    """Reject obvious non-TX strings (menu labels / multi-word button text)."""
    value = (text or "").strip()
    if len(value) < 4:
        return False
    words = value.split()
    # Menu buttons are usually "emoji + label" or multi-word English.
    if len(words) >= 2 and any(ch.isalpha() for ch in value):
        if not value.startswith("0x") and all(len(w) < 40 for w in words):
            return False
    return True


@router.message(Command("wallet"))
@router.message(MenuCommandFilter("wallet"))
async def wallet_command(message: Message) -> None:
    db = SessionLocal()
    try:
        user = get_or_create_user(db, str(message.from_user.id), message.from_user.username, message.from_user.full_name)
        methods = get_active_payment_methods(db)
        commands = get_command_map(db)
        icons = build_ui_icons(db)
        title, text = wallet_screen_copy(db, wallet_usdt=user.wallet_usdt, referral_wallet=user.referral_wallet)
    finally:
        db.close()
    keyboard = wallet_full_keyboard(methods, commands=commands, icons=icons)
    if methods:
        items = [payment_method_catalog_item(method) for method in methods]
        await send_catalog_photo(message, items, title=title, caption=text, reply_markup=keyboard)
    else:
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(Command("deposit"))
async def deposit_command(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("💳 Usage: /deposit AMOUNT TX_HASH")
        return
    try:
        amount = float(parts[1])
    except ValueError:
        await message.answer("⚠️ Amount must be a number.")
        return
    from utils.payment_security import normalize_payment_ref

    tx_hash = normalize_payment_ref(parts[2].strip())

    db = SessionLocal()
    try:
        config = db.query(BotConfig).first()
        user = get_or_create_user(db, str(message.from_user.id), message.from_user.username, message.from_user.full_name)
        if not config:
            await message.answer("⚠️ Deposit wallet is not configured yet. Please contact support.")
            return
        if config.usdt_network not in {"BINANCE", "BYBIT"} and not config.usdt_address:
            await message.answer("⚠️ Deposit wallet address is not configured yet. Please contact support.")
            return
        if amount < config.min_deposit:
            await message.answer(f"⚠️ Minimum deposit is {format_usdt(config.min_deposit)}.")
            return
        from utils.payment_security import payment_ref_already_used

        if payment_ref_already_used(db, tx_hash):
            await message.answer("⚠️ This TX hash is already submitted.")
            return
        method = db.query(PaymentMethod).filter(PaymentMethod.code == config.usdt_network).first()
        message_text = await submit_deposit(db, user.id, config, method, config.usdt_network, amount, tx_hash, config.usdt_address or "")
        await message.answer(message_text)
    finally:
        db.close()


@router.callback_query(F.data == "wallet:history")
async def wallet_history(callback: CallbackQuery) -> None:
    db = SessionLocal()
    try:
        user = get_or_create_user(db, str(callback.from_user.id), callback.from_user.username, callback.from_user.full_name)
        transactions = db.query(Transaction).filter(Transaction.user_id == user.id).order_by(Transaction.created_at.desc()).limit(10).all()
        history_icon = preset_icon(db, ("orders", "orderhistory", "orders history", "order_history"), "📜")
        if not transactions:
            text = f"{history_icon} No transactions yet."
        else:
            text = "\n".join(f"📌 {tx.tx_type}: {format_usdt(tx.amount)} - {tx.status}" for tx in transactions)
            text = f"{history_icon} Transaction History\n\n{text}"
    finally:
        db.close()
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "wallet:deposit")
async def wallet_deposit(callback: CallbackQuery) -> None:
    # Kept for backward compatibility with any old inline messages still showing
    # a "Top up / Deposit" button. The wallet screen now shows top-up buttons
    # for every method directly (see wallet_full_keyboard), so this just re-shows
    # the same wallet screen.
    db = SessionLocal()
    try:
        user = get_or_create_user(db, str(callback.from_user.id), callback.from_user.username, callback.from_user.full_name)
        methods = get_active_payment_methods(db)
        commands = get_command_map(db)
        icons = build_ui_icons(db)
        title, text = wallet_screen_copy(db, wallet_usdt=user.wallet_usdt, referral_wallet=user.referral_wallet)
    finally:
        db.close()

    if not methods:
        await callback.message.answer("⚠️ No payment methods are configured right now. Please contact support.")
        await callback.answer()
        return

    keyboard = wallet_full_keyboard(methods, commands=commands, icons=icons)
    items = [payment_method_catalog_item(method) for method in methods]
    await send_catalog_photo(callback.message, items, title=title, caption=text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("wallet:topup:"))
async def wallet_topup_method(callback: CallbackQuery, state: FSMContext) -> None:
    code = callback.data.rsplit(":", 1)[1]

    db = SessionLocal()
    try:
        method = db.query(PaymentMethod).filter(PaymentMethod.code == code, PaymentMethod.is_active.is_(True)).first()
        price_icon = preset_icon(db, ("price",), "💵")
        icons = build_ui_icons(db)
        method_icon_value = wallet_method_icon(method, icons) if method else None
    finally:
        db.close()

    if not method:
        await callback.message.answer("⚠️ This payment method is no longer available. Please choose another one.")
        await callback.answer()
        return

    method_icon = render_icon(method_icon_value or method.icon, "💳", html_mode=True)
    label = f"{method_icon} {html.escape(method.name)}"
    await state.clear()
    await state.set_state(DepositFlow.waiting_amount)
    await state.update_data(method=method.code)
    await callback.message.answer(
        f"{price_icon} Enter amount to top up via {label} (USD/USDT), example:\n"
        "10.5",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(DepositFlow.waiting_amount)
async def deposit_amount_received(message: Message, state: FSMContext) -> None:
    if await _bypass_deposit_menu_press(message, state):
        return
    try:
        amount = float((message.text or "").strip())
    except ValueError:
        await message.answer("⚠️ Please enter a valid amount, example: 10.5")
        return
    if amount <= 0:
        await message.answer("⚠️ Amount must be greater than 0.")
        return

    data = await state.get_data()
    code = data.get("method", "BEP20")
    db = SessionLocal()
    try:
        config = db.query(BotConfig).first()
        method = db.query(PaymentMethod).filter(PaymentMethod.code == code).first()
        min_deposit = config.min_deposit if config else 1.0
        if amount < min_deposit:
            await message.answer(f"⚠️ Minimum deposit is {format_usdt(min_deposit)}.")
            return

        if is_payfast_method(code, method):
            await state.clear()
            await _start_payfast_checkout(message, db, config, amount)
            return

        destination = get_deposit_destination(code, method, config)
    finally:
        db.close()

    label = f"{render_icon(method.icon, '💳', html_mode=True)} {html.escape(method.name)}" if method else html.escape(code)
    if not destination:
        await message.answer(f"⚠️ {label} deposit details are not configured yet. Please contact support.", parse_mode="HTML")
        await state.clear()
        return

    await state.set_state(DepositFlow.waiting_reference)
    await state.update_data(method=code, amount=amount, destination=destination)
    await message.answer(build_payment_instruction(code, method, amount, destination, db=db), parse_mode="HTML")


@router.message(DepositFlow.waiting_reference)
async def deposit_reference_received(message: Message, state: FSMContext) -> None:
    if await _bypass_deposit_menu_press(message, state):
        return
    from utils.payment_security import normalize_payment_ref

    tx_hash = normalize_payment_ref((message.text or "").strip())
    if not _looks_like_txid(tx_hash):
        await message.answer(
            "⚠️ Please send a valid transaction hash / order ID / payment reference "
            "(not a menu button)."
        )
        return

    data = await state.get_data()
    code = data.get("method", "BEP20")
    amount = float(data.get("amount", 0))
    destination = data.get("destination", "")
    db = SessionLocal()
    try:
        config = db.query(BotConfig).first()
        method = db.query(PaymentMethod).filter(PaymentMethod.code == code).first()
        user = get_or_create_user(db, str(message.from_user.id), message.from_user.username, message.from_user.full_name)
        from utils.payment_security import payment_ref_already_used

        if payment_ref_already_used(db, tx_hash):
            await message.answer("⚠️ This transaction/reference is already submitted.")
            await state.clear()
            return
        try:
            response = await submit_deposit(db, user.id, config, method, code, amount, tx_hash, destination)
        except Exception as exc:
            logging.getLogger(__name__).exception("submit_deposit crashed for user=%s code=%s tx=%s", user.id, code, tx_hash)
            await send_admin_message(
                f"⚠️ Deposit auto-verify crash\n"
                f"User: {user.telegram_id} (@{user.username or '-'})\n"
                f"Method: {code}\nAmount: {amount}\nTX: {tx_hash}\n"
                f"Error: {exc}",
                db=db,
            )
            response = USER_DEPOSIT_REVIEW
    finally:
        db.close()
    await state.clear()
    await message.answer(response)


def get_deposit_destination(code: str, method: PaymentMethod | None, config: BotConfig | None) -> str:
    # New DB-managed methods (JazzCash, EasyPaisa, Bank Transfer, or any admin-added method):
    # use the address stored on the PaymentMethod row directly.
    if method and method.address:
        return method.address

    # Backward-compatible fallback for the 4 original hardcoded methods, in case an
    # existing method row was migrated without an address (e.g. Binance/Bybit IDs
    # that used to live only in env vars).
    if code == "BINANCE":
        return os.getenv("BINANCE_PAY_ID") or os.getenv("BINANCE_ID") or os.getenv("BINANCE_UID") or ""
    if code == "BYBIT":
        return os.getenv("BYBIT_PAY_ID") or os.getenv("BYBIT_ID") or os.getenv("BYBIT_UID") or ""
    if code == "TRC20":
        return os.getenv("TRC20_USDT_ADDRESS") or os.getenv("TRON_USDT_ADDRESS") or ""
    if code == "BEP20":
        return os.getenv("BEP20_USDT_ADDRESS") or os.getenv("USDT_ADDRESS") or os.getenv("USDT_WALLET") or (config.usdt_address if config else "") or ""
    return ""


def build_payment_instruction(
    code: str,
    method: PaymentMethod | None,
    amount: float,
    destination: str,
    *,
    db=None,
) -> str:
    amount_text = f"{amount:.2f}"
    price_icon = preset_icon(db, ("price",), "💵")
    icon = render_icon(method.icon if method else None, "💳", html_mode=True)
    name = html.escape(method.name) if method else html.escape(code)

    # If the admin wrote custom instructions for this method, use those (with amount/destination filled in).
    if method and method.instructions:
        return (
            f"{icon} {name}\n"
            f"{html.escape(destination)}\n"
            f"{price_icon} Amount to transfer: ${amount_text}\n\n"
            f"{html.escape(method.instructions)}"
        )

    if code == "BINANCE":
        return (
            f"{icon} Binance ID (tap to copy): {html.escape(destination)}\n"
            f"{price_icon} Amount to transfer: ${amount_text}\n"
            "Please send the order ID or off-chain transaction reference after payment for verification."
        )
    if code == "BYBIT":
        return (
            f"{icon} Bybit ID (tap to copy): {html.escape(destination)}\n"
            f"{price_icon} Amount to transfer: ${amount_text}\n"
            "After payment, send the Bybit Pay / transfer ID for automatic verification."
        )
    if code == "TRC20":
        return (
            f"🚀 Please transfer {amount_text} USDT via TRC20 to:\n"
            f"{html.escape(destination)}\n\n"
            "After transfer, send the transaction hash (TXID) for confirmation."
        )
    if code == "BEP20":
        return (
            f"🚀 Please transfer {amount_text} USDT via BEP20 to:\n"
            f"{html.escape(destination)}\n\n"
            "After transfer, send the transaction hash (TXID) for confirmation."
        )

    # Generic fallback for any new admin-added manual method (JazzCash, EasyPaisa, Bank Transfer, etc.)
    return (
        f"{icon} {name}\n"
        f"{html.escape(destination)}\n"
        f"{price_icon} Amount to transfer: ${amount_text}\n\n"
        "After payment, send your transaction ID / reference number here for confirmation.\n\n"
        "⚠️ <b>Pay promptly</b> — unpaid checkouts may expire and release reserved stock."
    )


async def submit_deposit(
    db,
    user_id: int,
    config: BotConfig | None,
    method: PaymentMethod | None,
    code: str,
    amount: float,
    tx_hash: str,
    destination: str,
) -> str:
    from utils.payment_security import payment_ref_already_used

    if not config:
        config = BotConfig(auto_verify_enabled=False, usdt_network=code, usdt_address=destination)

    # Double-check inside the write path so two racing submits cannot both credit.
    if payment_ref_already_used(db, tx_hash):
        return "⚠️ This transaction/reference is already submitted."

    tx = Transaction(
        user_id=user_id,
        amount=amount,
        tx_type="deposit",
        tx_hash=tx_hash,
        status="pending",
        blockchain_status="pending",
        note=f"Deposit via {code}",
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)

    # A deposit is auto-verified only if BOTH the global switch is on AND this specific
    # payment method is marked "auto" by the admin. New manual methods (JazzCash, EasyPaisa,
    # Bank Transfer, or any manual method) always go to admin review, regardless of the
    # global auto_verify_enabled flag.
    method_type = method.method_type if method else ("auto" if code in {"BEP20", "TRC20"} else "manual")
    should_auto_verify = config.auto_verify_enabled and method_type == "auto"

    if should_auto_verify:
        from utils.payment_security import MAX_FAILED_VERIFICATIONS, recent_failed_verification_count

        if recent_failed_verification_count(db, user_id) >= MAX_FAILED_VERIFICATIONS:
            tx.status = "rejected"
            tx.blockchain_status = "failed"
            db.commit()
            telegram_id = getattr(getattr(tx, "user", None), "telegram_id", None) or user_id
            username = getattr(getattr(tx, "user", None), "username", None) or "-"
            await send_admin_message(
                f"🚫 Repeated failed deposit verification attempts\n"
                f"User: {telegram_id} (@{username})\n"
                f"Method: {code}\nLatest TX: {tx_hash}\n"
                f"Blocked further auto-verify attempts — needs manual review.",
                db=db,
            )
            return "❌ Too many failed verification attempts. Please contact admin/support."
        result = await verify_payment(code, tx_hash, amount, destination)
        verification = PaymentVerification(
            transaction_id=tx.id,
            tx_hash=tx_hash,
            blockchain=code,
            contract_address=result.contract_address,
            from_address=result.from_address,
            to_address=result.to_address,
            amount_verified=result.amount,
            verification_status=result.status,
            reason=result.reason,
            api_response=result.raw_json(),
        )
        if result.verified:
            if payment_ref_already_used(db, tx_hash, exclude_transaction_id=tx.id):
                verification.verification_status = "failed"
                verification.reason = "Duplicate TXID already credited on another deposit"
                tx.status = "rejected"
                tx.blockchain_status = "failed"
                db.add(verification)
                db.commit()
                return "⚠️ This transaction/reference was already used on another deposit."
            tx.status = "confirmed"
            tx.blockchain_status = "confirmed"
            tx.verified_at = datetime.utcnow()
            verification.verified_at = tx.verified_at
            # Credit only the user_id on this deposit row.
            tx.user.wallet_usdt += amount
            db.add(verification)
            db.commit()
            return f"✅ Deposit verified. Your new balance is {format_usdt(tx.user.wallet_usdt)}."
        db.add(verification)
        db.commit()
        # Never expose raw API / HTTP errors to customers — admin only.
        reason = result.reason or result.status or "not verified"
        telegram_id = getattr(getattr(tx, "user", None), "telegram_id", None) or user_id
        username = getattr(getattr(tx, "user", None), "username", None) or "-"
        await send_admin_message(
            f"⚠️ Deposit not auto-verified\n"
            f"User: {telegram_id} (@{username})\n"
            f"Method: {code}\nAmount: {amount}\nTX: {tx_hash}\n"
            f"Status: {result.status}\nReason: {reason}",
            db=db,
        )
        return USER_DEPOSIT_PENDING

    db.commit()
    return USER_DEPOSIT_REVIEW


def is_payfast_method(code: str | None, method: PaymentMethod | None = None) -> bool:
    """True when this payment method should use PayFast hosted checkout (no TXID)."""
    raw = (code or (method.code if method else "") or "").strip().upper()
    if raw == "PAYFAST":
        return True
    name = ((method.name if method else "") or "").strip().lower()
    return "payfast" in name


async def _start_payfast_checkout(message: Message, db, config: "BotConfig | None", amount: float) -> None:
    """PayFast doesn't need the user to paste a reference at all - PayFast
    itself calls our /pay/payfast/callback once the payment completes, so we
    just create a pending Transaction and hand the user a link to pay."""
    if not config or not config.payfast_merchant_id or not config.payfast_secured_key:
        await message.answer("⚠️ PayFast is not configured yet. Please contact support.")
        return

    icons = build_ui_icons(db)
    pkr_icon = preset_icon(db, ("pkr", "payfast"), "🟢")
    price_icon = preset_icon(db, ("price",), "💵")

    user = get_or_create_user(db, str(message.from_user.id), message.from_user.username, message.from_user.full_name)
    # Tag the row so Admin → Transactions can show Method=PAYFAST while still pending
    # (before the PayFast callback creates a PaymentVerification).
    tx = Transaction(
        user_id=user.id,
        amount=amount,
        tx_type="deposit",
        tx_hash=None,
        status="pending",
        blockchain_status="pending",
        note="payfast_deposit",
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)

    await _send_payfast_checkout_message(
        message,
        db=db,
        config=config,
        amount=amount,
        tx_id=tx.id,
        icons=icons,
        pkr_icon=pkr_icon,
        price_icon=price_icon,
        purpose="top-up",
    )


async def start_payfast_order_checkout(
    message: Message,
    *,
    from_user,
    service_id: int,
    quantity: int,
    total: float,
    customer_email: str | None = None,
) -> None:
    """Direct product purchase via PayFast — creates pending order + checkout link.

    On PayFast callback success the order is fulfilled (see api/payfast.py).
    """
    from utils.stock_manager import InsufficientStockError, reserve_stock

    db = SessionLocal()
    try:
        config = db.query(BotConfig).first()
        if not config or not config.payfast_merchant_id or not config.payfast_secured_key:
            await message.answer("⚠️ PayFast is not configured yet. Please contact support.")
            return

        user = get_or_create_user(db, str(from_user.id), from_user.username, from_user.full_name)
        service = db.get(Service, int(service_id))
        if not service:
            await message.answer("⚠️ Product not found.")
            return
        try:
            reserve_stock(db, service.id, int(quantity))
        except InsufficientStockError as exc:
            db.rollback()
            await message.answer(f"⚠️ {exc}")
            return

        order = Order(
            order_code=generate_order_code(db),
            user_id=user.id,
            service_id=service.id,
            link="digital_product_order",
            quantity=int(quantity),
            amount_usdt=float(total),
            status="pending",
            order_type="manual",
            payment_method="PAYFAST",
            customer_email=(customer_email or "").strip() or None,
            note="Awaiting PayFast payment.",
            expire_notify=True,
        )
        db.add(order)
        db.flush()
        tx = Transaction(
            user_id=user.id,
            amount=float(total),
            tx_type="deposit",
            tx_hash=None,
            status="pending",
            blockchain_status="pending",
            note=f"payfast_order:{order.id}",
            expire_notify=True,
        )
        db.add(tx)
        db.commit()
        db.refresh(tx)

        icons = build_ui_icons(db)
        pkr_icon = preset_icon(db, ("pkr", "payfast"), "🟢")
        price_icon = preset_icon(db, ("price",), "💵")
        await _send_payfast_checkout_message(
            message,
            db=db,
            config=config,
            amount=float(total),
            tx_id=tx.id,
            icons=icons,
            pkr_icon=pkr_icon,
            price_icon=price_icon,
            purpose="purchase",
            order_code=order.order_code,
            product_name=service.name,
        )
    finally:
        db.close()


async def _send_payfast_checkout_message(
    message: Message,
    *,
    db,
    config: "BotConfig",
    amount: float,
    tx_id: int,
    icons: dict,
    pkr_icon: str,
    price_icon: str,
    purpose: str = "top-up",
    order_code: str | None = None,
    product_name: str | None = None,
) -> None:
    from utils.helpers import icon_button

    base_url = os.getenv("WEBHOOK_URL") or os.getenv("RAILWAY_PUBLIC_DOMAIN") or ""
    if base_url and not base_url.startswith("http"):
        base_url = f"https://{base_url}"
    base_url = base_url.rstrip("/")
    if not base_url:
        await message.answer(
            "⚠️ PayFast checkout URL is not configured (WEBHOOK_URL / RAILWAY_PUBLIC_DOMAIN missing). "
            "Please contact support."
        )
        return

    checkout_link = f"{base_url}/pay/payfast/checkout/{tx_id}"
    pkr_amount = round(amount * (config.usd_to_pkr_rate or 280.0), 2)
    buttons = [
        [
            icon_button(
                f"Pay Rs. {pkr_amount:,.0f} on PayFast",
                icon_value=icons.get("pay"),
                icon_fallback="💳",
                url=checkout_link,
            )
        ],
        [InlineKeyboardButton(text="✅ I Have Paid", callback_data=f"payfast_check:{tx_id}")],
    ]
    tutorial_text = ""
    if config.payfast_tutorial_url:
        buttons.append([InlineKeyboardButton(text="🎬 Watch Tutorial", url=config.payfast_tutorial_url)])
        tutorial_text = "\n\n🎬 WATCH TUTORIAL IF YOU DON'T KNOW HOW TO PAY"
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    # Options shown on PayFast hosted checkout (excluding cards).
    methods_line = (
        "JazzCash | EasyPaisa | SadaPay | NayaPay | Raast | All Pakistani Banks "
        "(IBFT / account transfer)"
    )
    title = f"{pkr_icon} Payfast Payment Method"
    methods_block = (
        f"You can pay with:\n{methods_line}\n\n"
        "Open PayFast below and choose any of these options — cards are not required."
    )

    if purpose == "purchase":
        from utils.helpers import strip_html_tags
        from utils.ui_icons import label_icons

        labels = label_icons(db)
        detail = (
            f"{methods_block}\n\n"
            f"{labels['product']} Product: {html.escape(strip_html_tags(product_name) or 'Product')}\n"
            f"{labels['order']} Order: {html.escape(order_code or '-')}\n"
            f"{price_icon} Amount: {format_usdt(amount)} (≈ Rs. {pkr_amount:,.0f})\n\n"
            "Tap the button below to pay securely on PayFast. Your order is completed "
            "automatically once payment is confirmed. If it's been a few minutes and nothing "
            "happened yet (e.g. Raast approval was delayed), tap <b>✅ I Have Paid</b> and paste "
            "your PayFast Order ID shown on the payment page to check its status."
        )
    else:
        detail = (
            f"{methods_block}\n\n"
            f"{price_icon} Amount: {format_usdt(amount)} (≈ Rs. {pkr_amount:,.0f})\n\n"
            "Tap the button below to pay securely on PayFast. Your wallet is credited "
            "automatically the moment payment is confirmed. If it's been a few minutes and "
            "nothing happened yet, tap <b>✅ I Have Paid</b> and paste your PayFast Order ID "
            "shown on the payment page to check its status."
        )

    await message.answer(
        f"{title}\n\n{detail}{tutorial_text}",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("payfast_check:"))
async def payfast_check_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        tx_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer()
        return
    await state.set_state(PayFastReferenceFlow.waiting_reference)
    await state.update_data(payfast_tx_id=tx_id)
    await callback.answer()
    await callback.message.answer(
        "📋 Please paste your <b>PayFast Order ID</b> (shown as \"Order no.\" on the PayFast "
        "payment page, e.g. <code>SMFSHOP-A7K29Q</code>).",
        parse_mode="HTML",
    )


# Real (not animated) background polling: once a user pastes a reference
# that isn't confirmed yet, we keep re-checking it ourselves — on the same
# progress message — for up to _PAYFAST_POLL_TOTAL_SECONDS, instead of
# telling the customer to manually resend the same ID over and over (which
# used to spam a fresh "still being confirmed" bubble on every resubmit).
# The bar's % reflects real elapsed wait time, is capped below 100 while
# still pending, and only ever reaches 100% together with the actual
# confirmed message — never faked ahead of the real DB status.
#
# Window is 6 minutes: observed real-world confirmations (a delayed/late
# PayFast callback landing after the checkout's own idle-expiry) have taken
# up to ~5 minutes end to end, so a short window used to time out and fall
# back to "please resubmit" before the real confirmation ever arrived. The
# interval is deliberately slow (6s) so the bar creeps rather than looking
# like it's stuck — ~60 edits over the full window, well within Telegram's
# edit-rate limits.
_PAYFAST_POLL_TOTAL_SECONDS = 360
_PAYFAST_POLL_INTERVAL_SECONDS = 6
_PAYFAST_POLL_MAX_PENDING_PERCENT = 96

# Outcome codes that mean the payment was actually settled by PayFast's
# authenticated webhook (tx.verified_at set / tx.status == "rejected") —
# i.e. the webhook itself either already sent, or is about to send, its own
# confirmation message for this same event. See the single-notify guard
# (claim_payfast_user_notification) applied right after these are produced.
_WEBHOOK_NOTIFIED_CODES = {"wallet_credited", "already_delivered", "already_used", "failed"}

_PRODUCTS_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="🛍 Products", callback_data="open_products")]]
)


def _payfast_progress_bar(percent: int, *, width: int = 10) -> str:
    filled = round(width * percent / 100)
    return "▓" * filled + "░" * (width - filled)


def _payfast_progress_text(percent: int) -> str:
    return (
        "🔎 Your payment is being checked by our system...\n"
        "This usually takes 1–2 minutes, but can occasionally take up to "
        "5 minutes if the payment gateway is under heavy load.\n"
        f"{_payfast_progress_bar(percent)} {percent}%"
    )


def _payfast_resolved_text(outcome_message: str) -> str:
    """The bar's very last frame and the outcome message land in the same
    edit, so the bar visibly reaches 100% at the exact moment the result
    appears — never a 96% bar sitting there while a separate message shows
    up afterwards."""
    return f"{_payfast_progress_bar(100)} 100%\n\n{outcome_message}"


@router.message(PayFastReferenceFlow.waiting_reference)
async def payfast_check_reference_received(message: Message, state: FSMContext) -> None:
    if await _bypass_deposit_menu_press(message, state):
        return
    raw_reference = (message.text or "").strip()
    if not raw_reference or len(raw_reference) > 40:
        await message.answer("⚠️ Please send a valid PayFast Order ID, e.g. SMFSHOP-A7K29Q.")
        return

    await state.clear()

    from api.payfast import normalize_payfast_reference, verify_payfast_reference

    telegram_id = str(message.from_user.id)
    status_msg = await message.answer(_payfast_progress_text(0))

    # First check goes through the normal, rate-limited, format-validated
    # path — this is what actually consumes one of the user's lookup
    # attempts and catches typos/invalid ids/wrong-owner instantly.
    db = SessionLocal()
    try:
        outcome = await verify_payfast_reference(db, telegram_id=telegram_id, raw_reference=raw_reference)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("[PAYFAST] verify_payfast_reference crashed for ref=%s", raw_reference)
        try:
            await status_msg.edit_text("⚠️ We could not verify your payment right now. Please try again shortly.")
        except Exception:  # noqa: BLE001
            pass
        return
    finally:
        db.close()

    if outcome.code == "pending":
        outcome = await _poll_payfast_until_resolved(
            status_msg,
            telegram_id=telegram_id,
            ref=normalize_payfast_reference(raw_reference),
            fallback=outcome,
        )

    # Single-notify guard: these outcome codes mean the payment was actually
    # settled by PayFast's authenticated webhook (not just looked up here) —
    # so the webhook either already sent its own confirmation message, or is
    # about to. Claim the guard before showing our own text: if the webhook
    # already claimed it first, don't print a second, duplicate confirmation
    # here — just quietly clear the progress bubble instead.
    if outcome.code in _WEBHOOK_NOTIFIED_CODES and outcome.tx_id is not None:
        from utils.payment_security import claim_payfast_user_notification

        db = SessionLocal()
        try:
            we_won = claim_payfast_user_notification(db, outcome.tx_id)
        finally:
            db.close()
        if not we_won:
            try:
                await status_msg.delete()
            except Exception:  # noqa: BLE001
                pass
            return

    # Still pending after the whole poll window is the one case that isn't
    # "resolved" — don't claim 100% for a check that genuinely isn't done.
    final_text = outcome.message if outcome.code == "pending" else _payfast_resolved_text(outcome.message)

    keyboard = _PRODUCTS_KEYBOARD if outcome.code == "wallet_credited" else None
    try:
        await status_msg.edit_text(final_text, parse_mode=None, reply_markup=keyboard)
    except Exception:  # noqa: BLE001 - fall back to a fresh message if the edit fails
        await message.answer(final_text, parse_mode=None, reply_markup=keyboard)


async def _poll_payfast_until_resolved(status_msg: Message, *, telegram_id: str, ref: str, fallback):
    """Re-check `ref` on a timer, editing `status_msg` in place, until the
    authenticated PayFast callback has actually settled it (confirmed/
    failed/etc.) or the poll window runs out. Never marks anything
    confirmed itself — every check goes through the same read-only,
    callback-populated status lookup as the manual flow.
    """
    from api.payfast import lookup_payfast_reference_status

    elapsed = 0
    outcome = fallback
    while elapsed < _PAYFAST_POLL_TOTAL_SECONDS:
        await asyncio.sleep(_PAYFAST_POLL_INTERVAL_SECONDS)
        elapsed += _PAYFAST_POLL_INTERVAL_SECONDS

        db = SessionLocal()
        try:
            outcome = lookup_payfast_reference_status(db, telegram_id=telegram_id, ref=ref)
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("[PAYFAST] background poll crashed for ref=%s", ref)
            break
        finally:
            db.close()

        if outcome.code != "pending":
            return outcome

        percent = min(_PAYFAST_POLL_MAX_PENDING_PERCENT, round(100 * elapsed / _PAYFAST_POLL_TOTAL_SECONDS))
        try:
            await status_msg.edit_text(_payfast_progress_text(percent))
        except Exception:  # noqa: BLE001 - edit races (e.g. rate limit) are harmless here
            pass

    return outcome
