import asyncio
import html
import logging
import re
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.orm import joinedload

from bot.keyboards import (
    main_menu_keyboard,
    order_payment_keyboard,
    post_order_actions_keyboard,
    products_keyboard,
    services_keyboard,
)
from bot.handlers.wallet import (
    build_payment_instruction,
    get_deposit_destination,
    is_payfast_method,
    start_payfast_order_checkout,
)
from database.models import (
    BotConfig,
    Category,
    Order,
    PaymentMethod,
    PaymentVerification,
    Service,
    SessionLocal,
    Transaction,
    get_active_payment_methods,
)
from utils.helpers import format_usdt, generate_order_code, get_or_create_user, render_icon, render_rich_html
from utils.force_join import is_admin_telegram_id
from utils.catalog_image import send_catalog_photo, service_catalog_item
from utils.product_display import (
    build_product_in_stock_parts,
    build_product_out_of_stock_text,
    product_image_file,
    service_sold_units,
)
from utils.notifications import (
    format_delivery_receipt_html,
    notify_admin_new_order,
    notify_channel_order_completed,
    maybe_send_delivery_file,
    send_admin_message,
    stock_note_text,
)
from utils.payment_verify import verify_payment
from utils.provider_api import ProviderApiError, get_order_details, place_order
from utils.provider_delivery import (
    extract_provider_delivery_items,
    extract_provider_order_id,
    extract_provider_status,
    format_provider_delivery_note,
    merge_provider_responses,
    provider_response_has_delivery,
)
from utils.stock_display import build_stock_legend, effective_available_qty, preset_icon
from utils.ui_icons import build_ui_icons
from utils.stock_manager import InsufficientStockError, complete_reserved_stock, consume_stock_account, reserve_stock
from utils.menu_commands import MenuCommandFilter, get_command_map, text_matches_any_menu

logger = logging.getLogger(__name__)

router = Router()

# Jitni der tak koi user "enter quantity" ya "send payment reference" state mein
# atka reh sakta hai - PayFast unpaid checkout window ke barabar (default 10 min).
def _order_session_timeout() -> timedelta:
    from utils.checkout_expire import unpaid_checkout_expire_minutes

    return timedelta(minutes=unpaid_checkout_expire_minutes())


# In buttons/commands ko user kabhi bhi bhej sakta hai chahe wo quantity ya
# payment-reference state mein hi kyun na atka ho - aise messages ko purane
# order flow ka hissa samajh kar process nahi karna.
async def _bypass_stale_order_state(message: Message, state: FSMContext) -> bool:
    """Menu press / timeout handling while in an order FSM state.

    - waiting_quantity: no order exists yet → silent clear + SkipHandler (menu opens)
    - waiting_payment_reference: payment pending → warn, then SkipHandler
    Returns True if the quantity/payment handler should stop (after raising SkipHandler
    this path is unused; kept for timeout-only cases that answer and stop).
    """
    text = (message.text or "").strip()
    db = SessionLocal()
    try:
        is_menu = text_matches_any_menu(text, db)
        commands = get_command_map(db)
    finally:
        db.close()

    current = await state.get_state()
    if is_menu:
        await state.clear()
        # Only warn when user had already reached payment (TXID / pay screen).
        if current == ProductOrderFlow.waiting_payment_reference.state:
            show_admin = is_admin_telegram_id(message.from_user.id if message.from_user else None)
            await message.answer(
                "↩️ Payment cancelled since you moved to another menu.\n"
                "Please tap the option you need again.",
                reply_markup=main_menu_keyboard(commands, show_admin=show_admin),
            )
        raise SkipHandler

    data = await state.get_data()
    started_at_raw = data.get("session_started_at")
    if started_at_raw:
        try:
            started_at = datetime.fromisoformat(started_at_raw)
        except ValueError:
            started_at = None
        if started_at and datetime.utcnow() - started_at > _order_session_timeout():
            await state.clear()
            show_admin = is_admin_telegram_id(message.from_user.id if message.from_user else None)
            await message.answer(
                "⌛ This order session expired.\n"
                "Please choose the product again.",
                reply_markup=main_menu_keyboard(commands, show_admin=show_admin),
            )
            return True
    return False


class ProductOrderFlow(StatesGroup):
    waiting_quantity = State()
    waiting_email = State()
    waiting_payment_reference = State()


_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


def _normalize_customer_email(text: str | None) -> str | None:
    email = (text or "").strip().lower()
    if not email or not _EMAIL_RE.fullmatch(email):
        return None
    return email


