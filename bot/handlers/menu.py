from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.orm import joinedload

from bot.handlers.profile import build_profile_text
from bot.handlers.support import build_support_text
from bot.keyboards import (
    answer_with_start_menu,
    language_keyboard,
    orders_list_keyboard,
    products_keyboard,
    send_quick_reply_menu,
    services_keyboard,
    start_menu_keyboard,
    wallet_full_keyboard,
)
from database.models import (
    BotConfig,
    Category,
    Order,
    Service,
    SessionLocal,
    get_active_languages,
    get_active_payment_methods,
)
from utils.catalog_image import (
    category_catalog_item,
    payment_method_catalog_item,
    send_catalog_photo,
    service_catalog_item,
)
from utils.force_join import is_admin_telegram_id, restore_user_bot_commands
from utils.helpers import get_or_create_user, resolve_welcome_msg
from utils.menu_commands import MenuCommandFilter, get_command_map
from utils.pricing import service_unit_prices
from utils.ui_icons import build_ui_icons, wallet_screen_copy
from utils.stock_display import build_stock_legend, preset_icon

router = Router()


def _user_service_prices(db, services, tg_user) -> dict[int, float]:
    if not services or tg_user is None:
        return {}
    user = get_or_create_user(db, str(tg_user.id), tg_user.username, tg_user.full_name)
    return service_unit_prices(db, services, user)


class CatalogSearch(StatesGroup):
    waiting_query = State()


@router.message(Command("menu"))
async def menu_command(message: Message) -> None:
    db = SessionLocal()
    try:
        config = db.query(BotConfig).first()
        welcome = resolve_welcome_msg(config, db)
        commands = get_command_map(db)
        stock_legend = build_stock_legend(db)
    finally:
        db.close()
    show_admin = is_admin_telegram_id(message.from_user.id if message.from_user else None)
    await answer_with_start_menu(message, welcome, commands, show_admin=show_admin)
    await send_quick_reply_menu(message, commands, show_admin=show_admin)
    try:
        await restore_user_bot_commands(message.bot, message.chat.id)
    except Exception:  # noqa: BLE001
        pass
    try:
        from aiogram.types import MenuButtonCommands

        await message.bot.set_chat_menu_button(
            chat_id=message.chat.id,
            menu_button=MenuButtonCommands(),
        )
    except Exception:  # noqa: BLE001
        pass


@router.message(Command("catalog"))
@router.message(MenuCommandFilter("catalog"))
async def catalog_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    db = SessionLocal()
    try:
        categories = (
            db.query(Category)
            .options(joinedload(Category.services).joinedload(Service.stock))
            .filter(Category.is_active.is_(True))
            .order_by(Category.sort_order.asc(), Category.name.asc())
            .all()
        )
        if not categories:
            services = (
                db.query(Service)
                .options(joinedload(Service.stock))
                .filter(Service.is_active.is_(True), Service.is_deleted.is_(False))
                .order_by(Service.sort_order.asc(), Service.name.asc())
                .all()
            )
        else:
            services = []
        category_items = [category_catalog_item(category) for category in categories]
        service_items = [service_catalog_item(service) for service in services]
        prices = _user_service_prices(db, services, message.from_user)
        commands = get_command_map(db)
        icons = build_ui_icons(db)
        stock_legend = build_stock_legend(db)
    finally:
        db.close()
    if categories:
        await send_catalog_photo(
            message,
            category_items,
            title="Categories",
            caption=f"{stock_legend}\nChoose a category:",
            reply_markup=products_keyboard(categories, commands=commands, icons=icons),
        )
        return
    if services:
        await send_catalog_photo(
            message,
            service_items,
            title="Available Products",
            caption=stock_legend,
            reply_markup=services_keyboard(services, commands=commands, icons=icons, prices=prices),
        )
        return
    await message.answer("No categories or products are available yet.")


@router.callback_query(F.data == "menu:catalog")
async def menu_catalog(callback: CallbackQuery) -> None:
    db = SessionLocal()
    try:
        categories = (
            db.query(Category)
            .options(joinedload(Category.services).joinedload(Service.stock))
            .filter(Category.is_active.is_(True))
            .order_by(Category.sort_order.asc(), Category.name.asc())
            .all()
        )
        if not categories:
            services = (
                db.query(Service)
                .options(joinedload(Service.stock))
                .filter(Service.is_active.is_(True), Service.is_deleted.is_(False))
                .order_by(Service.sort_order.asc(), Service.name.asc())
                .all()
            )
        else:
            services = []
        category_items = [category_catalog_item(category) for category in categories]
        service_items = [service_catalog_item(service) for service in services]
        prices = _user_service_prices(db, services, callback.from_user)
        commands = get_command_map(db)
        icons = build_ui_icons(db)
        stock_legend = build_stock_legend(db)
    finally:
        db.close()
    if categories:
        await send_catalog_photo(
            callback.message,
            category_items,
            title="Categories",
            caption=f"{stock_legend}\nChoose a category:",
            reply_markup=products_keyboard(categories, commands=commands, icons=icons),
        )
        await callback.answer()
        return
    if services:
        await send_catalog_photo(
            callback.message,
            service_items,
            title="Available Products",
            caption=stock_legend,
            reply_markup=services_keyboard(services, commands=commands, icons=icons, prices=prices),
        )
        await callback.answer()
        return
    await callback.message.answer("No categories or products are available yet.")
    await callback.answer()


