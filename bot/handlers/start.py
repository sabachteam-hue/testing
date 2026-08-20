from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from bot.handlers.force_join import send_force_join_gate
from bot.keyboards import answer_with_start_menu, send_quick_reply_menu
from database.models import BotConfig, SessionLocal, User
from utils.force_join import (
    is_admin_telegram_id,
    load_force_join_targets,
    restore_user_bot_commands,
    user_may_use_bot,
)
from utils.helpers import get_or_create_user, get_referral_settings, resolve_welcome_msg
from utils.notifications import notify_referrer_new_join
from utils.menu_commands import get_command_map

router = Router()


@router.message(CommandStart())
async def start_command(message: Message) -> None:
    db = SessionLocal()
    try:
        args = (message.text or "").split(maxsplit=1)
        start_payload = args[1].strip() if len(args) > 1 else ""
        referral_code = (
            start_payload.replace("ref_", "", 1)
            if start_payload.startswith("ref_")
            else None
        )
        open_products = start_payload.lower() in {"products", "shop", "catalog"}
        is_new_user = db.query(User).filter(User.telegram_id == str(message.from_user.id)).first() is None
        user = get_or_create_user(
            db,
            telegram_id=str(message.from_user.id),
            username=message.from_user.username,
            full_name=message.from_user.full_name,
            referral_code=referral_code,
        )
        referrer_telegram_id = None
        referral_settings = None
        if is_new_user and user.referrer_id:
            referrer = db.get(User, user.referrer_id)
            if referrer:
                referrer_telegram_id = referrer.telegram_id
                referral_settings = get_referral_settings(db)
        config = db.query(BotConfig).first()
        welcome = resolve_welcome_msg(config, db)
        commands = get_command_map(db)
        force_targets = load_force_join_targets(db)
    finally:
        db.close()

    if referrer_telegram_id and referral_settings and referral_settings["enabled"]:
        await notify_referrer_new_join(
            referrer_telegram_id,
            message.from_user.username,
            message.from_user.full_name,
            referral_settings,
        )

    show_admin = is_admin_telegram_id(message.from_user.id)

    # Gate everyone (old + new) until they confirm — even if already in channel/group.
    if force_targets.active and not show_admin:
        if not await user_may_use_bot(message.bot, message.from_user.id):
            await send_force_join_gate(message)
            return

    # View Product deep-link (and shop/catalog aliases) → same UI as /products.
    if open_products:
        from bot.handlers.products import products_command

        await products_command(message)
        return

    # Welcome first so /start never stays silent if later Telegram API calls fail.
    await answer_with_start_menu(message, welcome, commands, show_admin=show_admin)
    await send_quick_reply_menu(message, commands, show_admin=show_admin)

    try:
        await restore_user_bot_commands(message.bot, message.chat.id)
    except Exception:  # noqa: BLE001
        pass
    try:
        from bot.bot_main import apply_mini_app_menu_button

        await apply_mini_app_menu_button(message.bot, message.chat.id)
    except Exception:  # noqa: BLE001
        pass