async def _show_order_payment_methods(
    message: Message,
    state: FSMContext,
    *,
    service,
    quantity: int,
    total: float,
    unit_price: float | None = None,
) -> None:
    """Order summary + payment method picker (after qty, and after email when required)."""
    from utils.ui_icons import label_icons

    db = SessionLocal()
    try:
        methods = get_active_payment_methods(db)
        icons = label_icons(db)
        data = await state.get_data()
        email_line = ""
        customer_email = (data.get("customer_email") or "").strip()
        if customer_email:
            email_line = f"{icons['email']} Email: {customer_email}\n"
        price = float(unit_price if unit_price is not None else (data.get("unit_price") or service.sell_price))
        list_price = float(data.get("list_price") or service.sell_price)
        if abs(price - list_price) > 1e-9:
            price_line = (
                f"{icons['price']} Unit price: <s>{format_usdt(list_price)}</s> → "
                f"<b>{format_usdt(price)}</b> (Special Price)\n"
            )
        else:
            price_line = f"{icons['price']} Unit price: {format_usdt(price)}\n"
        caption = (
            f"{icons['order']} Order summary\n\n"
            f"{icons['product']} Product: {service.name}\n"
            f"{icons['quantity']} Quantity: {quantity}\n"
            f"{email_line}"
            f"{price_line}"
            f"{icons['total']} Total: {format_usdt(total)}\n\n"
            "Choose payment method:"
        )
        markup = order_payment_keyboard(total, methods)
    finally:
        db.close()
    # Plain text + keyboard only (no catalog wrapper) so Pay with Wallet always shows.
    await message.answer(caption, reply_markup=markup, parse_mode="HTML")


async def _send_products_catalog(
    target: Message, *, telegram_id: str, username: str | None, full_name: str | None
) -> None:
    """Shared by /products, the shop menu button, and the inline 'Products'
    button we attach to PayFast status messages — same catalog render
    regardless of what triggered it."""
    db = SessionLocal()
    try:
        services = (
            db.query(Service)
            .options(joinedload(Service.stock))
            .filter(Service.is_active.is_(True), Service.is_deleted.is_(False))
            .order_by(Service.sort_order.asc(), Service.name.asc())
            .all()
        )
        from utils.pricing import service_unit_prices

        user = get_or_create_user(db, telegram_id, username, full_name)
        prices = service_unit_prices(db, services, user)
        commands = get_command_map(db)
        icons = build_ui_icons(db)
        stock_legend = build_stock_legend(db)
        items = [service_catalog_item(service) for service in services]
    finally:
        db.close()
    if not services:
        await target.answer("No products are available yet.")
        return
    await send_catalog_photo(
        target,
        items,
        title="Available Products",
        caption=f"{stock_legend}\nChoose a product:",
        reply_markup=services_keyboard(services, commands=commands, icons=icons, prices=prices),
    )


@router.message(Command("products"))
@router.message(MenuCommandFilter("shop"))
async def products_command(message: Message) -> None:
    await _send_products_catalog(
        message,
        telegram_id=str(message.from_user.id),
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )


@router.callback_query(F.data == "open_products")
async def open_products_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    await _send_products_catalog(
        callback.message,
        telegram_id=str(callback.from_user.id),
        username=callback.from_user.username,
        full_name=callback.from_user.full_name,
    )


@router.callback_query(F.data.startswith("cat:"))
async def category_callback(callback: CallbackQuery) -> None:
    category_id = int(callback.data.split(":", 1)[1])
    db = SessionLocal()
    try:
        services = (
            db.query(Service)
            .options(joinedload(Service.stock))
            .filter(Service.category_id == category_id, Service.is_active.is_(True), Service.is_deleted.is_(False))
            .order_by(Service.sort_order.asc(), Service.name.asc())
            .all()
        )
        from utils.pricing import service_unit_prices

        user = get_or_create_user(
            db,
            str(callback.from_user.id),
            callback.from_user.username,
            callback.from_user.full_name,
        )
        prices = service_unit_prices(db, services, user)
        commands = get_command_map(db)
        icons = build_ui_icons(db)
        stock_legend = build_stock_legend(db)
        items = [service_catalog_item(service) for service in services]
    finally:
        db.close()
    if not services:
        await callback.message.answer("No active products in this category.")
        await callback.answer()
        return
    await send_catalog_photo(
        callback.message,
        items,
        title="Available Products",
        caption=stock_legend,
        reply_markup=services_keyboard(services, commands=commands, icons=icons, prices=prices),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("svc:"))
