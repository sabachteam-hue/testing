import html
from datetime import timedelta

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.orm import joinedload

from bot.keyboards import orders_list_keyboard, report_issue_keyboard
from database.models import IssueReport, Order, SessionLocal
from utils.helpers import format_usdt, get_or_create_user
from utils.notifications import format_delivery_receipt_html, maybe_send_delivery_file, stock_note_text
from utils.menu_commands import MenuCommandFilter
from utils.refund_tool import parse_subscription_days, purchase_date_for_refund
from utils.ui_icons import build_ui_icons, label_icons


router = Router()


class ReportIssueFlow(StatesGroup):
    waiting_message = State()


@router.message(Command("orders"))
@router.message(MenuCommandFilter("orders"))
async def orders_command(message: Message) -> None:
    db = SessionLocal()
    try:
        user = get_or_create_user(db, str(message.from_user.id), message.from_user.username, message.from_user.full_name)
        # Clients only see successfully completed purchases — hide expired/pending/failed.
        orders = (
            db.query(Order)
            .options(joinedload(Order.service))
            .filter(Order.user_id == user.id, Order.status == "completed")
            .order_by(Order.created_at.desc())
            .limit(20)
            .all()
        )
        if not orders:
            await message.answer("No completed orders found.")
            return
        markup = orders_list_keyboard(orders)
        icons = label_icons(db)
    finally:
        db.close()
    await message.answer(
        f"{icons['orders']} PURCHASE HISTORY\n\nSelect an order to view details:",
        reply_markup=markup,
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("orderview:"))
async def order_detail_callback(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":", 1)[1])
    db = SessionLocal()
    note = None
    note_icon = None
    order = None
    service = None
    delivered_info = None
    lines: list[str] = []
    try:
        user = get_or_create_user(db, str(callback.from_user.id), callback.from_user.username, callback.from_user.full_name)
        order = (
            db.query(Order)
            .options(joinedload(Order.service))
            .filter(
                Order.id == order_id,
                Order.user_id == user.id,
                Order.status == "completed",
            )
            .first()
        )
        if not order:
            await callback.answer("Order not found", show_alert=True)
            return

        icons = label_icons(db)
        ui_icons = build_ui_icons(db)
        lines = [
            f"{icons['details']} ORDER DETAILS",
            "",
            f"{icons['order']} Order code: {html.escape(order.order_code)}",
            f"{icons['product']} Product: {html.escape(order.service.name)}",
            f"{icons['quantity']} Quantity: {order.quantity}",
            f"{icons['price']} Amount: {html.escape(format_usdt(order.amount_usdt))}",
            f"{icons['status']} Status: completed",
            f"{icons['time']} Time: {order.created_at:%d/%m/%Y %H:%M}",
        ]
        if order.delivered_info:
            lines.append("")
            lines.append(format_delivery_receipt_html(order, order.service, order.delivered_info))
        else:
            lines.append("")
            lines.append("🔐 Delivered account: not recorded for this order.")
        note = stock_note_text(order.service)
        note_icon = icons["note"]
        delivered_info = order.delivered_info
        service = order.service
        # Touch + detach so attrs survive expire_on_commit / session close.
        _ = (order.order_code, order.quantity, order.amount_usdt, delivered_info)
        if service is not None:
            _ = (service.name, getattr(service, "warranty", None))
            db.expunge(service)
        db.expunge(order)
    finally:
        db.close()
    markup = report_issue_keyboard(order.id, ui_icons) if order is not None else None
    await callback.message.answer("\n".join(lines), reply_markup=markup, parse_mode="HTML")
    if order is not None and delivered_info:
        await maybe_send_delivery_file(
            order=order,
            service=service,
            credentials=delivered_info,
            message=callback.message,
        )
    if note:
        await callback.message.answer(f"{note_icon} Note:\n{html.escape(note)}", parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("reportissue:"))
async def report_issue_start(callback: CallbackQuery, state: FSMContext) -> None:
    order_id = int(callback.data.split(":", 1)[1])
    db = SessionLocal()
    try:
        user = get_or_create_user(db, str(callback.from_user.id), callback.from_user.username, callback.from_user.full_name)
        order = (
            db.query(Order)
            .options(joinedload(Order.service))
            .filter(
                Order.id == order_id,
                Order.user_id == user.id,
                Order.status == "completed",
            )
            .first()
        )
        if not order:
            await callback.answer("Order not found", show_alert=True)
            return

        icons = label_icons(db)
        order_code = order.order_code
        product_name = order.service.name if order.service else "—"

        covered_line = None
        warranty = getattr(order.service, "warranty", None) if order.service else None
        days = parse_subscription_days(warranty)
        if days:
            covered_until = purchase_date_for_refund(order) + timedelta(days=days)
            covered_line = f"{icons.get('time', '🕒')} Covered until {covered_until:%Y-%m-%d %H:%M} UTC"
    finally:
        db.close()

    lines = [
        f"{icons.get('report', '🛡')} Report a problem",
        "",
        f"{icons['order']} Order {html.escape(order_code)} — {html.escape(product_name)}",
    ]
    if covered_line:
        lines.append(covered_line)
    lines.append("")
    lines.append(
        "Tell us what went wrong, in a sentence or two. "
        "What you write goes straight to the shop owner, so include anything that would help — "
        "when it stopped working, what you saw."
    )

    await state.set_state(ReportIssueFlow.waiting_message)
    await state.update_data(order_id=order_id, order_code=order_code)
    await callback.message.answer("\n".join(lines), parse_mode="HTML")
    await callback.answer()


@router.message(ReportIssueFlow.waiting_message)
async def report_issue_received(message: Message, state: FSMContext) -> None:
    from utils.menu_commands import text_matches_any_menu

    text = (message.text or "").strip()

    db = SessionLocal()
    try:
        if text_matches_any_menu(text, db):
            await state.clear()
            from aiogram.dispatcher.event.bases import SkipHandler

            raise SkipHandler
    finally:
        db.close()

    if len(text) < 3:
        await message.answer("⚠️ Please describe the issue in a bit more detail.")
        return

    data = await state.get_data()
    order_id = data.get("order_id")
    order_code = data.get("order_code") or ""
    if not order_id:
        await state.clear()
        await message.answer("⚠️ Session expired. Please open the order again and tap Report a problem.")
        return

    db = SessionLocal()
    try:
        user = get_or_create_user(db, str(message.from_user.id), message.from_user.username, message.from_user.full_name)
        order = db.query(Order).filter(Order.id == int(order_id), Order.user_id == user.id).first()
        if not order:
            await state.clear()
            await message.answer("⚠️ Order not found.")
            return
        db.add(
            IssueReport(
                order_id=order.id,
                order_code=order_code or order.order_code,
                user_id=user.id,
                message=text[:2000],
            )
        )
        db.commit()
        icons = label_icons(db)
    finally:
        db.close()

    await state.clear()
    await message.answer(
        f"{icons['tick']} Thanks — your report has been sent to the shop owner. We'll get back to you soon.",
        parse_mode="HTML",
    )
