import asyncio
import logging
import os

from aiogram import Bot, Dispatcher

from bot.handlers import admin_bot, api_keys, force_join, menu, orders, products, profile, referral, start, support, wallet
from bot.middleware import ForceJoinMiddleware, MaintenanceMiddleware, SilenceCheckoutNotifyMiddleware

_dispatcher: Dispatcher | None = None


def get_bot_token() -> str:
    return os.getenv("BOT_TOKEN", "").strip().strip('"').strip("'")


def create_bot() -> Bot | None:
    token = get_bot_token()
    if not token:
        return None
    return Bot(token=token)


def build_dispatcher() -> Dispatcher:
    global _dispatcher
    if _dispatcher is not None:
        return _dispatcher

    dispatcher = Dispatcher()
    silence = SilenceCheckoutNotifyMiddleware()
    maintenance_mw = MaintenanceMiddleware()
    force_join_mw = ForceJoinMiddleware()
    # Maintenance first so clients never hit shop handlers while updating.
    dispatcher.message.middleware(maintenance_mw)
    dispatcher.callback_query.middleware(maintenance_mw)
    dispatcher.message.middleware(silence)
    dispatcher.callback_query.middleware(silence)
    dispatcher.message.middleware(force_join_mw)
    dispatcher.callback_query.middleware(force_join_mw)
    for router in (
        start.router,
        force_join.router,
        menu.router,
        products.router,
        wallet.router,
        api_keys.router,
        orders.router,
        referral.router,
        support.router,
        profile.router,
        admin_bot.router,
    ):
        dispatcher.include_router(router)
    _dispatcher = dispatcher
    return dispatcher


async def start_bot() -> None:
    bot = create_bot()
    if not bot:
        logging.warning("BOT_TOKEN is not configured; Telegram bot polling is disabled.")
        return

    dispatcher = build_dispatcher()
    await register_bot_commands(bot)
    await apply_mini_app_menu_button(bot)
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()


async def setup_webhook_bot(webhook_url: str) -> tuple[Bot, Dispatcher]:
    bot = create_bot()
    if not bot:
        raise RuntimeError("BOT_TOKEN is not configured")
    dispatcher = build_dispatcher()
    await register_bot_commands(bot)
    await apply_mini_app_menu_button(bot)
    await bot.set_webhook(
        webhook_url,
        allowed_updates=dispatcher.resolve_used_update_types(),
        drop_pending_updates=True,
    )
    logging.info("Telegram webhook configured: %s", webhook_url)
    return bot, dispatcher


async def apply_mini_app_menu_button(bot: Bot) -> None:
    """Keep the left input-bar menu as slash commands (/start, /catalog, …).

    MenuButtonWebApp replaces that whole command list with a single Shop
    button and also delayed /start (Telegram API call before the welcome).
    Mini App stays on the inline / reply keyboards only.
    """
    from aiogram.types import MenuButtonCommands

    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logging.info("Telegram chat menu restored to bot commands.")
    except Exception:  # noqa: BLE001
        logging.exception("Failed to restore Telegram command menu button")


async def register_bot_commands(bot: Bot) -> None:
    from aiogram.types import BotCommandScopeAllPrivateChats, BotCommandScopeDefault

    from utils.force_join import is_admin_telegram_id, restore_user_bot_commands
    from utils.maintenance import (
        _DEFAULT_BOT_COMMANDS,
        clear_stale_chat_command_scopes,
        is_maintenance_enabled,
    )

    if is_maintenance_enabled():
        await bot.set_my_commands([])
        logging.info("Maintenance mode ON — bot slash commands hidden.")
        return

    cmds = list(_DEFAULT_BOT_COMMANDS)
    await bot.set_my_commands(cmds, scope=BotCommandScopeDefault())
    try:
        await bot.set_my_commands(cmds, scope=BotCommandScopeAllPrivateChats())
    except Exception:  # noqa: BLE001
        logging.exception("Failed to set AllPrivateChats bot commands")
    logging.info("Registered %s Telegram bot commands.", len(cmds))
    # Re-apply Menu for every chat (fixes empty scopes from past maintenance).
    try:
        await clear_stale_chat_command_scopes(bot)
    except Exception:  # noqa: BLE001
        logging.exception("Failed to restore per-chat bot command menus")

    # Guarantee admin chat has /admin + /adminstats even if ADMIN_ID is not in users yet.
    admin_raw = (os.getenv("ADMIN_ID") or os.getenv("ADMIN_TG_ID") or "").strip()
    if admin_raw.lstrip("-").isdigit() and is_admin_telegram_id(admin_raw):
        try:
            await restore_user_bot_commands(bot, int(admin_raw), include_admin=True)
            logging.info("Restored admin slash Menu for chat %s", admin_raw)
        except Exception:  # noqa: BLE001
            logging.exception("Failed to restore admin bot commands for %s", admin_raw)


def run_bot_forever() -> None:
    asyncio.run(start_bot())