@router.callback_query(F.data == "catalog:search")
async def catalog_search_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CatalogSearch.waiting_query)
    await callback.message.answer("🔍 Send a product name to search (or part of the name):")
    await callback.answer()


@router.message(CatalogSearch.waiting_query)
async def catalog_search_query(message: Message, state: FSMContext) -> None:
    from utils.helpers import strip_html_tags
    from utils.menu_commands import text_matches_any_menu

    text = (message.text or "").strip()
    db = SessionLocal()
    try:
        if text_matches_any_menu(text, db):
            await state.clear()
            # Let the normal menu handlers run for this message.
            from aiogram.dispatcher.event.bases import SkipHandler

            raise SkipHandler
        needle = text.lower()
        if len(needle) < 1:
            await message.answer("⚠️ Please type at least 1 character to search.")
            return
        services = (
            db.query(Service)
            .options(joinedload(Service.stock))
            .filter(Service.is_active.is_(True), Service.is_deleted.is_(False))
            .order_by(Service.sort_order.asc(), Service.name.asc())
            .all()
        )
        matched = [
            service
            for service in services
            if needle in strip_html_tags(service.name or "").lower()
            or needle in (service.sku or "").lower()
        ]
        commands = get_command_map(db)
        icons = build_ui_icons(db)
        stock_legend = build_stock_legend(db)
        items = [service_catalog_item(service) for service in matched]
        prices = _user_service_prices(db, matched, message.from_user)
    finally:
        db.close()
    await state.clear()
    if not matched:
        await message.answer(f"No products found for “{text}”. Try another search from Catalog.")
        return
    await send_catalog_photo(
        message,
        items,
        title="Search results",
        caption=f"{stock_legend}\nResults for “{text}”:",
        reply_markup=services_keyboard(matched, commands=commands, icons=icons, prices=prices),
    )


@router.callback_query(F.data == "menu:shopall")
async def menu_shop_all(callback: CallbackQuery) -> None:
    db = SessionLocal()
    try:
        services = (
            db.query(Service)
            .options(joinedload(Service.stock))
            .filter(Service.is_active.is_(True), Service.is_deleted.is_(False))
            .order_by(Service.sort_order.asc(), Service.name.asc())
            .all()
        )
        service_items = [service_catalog_item(service) for service in services]
        prices = _user_service_prices(db, services, callback.from_user)
        commands = get_command_map(db)
        icons = build_ui_icons(db)
        stock_legend = build_stock_legend(db)
    finally:
        db.close()
    if not services:
        await callback.message.answer("No products are available yet.")
        await callback.answer()
        return
    await send_catalog_photo(
        callback.message,
        service_items,
        title="Available Products",
        caption=stock_legend,
        reply_markup=services_keyboard(services, commands=commands, icons=icons, prices=prices),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:refer")
async def menu_refer(callback: CallbackQuery) -> None:
    from bot.handlers.referral import referral_command

    await referral_command(callback.message)
    await callback.answer()


@router.callback_query(F.data == "menu:profile")
async def menu_profile(callback: CallbackQuery) -> None:
    me = await callback.bot.get_me()
    text = await build_profile_text(me.username, callback.from_user)
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data == "menu:orders")
async def menu_orders(callback: CallbackQuery) -> None:
    db = SessionLocal()
    try:
        user = get_or_create_user(db, str(callback.from_user.id), callback.from_user.username, callback.from_user.full_name)
        orders = (
            db.query(Order)
            .options(joinedload(Order.service))
            .filter(Order.user_id == user.id)
            .order_by(Order.created_at.desc())
            .limit(20)
            .all()
        )
        if not orders:
            await callback.message.answer("No orders found.")
            await callback.answer()
            return
        markup = orders_list_keyboard(orders)
        from utils.ui_icons import label_icons

        icons = label_icons(db)
    finally:
        db.close()
    await callback.message.answer(
        f"{icons['orders']} PURCHASE HISTORY\n\nSelect an order to view details:",
        reply_markup=markup,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "menu:wallet")