async def service_callback(callback: CallbackQuery, state: FSMContext) -> None:
    service_id = int(callback.data.split(":", 1)[1])
    db = SessionLocal()
    try:
        service = (
            db.query(Service)
            .options(joinedload(Service.stock))
            .filter(Service.id == service_id)
            .first()
        )
        if not service:
            await callback.answer("Service not found", show_alert=True)
            return
        available = effective_available_qty(service)
        if available <= 0:
            await _send_product_message(
                callback,
                build_product_out_of_stock_text(service, db=db),
            )
            await state.clear()
            await callback.answer()
            return

        from utils.pricing import resolve_unit_price

        user = get_or_create_user(
            db,
            str(callback.from_user.id),
            callback.from_user.username,
            callback.from_user.full_name,
        )
        quote = resolve_unit_price(db, service, user)
        sold = service_sold_units(db, service.id)
        card, qty_prompt = build_product_in_stock_parts(
            service,
            available=available,
            sold=sold,
            db=db,
            unit_price=quote.unit_price,
            list_price=quote.list_price,
            personal_discount=quote.has_discount,
        )
        photo = product_image_file(service)
    finally:
        db.close()

    try:
        await state.clear()
        await state.set_state(ProductOrderFlow.waiting_quantity)
        await state.update_data(
            service_id=service_id,
            session_started_at=datetime.utcnow().isoformat(),
        )
        # One card (details + description box), then quantity prompt.
        if photo is not None:
            await _send_product_photo(callback, photo)
        await _send_product_html(callback, card)
        await _send_product_html(callback, qty_prompt)
        await callback.answer()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to open product %s: %s", service_id, exc)
        try:
            await callback.answer(
                "Could not open this product. Please try again.",
                show_alert=True,
            )
        except Exception:  # noqa: BLE001
            pass
        await state.clear()


async def _send_product_photo(callback: CallbackQuery, photo) -> None:
    from aiogram.exceptions import TelegramBadRequest

    try:
        await callback.message.answer_photo(photo=photo)
    except TelegramBadRequest as exc:
        logger.warning("Product banner photo failed: %s", exc)


async def _send_product_html(callback: CallbackQuery, text: str) -> None:
    """Send HTML product fragment; keep blockquote if custom emoji fails."""
    from aiogram.exceptions import TelegramBadRequest
    from utils.helpers import strip_html_tags, strip_tg_emoji_html

    try:
        await callback.message.answer(text, parse_mode="HTML")
        return
    except TelegramBadRequest as exc:
        logger.warning("Product HTML send failed (%s); retry without custom emoji", exc)

    cleaned = strip_tg_emoji_html(text)
    try:
        await callback.message.answer(cleaned, parse_mode="HTML")
        return
    except TelegramBadRequest as exc:
        logger.warning("Product HTML retry failed (%s); sending plain text", exc)

    await callback.message.answer(strip_html_tags(cleaned))


async def _send_product_message(callback: CallbackQuery, text: str, photo=None) -> None:
    """Out-of-stock / short messages — optional photo then HTML text."""
    if photo is not None:
        await _send_product_photo(callback, photo)
    await _send_product_html(callback, text)


@router.message(ProductOrderFlow.waiting_quantity)
async def product_quantity_received(message: Message, state: FSMContext) -> None:
    try:
        if await _bypass_stale_order_state(message, state):
            return
        try:
            quantity = int((message.text or "").strip())
        except ValueError:
            await message.answer("⚠️ Please enter quantity as a number, example: 1")
            return

        data = await state.get_data()
        service_id = data.get("service_id")
        if not service_id:
            await state.clear()
            await message.answer("⚠️ Order session expired. Please choose the product again.")
            return
        service_id = int(service_id)
        db = SessionLocal()
        try:
            service = (
                db.query(Service)
                .options(joinedload(Service.stock))
                .filter(Service.id == service_id)
                .first()
            )
            if not service or not service.is_active:
                await message.answer("⚠️ Product is not available anymore.")
                await state.clear()
                return
            available = effective_available_qty(service)
            if available <= 0:
                show_admin = is_admin_telegram_id(message.from_user.id if message.from_user else None)
                await message.answer(
                    f"🚫 This product is out of stock, we will update soon.",
                    reply_markup=main_menu_keyboard(show_admin=show_admin),
                )
                await state.clear()
                return
            if quantity < service.min_qty or quantity > service.max_qty:
                await message.answer(f"⚠️ Quantity must be between {service.min_qty} and {service.max_qty}.")
                return
            if quantity > available:
                await message.answer(f"⚠️ Only {available} items are available.")
                return
            from utils.pricing import resolve_unit_price

            user = get_or_create_user(
                db,
                str(message.from_user.id),
                message.from_user.username,
                message.from_user.full_name,
            )
            quote = resolve_unit_price(db, service, user)
            total = round(quote.unit_price * quantity, 6)
            await state.update_data(
                quantity=quantity,
                total=total,
                unit_price=quote.unit_price,
                list_price=quote.list_price,
                session_started_at=datetime.utcnow().isoformat(),
            )
            if getattr(service, "require_email", False):
                await state.set_state(ProductOrderFlow.waiting_email)
                mail_icon = preset_icon(db, ("email", "mail", "gmail"), "📧")
                await message.answer(
                    f"{mail_icon} Enter the email to invite/add for this plan:\n"
                    f"(example: name@gmail.com)"
                )
                return
            await _show_order_payment_methods(
                message,
                state,
                service=service,
                quantity=quantity,
                total=total,
                unit_price=quote.unit_price,
            )
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Quantity handler failed: %s", exc)
        try:
            await message.answer("⚠️ Something went wrong. Please tap the product again and enter quantity.")
        except Exception:  # noqa: BLE001
            pass
        await state.clear()


