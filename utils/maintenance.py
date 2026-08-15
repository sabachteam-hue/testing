"""Bot maintenance mode helpers."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BotCommand, ReplyKeyboardRemove

from database.models import BotConfig, SessionLocal

logger = logging.getLogger(__name__)

MAINTENANCE_MESSAGE = (
    "🛠 <b>BOT IS ON MAINTENANCE</b>\n\n"
    "We are updating something good for you.\n"
    "Don't order anything right now.\n"
    "Please wait for the next announcement..."
)

ACTIVE_MESSAGE = (
    "✅ <b>BOT IS ACTIVE NOW</b>\n\n"
    "You can buy products again.\n"
    "Quick menu is ready below 👇"
)

_DEFAULT_BOT_COMMANDS = [
    BotCommand(command="start", description="🚀 Start the bot"),
    BotCommand(command="menu", description="🏠 Open main menu"),
    BotCommand(command="catalog", description="🗂 Browse by category"),
    BotCommand(command="products", description="🛍 Browse products"),
    BotCommand(command="wallet", description="👛 Wallet and deposits"),
    BotCommand(command="api", description="🔗 Reseller API key"),
    BotCommand(command="orders", description="📦 Order history"),
    BotCommand(command="referral", description="💎 Referral link and earnings"),
    BotCommand(command="support", description="💬 Contact support"),
    BotCommand(command="admin", description="🔐 Admin panel link & login"),
    BotCommand(command="adminstats", description="📊 Admin statistics"),
]


def is_maintenance_enabled(db=None) -> bool:
    own = db is None
    session = db or SessionLocal()
    try:
        config = session.query(BotConfig).first()
        return bool(config and config.maintenance)
    finally:
        if own:
            session.close()


def _bot_token() -> str:
    import os

    return (os.getenv("BOT_TOKEN") or "").strip().strip('"').strip("'")


async def hide_global_bot_commands() -> None:
    """Hide slash-command menu for everyone while maintenance is on."""
    from aiogram.types import BotCommandScopeAllPrivateChats, BotCommandScopeDefault

    token = _bot_token()
    if not token:
        logger.warning("[MAINTENANCE] BOT_TOKEN missing — cannot hide commands")
        return
    bot = Bot(token=token)
    try:
        # Clear Default + AllPrivateChats so private chats do not keep an older Menu list.
        for scope in (BotCommandScopeDefault(), BotCommandScopeAllPrivateChats()):
            try:
                await bot.set_my_commands([], scope=scope)
            except Exception:  # noqa: BLE001
                logger.debug("[MAINTENANCE] clear commands failed for %s", type(scope).__name__, exc_info=True)
        logger.info("[MAINTENANCE] Global bot commands cleared")
    except Exception:  # noqa: BLE001
        logger.exception("[MAINTENANCE] Failed to clear global bot commands")
    finally:
        await bot.session.close()


async def clear_stale_chat_command_scopes(bot: Bot | None = None) -> int:
    """Re-apply slash Menu commands for every private chat.

    Past maintenance set empty BotCommandScopeChat lists; deleting them is not
    always enough on Telegram Desktop. We explicitly set the full command list
    per chat (and AllPrivateChats), then re-hide only force-join locked users.
    """
    from aiogram.types import BotCommandScopeAllPrivateChats, BotCommandScopeChat, BotCommandScopeDefault

    from database.models import User
    from utils.force_join import (
        clear_user_bot_commands,
        is_admin_telegram_id,
        load_force_join_targets,
        restore_user_bot_commands,
    )

    owns_bot = bot is None
    token = _bot_token()
    if owns_bot:
        if not token:
            logger.warning("[MAINTENANCE] BOT_TOKEN missing — cannot restore chat command scopes")
            return 0
        bot = Bot(token=token)

    restored = 0
    db = SessionLocal()
    try:
        users = db.query(User).filter(User.is_banned.is_(False)).all()
        force_join_active = load_force_join_targets(db).active

        # Default + all private chats — covers admin and clients.
        try:
            await bot.set_my_commands(_DEFAULT_BOT_COMMANDS, scope=BotCommandScopeDefault())
        except Exception:  # noqa: BLE001
            logger.debug("[MAINTENANCE] set_my_commands Default failed", exc_info=True)
        try:
            await bot.set_my_commands(_DEFAULT_BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats())
        except Exception:  # noqa: BLE001
            logger.debug("[MAINTENANCE] set_my_commands AllPrivateChats failed", exc_info=True)

        for user in users:
            raw = (user.telegram_id or "").strip()
            if not raw.lstrip("-").isdigit():
                continue
            chat_id = int(raw)
            locked = (
                force_join_active
                and not bool(getattr(user, "force_join_ok", False))
                and not is_admin_telegram_id(chat_id)
            )
            for _attempt in range(4):
                try:
                    if locked:
                        await clear_user_bot_commands(bot, chat_id)
                    else:
                        # Drop empty override then set full Menu (admin gets /admin too).
                        try:
                            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=chat_id))
                        except Exception:  # noqa: BLE001
                            pass
                        await restore_user_bot_commands(bot, chat_id)
                        restored += 1
                    break
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(float(getattr(exc, "retry_after", 1) or 1) + 0.5)
                except Exception:  # noqa: BLE001
                    logger.debug("[MAINTENANCE] restore Menu failed for %s", chat_id)
                    break
            await asyncio.sleep(0.03)
    finally:
        db.close()
        if owns_bot and bot is not None:
            await bot.session.close()

    logger.info("[MAINTENANCE] Restored slash Menu for %s chats", restored)
    return restored


async def restore_global_bot_commands() -> None:
    token = _bot_token()
    if not token:
        logger.warning("[MAINTENANCE] BOT_TOKEN missing — cannot restore commands")
        return
    bot = Bot(token=token)
    try:
        await bot.set_my_commands(_DEFAULT_BOT_COMMANDS)
        logger.info("[MAINTENANCE] Global bot commands restored")
        await clear_stale_chat_command_scopes(bot)
    except Exception:  # noqa: BLE001
        logger.exception("[MAINTENANCE] Failed to restore global bot commands")
    finally:
        await bot.session.close()


async def broadcast_maintenance_notice() -> int:
    """DM every user the maintenance message and strip reply keyboards."""
    from utils.notifications import (
        broadcast_to_all_users,
        post_to_notify_channel,
        post_to_notify_group,
    )

    sent = await broadcast_to_all_users(
        MAINTENANCE_MESSAGE,
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    await post_to_notify_group(MAINTENANCE_MESSAGE, parse_mode="HTML")
    await post_to_notify_channel(MAINTENANCE_MESSAGE, parse_mode="HTML")
    logger.info("[MAINTENANCE] Broadcast complete sent=%s", sent)
    return sent


async def broadcast_active_notice() -> int:
    """DM every user that the bot is back online and restore the reply Quick menu."""
    from bot.keyboards import main_menu_keyboard
    from utils.menu_commands import get_command_map
    from utils.notifications import (
        broadcast_to_all_users,
        post_to_notify_channel,
        post_to_notify_group,
    )

    # Same labels as /start Quick menu (Admin → Commands reply names).
    commands = get_command_map(None)
    sent = await broadcast_to_all_users(
        ACTIVE_MESSAGE,
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(commands, show_admin=False),
    )
    await post_to_notify_group(ACTIVE_MESSAGE, parse_mode="HTML")
    await post_to_notify_channel(ACTIVE_MESSAGE, parse_mode="HTML")
    logger.info("[MAINTENANCE] Active broadcast complete sent=%s", sent)
    return sent


async def on_maintenance_enabled() -> int:
    """Side effects when admin turns Maintenance Mode on."""
    await hide_global_bot_commands()
    return await broadcast_maintenance_notice()


async def on_maintenance_disabled() -> int:
    """Side effects when admin turns Maintenance Mode off."""
    await restore_global_bot_commands()
    return await broadcast_active_notice()