async def menu_wallet(callback: CallbackQuery) -> None:
    db = SessionLocal()
    try:
        user = get_or_create_user(db, str(callback.from_user.id), callback.from_user.username, callback.from_user.full_name)
        methods = get_active_payment_methods(db)
        commands = get_command_map(db)
        icons = build_ui_icons(db)
        title, text = wallet_screen_copy(db, wallet_usdt=user.wallet_usdt, referral_wallet=user.referral_wallet)
        items = [payment_method_catalog_item(method) for method in methods]
    finally:
        db.close()
    if items:
        await send_catalog_photo(
            callback.message,
            items,
            title=title,
            caption=text,
            reply_markup=wallet_full_keyboard(methods, commands=commands, icons=icons),
        )
    else:
        await callback.message.answer(
            text,
            reply_markup=wallet_full_keyboard(methods, commands=commands, icons=icons),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == "wallet:refresh")
async def wallet_refresh(callback: CallbackQuery) -> None:
    db = SessionLocal()
    try:
        user = get_or_create_user(db, str(callback.from_user.id), callback.from_user.username, callback.from_user.full_name)
        methods = get_active_payment_methods(db)
        commands = get_command_map(db)
        icons = build_ui_icons(db)
        _title, text = wallet_screen_copy(db, wallet_usdt=user.wallet_usdt, referral_wallet=user.referral_wallet)
    finally:
        db.close()
    await callback.message.answer(
        text,
        reply_markup=wallet_full_keyboard(methods, commands=commands, icons=icons),
        parse_mode="HTML",
    )
    await callback.answer("Balance refreshed")


@router.callback_query(F.data == "menu:back")
async def menu_back(callback: CallbackQuery) -> None:
    db = SessionLocal()
    try:
        config = db.query(BotConfig).first()
        welcome = resolve_welcome_msg(config, db)
        commands = get_command_map(db)
    finally:
        db.close()
    show_admin = is_admin_telegram_id(callback.from_user.id if callback.from_user else None)
    await answer_with_start_menu(callback.message, welcome, commands, show_admin=show_admin)
    await send_quick_reply_menu(callback.message, commands, show_admin=show_admin)
    await callback.answer()


@router.callback_query(F.data == "menu:admin")
async def menu_admin(callback: CallbackQuery) -> None:
    from bot.handlers.admin_bot import deliver_admin_panel_access, is_admin_user_id

    if not callback.from_user or not is_admin_user_id(callback.from_user.id):
        await callback.answer("🚫 No access", show_alert=True)
        return
    await deliver_admin_panel_access(callback.message)
    await callback.answer()


@router.callback_query(F.data == "menu:api")
async def menu_api(callback: CallbackQuery) -> None:
    from bot.handlers.api_keys import send_api_panel
    from database.models import ApiKey
    from utils.helpers import generate_api_credentials

    db = SessionLocal()
    try:
        user = get_or_create_user(db, str(callback.from_user.id), callback.from_user.username, callback.from_user.full_name)
        active = db.query(ApiKey).filter(ApiKey.user_id == user.id, ApiKey.is_active.is_(True)).first()
        flash = None
        if not active:
            key, _ = generate_api_credentials(db, user)
            flash = f"✅ API key created automatically:\n<code>{key.api_key}</code>"
    finally:
        db.close()
    await send_api_panel(callback.message, callback.from_user, flash=flash)
    await callback.answer()


@router.callback_query(F.data == "menu:support")
async def menu_support(callback: CallbackQuery) -> None:
    db = SessionLocal()
    try:
        config = db.query(BotConfig).first()
        text = build_support_text(config, db=db)
    finally:
        db.close()
    await callback.message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
    await callback.answer()


@router.callback_query(F.data == "menu:language")
async def menu_language(callback: CallbackQuery) -> None:
    db = SessionLocal()
    try:
        user = get_or_create_user(db, str(callback.from_user.id), callback.from_user.username, callback.from_user.full_name)
        languages = get_active_languages(db)
        current_code = user.language
    finally:
        db.close()
    if not languages:
        await callback.message.answer("No languages are configured yet.")
        await callback.answer()
        return
    await callback.message.answer("Choose your language:", reply_markup=language_keyboard(languages, current_code))
    await callback.answer()


@router.callback_query(F.data.startswith("lang:"))
async def language_selected(callback: CallbackQuery) -> None:
    code = callback.data.rsplit(":", 1)[1]
    db = SessionLocal()
    try:
        user = get_or_create_user(db, str(callback.from_user.id), callback.from_user.username, callback.from_user.full_name)
        user.language = code
        db.commit()
    finally:
        db.close()
    await callback.message.answer(f"Language updated to {code}.")
    await callback.answer()