@router.message(ProductOrderFlow.waiting_email)
async def product_email_received(message: Message, state: FSMContext) -> None:
    try:
        if await _bypass_stale_order_state(message, state):
            return
        email = _normalize_customer_email(message.text)
        if not email:
            await message.answer("⚠️ Please enter a valid email, example: name@gmail.com")
            return

        data = await state.get_data()
        service_id = data.get("service_id")
        quantity = data.get("quantity")
        total = data.get("total")
        if not service_id or not quantity or total is None:
            await state.clear()
            await message.answer("⚠️ Order session expired. Please choose the product again.")
            return

        db = SessionLocal()
        try:
            service = (
                db.query(Service)
                .options(joinedload(Service.stock))
                .filter(Service.id == int(service_id))
                .first()
            )
            if not service or not service.is_active:
                await message.answer("⚠️ Product is not available anymore.")
                await state.clear()
                return
        finally:
            db.close()

        await state.update_data(customer_email=email, session_started_at=datetime.utcnow().isoformat())
        await _show_order_payment_methods(
            message, state, service=service, quantity=int(quantity), total=float(total)
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Email handler failed: %s", exc)
        try:
            await message.answer("⚠️ Something went wrong. Please tap the product again.")
        except Exception:  # noqa: BLE001
            pass
        await state.clear()


def _post_order_keyboard(order_id: int):
    """My Orders / Report a problem / Main menu — built with live Icon Preset icons."""
    from utils.menu_commands import get_command_map
    from utils.ui_icons import build_ui_icons

    db = SessionLocal()
    try:
        commands = get_command_map(db)
        icons = build_ui_icons(db)
    finally:
        db.close()
    return post_order_actions_keyboard(order_id, commands=commands, icons=icons)


async def _answer_order_result(
    message: Message,
    body: str,
    note: str | None,
    *,
    order: Order | None = None,
    service: Service | None = None,
    status_message: Message | None = None,
    keyboard=None,
) -> None:
    """Send order result, bulk delivery .txt when needed, then stock Note.

    If status_message is set (e.g. “Processing…”), edit that bubble in place so
    the customer sees order details instantly without a blank gap.

    `keyboard` (My Orders / Report a problem / Main menu) attaches to the
    body message itself when the order was delivered (order + service set).
    """
    from utils.ui_icons import label_icons

    if status_message is not None:
        try:
            await status_message.edit_text(body, reply_markup=keyboard, parse_mode="HTML")
        except Exception:  # noqa: BLE001
            await message.answer(body, reply_markup=keyboard, parse_mode="HTML")
    else:
        await message.answer(body, reply_markup=keyboard, parse_mode="HTML")
    if order is not None and service is not None:
        await maybe_send_delivery_file(
            order=order,
            service=service,
            credentials=getattr(order, "delivered_info", None),
            message=message,
        )
    if note:
        icons = label_icons()
        await message.answer(f"{icons['note']} Note:\n{html.escape(note)}", parse_mode="HTML")


@router.callback_query(F.data.startswith("orderpay:"))
async def order_payment_selected(callback: CallbackQuery, state: FSMContext) -> None:
    code = callback.data.rsplit(":", 1)[1]
    data = await state.get_data()
    if not data.get("service_id") or not data.get("quantity"):
        await callback.answer("Order session expired. Choose product again.", show_alert=True)
        return

    # Products that require email must have collected it before payment.
    db_check = SessionLocal()
    try:
        svc = db_check.get(Service, int(data["service_id"]))
        if svc and getattr(svc, "require_email", False) and not (data.get("customer_email") or "").strip():
            await callback.answer("Please enter your email first.", show_alert=True)
            await state.set_state(ProductOrderFlow.waiting_email)
            await callback.message.answer("📧 Enter the email to invite/add for this plan:")
            return
    finally:
        db_check.close()

    if code == "WALLET":
        # 1) Answer callback immediately (Telegram button timeout).
        # 2) Clear session + strip buttons (no double charge).
        # 3) Show an instant chat line, then replace it with order details.
        await callback.answer("Processing wallet payment…")
        await state.clear()
        try:
            if callback.message:
                await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass

        status_msg = await callback.message.answer(
            "⏳ <b>Processing your wallet payment…</b>\nPlease wait — order details will appear here.",
            parse_mode="HTML",
        )
        try:
            response, note, order, service = await create_wallet_order(callback.from_user, data)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Wallet order failed: %s", exc)
            try:
                await status_msg.edit_text(
                    "⚠️ Wallet payment failed. Please try again or contact support.",
                    parse_mode="HTML",
                )
            except Exception:  # noqa: BLE001
                await callback.message.answer(
                    "⚠️ Wallet payment failed. Please try again or contact support."
                )
            return

        keyboard = _post_order_keyboard(order.id) if order is not None else None
        await _answer_order_result(
            callback.message,
            response,
            note,
            order=order,
            service=service,
            status_message=status_msg,
            keyboard=keyboard,
        )
        return

    db = SessionLocal()
    try:
        config = db.query(BotConfig).first()
        method = db.query(PaymentMethod).filter(PaymentMethod.code == code, PaymentMethod.is_active.is_(True)).first()
        if not method:
            await callback.message.answer("⚠️ This payment method is no longer available. Please choose another one.")
            await callback.answer()
            return

        if is_payfast_method(code, method):
            service_id = int(data["service_id"])
            quantity = int(data["quantity"])
            total = float(data["total"])
            customer_email = (data.get("customer_email") or "").strip() or None
            await callback.answer("Opening PayFast…")
            await state.clear()
            try:
                if callback.message:
                    await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:  # noqa: BLE001
                pass
            await start_payfast_order_checkout(
                callback.message,
                from_user=callback.from_user,
                service_id=service_id,
                quantity=quantity,
                total=total,
                customer_email=customer_email,
            )
            return

        destination = get_deposit_destination(code, method, config)
        instruction = build_payment_instruction(code, method, float(data["total"]), destination, db=db)
    finally:
        db.close()

    label = f"{render_icon(method.icon, '💳', html_mode=True)} {html.escape(method.name)}"
    if not destination:
        await callback.message.answer(f"⚠️ {label} payment details are not configured yet. Please contact support.", parse_mode="HTML")
        await callback.answer()
        return
    await state.set_state(ProductOrderFlow.waiting_payment_reference)
    await state.update_data(method=code, destination=destination, session_started_at=datetime.utcnow().isoformat())
    from utils.checkout_expire import unpaid_checkout_expire_minutes

    expire_minutes = unpaid_checkout_expire_minutes()
    await callback.answer()
    await callback.message.answer(
        f"{instruction}\n\n"
        f"After payment, send your reference/TXID here.\n\n"
        f"⚠️ <b>Pay within {expire_minutes} minutes</b> — otherwise the order will expire.",
        parse_mode="HTML",
    )

@router.message(ProductOrderFlow.waiting_payment_reference)
async def order_payment_reference_received(message: Message, state: FSMContext) -> None:
    if await _bypass_stale_order_state(message, state):
        return
    reference = (message.text or "").strip()
    if len(reference) < 4:
        await message.answer("⚠️ Please send a valid payment reference/TXID.")
        return
    data = await state.get_data()
    response, note, order, service = await create_external_payment_order(message.from_user, data, reference)
    await state.clear()
    keyboard = _post_order_keyboard(order.id) if order is not None else None
    await _answer_order_result(message, response, note, order=order, service=service, keyboard=keyboard)


async def create_wallet_order(from_user, data: dict) -> tuple[str, str | None, Order | None, Service | None]:
    db = SessionLocal()
    try:
        user = get_or_create_user(db, str(from_user.id), from_user.username, from_user.full_name)
        service = db.get(Service, int(data["service_id"]))
        quantity = int(data["quantity"])
        total = float(data["total"])
        if not service:
            return "⚠️ Product not found.", None, None, None
        if user.wallet_usdt < total:
            return (
                f"⚠️ Insufficient wallet balance. Required: {format_usdt(total)}, available: {format_usdt(user.wallet_usdt)}.",
                None,
                None,
                None,
            )
        try:
            reserve_stock(db, service.id, quantity)
        except InsufficientStockError as exc:
            db.rollback()
            return f"⚠️ {exc}", None, None, None
        user.wallet_usdt -= total
        order = Order(
            order_code=generate_order_code(db),
            user_id=user.id,
            service_id=service.id,
            link="digital_product_order",
            quantity=quantity,
            amount_usdt=total,
            status="manual_pending",
            order_type="manual",
            payment_method="WALLET",
            customer_email=(data.get("customer_email") or "").strip() or None,
            note="Paid with wallet from Telegram bot.",
        )
        db.add(order)
        db.add(Transaction(user_id=user.id, amount=total, tx_type="deduct", status="confirmed", blockchain_status="confirmed", note=f"Order {order.order_code}"))
        db.flush()
        fulfillment = await fulfill_provider_order(db, order, service)
        db.commit()
        await notify_admin_new_order(order, user, service)
        if order.status == "completed":
            await notify_channel_order_completed(order, service, db)
        note = stock_note_text(service) if order.status == "completed" else None
        from utils.ui_icons import label_icons

        icons = label_icons(db)
        completed_order, completed_service = _detach_completed_for_delivery(db, order, service)
        return (
            f"{icons['tick']} Order placed successfully!\n"
            f"{icons['order']} Order: {order.order_code}{fulfillment}",
            note,
            completed_order,
            completed_service,
        )
    finally:
        db.close()


async def create_external_payment_order(
    from_user, data: dict, reference: str
) -> tuple[str, str | None, Order | None, Service | None]:
    db = SessionLocal()
    try:
        user = get_or_create_user(db, str(from_user.id), from_user.username, from_user.full_name)
        service = db.get(Service, int(data["service_id"]))
        quantity = int(data["quantity"])
        total = float(data["total"])
        code = data.get("method", "BEP20")
        destination = data.get("destination", "")
        method = db.query(PaymentMethod).filter(PaymentMethod.code == code).first()
        config = db.query(BotConfig).first()
        if not service:
            return "⚠️ Product not found.", None, None, None
        from utils.payment_security import payment_ref_already_used

        if payment_ref_already_used(db, reference):
            return "⚠️ This payment reference/TXID is already submitted.", None, None, None
        try:
            reserve_stock(db, service.id, quantity)
        except InsufficientStockError as exc:
            db.rollback()
            return f"⚠️ {exc}", None, None, None
        order = Order(
            order_code=generate_order_code(db),
            user_id=user.id,
            service_id=service.id,
            link="digital_product_order",
            quantity=quantity,
            amount_usdt=total,
            status="pending",
            order_type="manual",
            payment_method=(code or "UNKNOWN").upper(),
            customer_email=(data.get("customer_email") or "").strip() or None,
            note=f"Awaiting {code} payment verification. Reference: {reference}",
        )
        db.add(order)
        tx = Transaction(user_id=user.id, amount=total, tx_type="deposit", tx_hash=reference, status="pending", blockchain_status="pending", note=f"Payment for order {order.order_code} via {code}")
        db.add(tx)
        db.flush()

        # A payment is auto-verified only if BOTH the global switch is on AND this
        # specific payment method is marked "auto" by the admin. New manual methods
        # (JazzCash, EasyPaisa, Bank Transfer, or any admin-added manual method)
        # always go to admin review instead of calling the blockchain verifier.
        method_type = method.method_type if method else ("auto" if code in {"BEP20", "TRC20"} else "manual")
        should_auto_verify = bool(config and config.auto_verify_enabled) and method_type == "auto"

        if not should_auto_verify:
            db.commit()
            await notify_admin_new_order(order, user, service)
            return (
                f"⏳ Payment submitted for admin review.\nOrder: {order.order_code}\nStatus: pending",
                None,
                None,
                None,
            )

        from utils.payment_security import MAX_FAILED_VERIFICATIONS, recent_failed_verification_count

        if recent_failed_verification_count(db, user.id) >= MAX_FAILED_VERIFICATIONS:
            tx.status = "rejected"
            tx.blockchain_status = "failed"
            order.status = "cancelled"
            order.note = "Cancelled: too many failed payment verification attempts."
            try:
                from utils.stock_manager import release_stock

                release_stock(db, service.id, quantity)
            except Exception:  # noqa: BLE001
                pass
            db.commit()
            await send_admin_message(
                f"🚫 Repeated failed order payment verification attempts\n"
                f"Order: {order.order_code}\nUser: {user.telegram_id}\n"
                f"Method: {code}\nLatest reference: {reference}\n"
                f"Blocked further auto-verify attempts — needs manual review.",
                db=db,
            )
            return "❌ Too many failed verification attempts. Please contact admin/support.", None, None, None

        result = await verify_payment(code, reference, total, destination)
        verification = PaymentVerification(
            transaction_id=tx.id,
            tx_hash=reference,
            blockchain=code,
            contract_address=result.contract_address,
            from_address=result.from_address,
            to_address=result.to_address,
            amount_verified=result.amount,
            verification_status=result.status,
            reason=result.reason,
            api_response=result.raw_json(),
        )
        db.add(verification)
        if result.verified:
            if payment_ref_already_used(db, reference, exclude_transaction_id=tx.id):
                verification.verification_status = "failed"
                verification.reason = "Duplicate TXID already credited on another deposit"
                tx.status = "rejected"
                tx.blockchain_status = "failed"
                order.status = "cancelled"
                order.note = "Cancelled: payment reference already used on another deposit."
                try:
                    from utils.stock_manager import release_stock

                    release_stock(db, service.id, quantity)
                except Exception:  # noqa: BLE001
                    pass
                db.commit()
                return "⚠️ This payment reference/TXID was already used on another deposit.", None, None, None
            if int(order.user_id) != int(tx.user_id):
                tx.status = "rejected"
                tx.blockchain_status = "failed"
                verification.verification_status = "failed"
                verification.reason = "Order/user mismatch — payment not applied"
                db.commit()
                return "⚠️ Payment could not be applied to this order. Contact support.", None, None, None
            tx.status = "confirmed"
            tx.blockchain_status = "confirmed"
            tx.verified_at = datetime.utcnow()
            verification.verified_at = tx.verified_at
            order.status = "manual_pending"
            order.note = f"Paid via {code}. Reference: {reference}"
            fulfillment = await fulfill_provider_order(db, order, service)
            db.commit()
            await notify_admin_new_order(order, user, service)
            if order.status == "completed":
                await notify_channel_order_completed(order, service, db)
            note = stock_note_text(service) if order.status == "completed" else None
            from utils.ui_icons import label_icons

            icons = label_icons(db)
            completed_order, completed_service = _detach_completed_for_delivery(db, order, service)
            return (
                f"{icons['tick']} Payment verified and order placed!\n"
                f"{icons['order']} Order: {order.order_code}{fulfillment}",
                note,
                completed_order,
                completed_service,
            )
        db.commit()
        await notify_admin_new_order(order, user, service)
        reason = result.reason or result.status or "not verified"
        await send_admin_message(
            f"⚠️ Order payment not auto-verified\n"
            f"Order: {order.order_code}\nUser: {user.telegram_id}\n"
            f"Method: {code}\nAmount: {total}\nTX: {reference}\n"
            f"Status: {result.status}\nReason: {reason}",
            db=db,
        )
        return (
            f"⏳ Payment submitted for verification.\n"
            f"Order: {order.order_code}\n"
            "Status: pending\n"
            "We'll notify you once it's confirmed.",
            None,
            None,
            None,
        )
    finally:
        db.close()


def _detach_completed_for_delivery(db, order: Order, service: Service):
    """Keep delivery fields usable after SessionLocal closes (expire_on_commit)."""
    if order is None or getattr(order, "status", None) != "completed":
        return None, None
    # Touch attrs while the session is open so they are not expired/lazy-load later.
    _ = (
        order.delivered_info,
        order.order_code,
        order.quantity,
        order.amount_usdt,
        service.name,
        getattr(service, "warranty", None),
    )
    db.expunge(order)
    db.expunge(service)
    return order, service


async def fulfill_provider_order(db, order: Order, service: Service) -> str:
    fulfillment_type = getattr(service, "fulfillment_type", "auto")

    if fulfillment_type == "stock":
        return fulfill_stock_order(db, order, service)

    if fulfillment_type == "canva":
        if not (order.customer_email or "").strip():
            order.status = "manual_pending"
            order.note = "Canva automation requires customer email."
            return "\n⚠️ Canva email is missing; admin review required."
        order.status = "manual_pending"
        order.note = "Paid — queued for automatic Canva Education email invitation."
        return "\n\n⏳ Payment confirmed. Your Canva Education invitation will be sent automatically to your order email."

    provider = service.provider
    if fulfillment_type == "manual" or not provider or provider.type != "api" or not service.provider_service_id:
        return "\nDelivery: pending admin processing."
    try:
        response = await place_order(
            provider,
            service.provider_service_id,
            order.link,
            order.quantity,
            external_order_id=order.order_code,
        )
    except Exception as exc:  # noqa: BLE001 - any provider/network failure must degrade
        # to manual processing, never blow up the whole order (which would look like
        # a random "payment failed" to the customer even though wallet/stock already
        # committed). ProviderApiError is the expected case; broadened to catch
        # unexpected network/parsing errors too so the order still completes instantly
        # from the customer's side.
        order.status = "manual_pending"
        order.note = f"Provider API failed: {exc}"
        logger.warning(f"[PROVIDER FULFILL FAILED] order={order.order_code} error={exc}")
        # Customer never sees the raw provider debug dump (URLs/status codes/JSON) —
        # that stays in order.note for the admin panel / Refund Tool / admin DM.
        return "\n⚠️ Your order is confirmed — our team is completing delivery manually and will notify you shortly."

    # DIAGNOSTIC LOGGING - check Railway Deploy Logs for this line after a test order
    # to see the exact JSON shape your provider returns. Remove once auto-delivery
    # keys are confirmed and added to extract_delivery_items().
    logger.info(f"[PROVIDER RAW RESPONSE] order={order.order_code} purchase_response={response}")

    provider_oid = extract_provider_order_id(response)
    # Never fall back to our SMM order code — that made status polling look up a
    # fake id forever while the provider wallet was never charged.
    order.provider_order_id = provider_oid or None
    delivery_response = response
    if provider_oid and not extract_provider_delivery_items(response):
        # Provider often confirms the order first and only attaches the actual
        # account/credentials a few seconds later. One instant check used to
        # miss that window and fall straight to "processing" (customer saw a
        # processing message, then delivery ~1-2 min later via the background
        # poller). Give it a few quick, short-spaced re-checks here instead —
        # still one synchronous reply to the customer, direct delivery if the
        # provider finishes in time, "processing" only as a last resort.
        for attempt in range(4):
            try:
                details = await get_order_details(provider, provider_oid)
                logger.info(f"[PROVIDER RAW RESPONSE] order={order.order_code} detail_response={details}")
                if extract_provider_delivery_items(details) or details.get("status") or details.get("success"):
                    delivery_response = merge_provider_responses(response, details)
                if extract_provider_delivery_items(delivery_response):
                    break
            except Exception as exc:  # noqa: BLE001 - optional lookup, never let it break the order
                logger.info(f"[PROVIDER RAW RESPONSE] order={order.order_code} detail_fetch_failed={exc}")
            if attempt < 3:
                await asyncio.sleep(2)
    order.provider_status = extract_provider_status(response)
    delivered_items = extract_provider_delivery_items(delivery_response)
    if provider_response_has_delivery(delivery_response):
        order.status = "completed"
        order.completed_at = datetime.utcnow()
        if delivered_items:
            order.delivered_info = "\n".join(delivered_items)
        complete_reserved_stock(db, order.service_id, order.quantity)
        order.note = "Auto-delivered to customer via provider API."
    elif provider_oid:
        order.status = "processing"
        order.note = "Submitted to provider API — awaiting delivery."
    else:
        order.status = "manual_pending"
        order.note = (
            "Provider API returned HTTP OK but no order id or delivery credentials "
            "(buy may not have charged the provider). Admin will process manually."
        )
        return (
            "\n⚠️ Provider did not confirm the purchase; admin will process manually."
        )

    return _customer_fulfillment_message(order, service, delivered_items)


def _customer_fulfillment_message(order: Order, service: Service, delivered_items: list[str] | str) -> str:
    """One customer-facing delivery block — same copyable box for every payment method."""
    if isinstance(delivered_items, str):
        delivered_items = [line for line in delivered_items.splitlines() if line.strip()] or [delivered_items]
    if delivered_items:
        return "\n\n" + format_delivery_receipt_html(order, service, "\n".join(delivered_items))
    if order.status == "processing":
        return "\n\n⏳ Your order is being processed. You will receive your item shortly."
    return ""


def fulfill_stock_order(db, order: Order, service: Service) -> str:
    """'stock' fulfillment_type products ke liye: admin ne Stock Management mein
    jo account/login lines dal rakhi hain, un mein se order.quantity lines
    customer ko turant deliver kar deta hai aur wo lines dobara istemal ke liye
    stock se nikal deta hai. Agar itni ready lines na hon to order manual_pending
    reh jata hai taake admin baad mein Orders page se complete kar sake."""
    delivered = consume_stock_account(db, service.id, order.quantity)
    if delivered is None:
        order.status = "manual_pending"
        order.note = "Stock delivery: not enough account details entered in stock yet. Admin will complete manually."
        return "\n⚠️ Stock delivery: no ready account details found; admin will process this manually."

    delivered_text = "\n".join(delivered)
    order.status = "completed"
    order.completed_at = datetime.utcnow()
    order.delivered_info = delivered_text
    order.note = "Delivered automatically from stock."
    complete_reserved_stock(db, order.service_id, order.quantity)
    return _customer_fulfillment_message(order, service, delivered_text)


def extract_order_status(response: dict) -> str:
    return extract_provider_status(response)


def extract_provider_order_code(response: dict) -> str:
    return extract_provider_order_id(response)


def has_immediate_delivery(response: dict) -> bool:
    return provider_response_has_delivery(response)


def format_provider_delivery(response: dict) -> str:
    # NOTE: provider order id intentionally NOT shown to the customer anymore.
    lines: list[str] = []

    delivered = extract_provider_delivery_items(response)
    if delivered:
        lines.append("\n\n📦 Your account details:")
        lines.extend(delivered)
    elif response.get("success") is True:
        lines.append("\n\n⏳ Your order has been submitted and is being processed. You will receive your item shortly.")
    elif response.get("message"):
        lines.append(f"\n\n{str(response['message'])[:300]}")

    return "\n".join(lines) if lines else ""


def extract_delivery_items(response: dict) -> list[str]:
    return extract_provider_delivery_items(response)
