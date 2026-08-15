"""Force-join confirm handler — verifies real Telegram membership."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, ReplyKeyboardRemove

from bot.keyboards import answer_with_start_menu, force_join_keyboard, send_quick_reply_menu
from database.models import BotConfig, SessionLocal
from utils.force_join import (
    check_membership,
    clear_user_bot_commands,
    invalidate_membership_cache,
    is_admin_telegram_id,
    load_force_join_targets,
    membership_required_text,
    restore_user_bot_commands,
    set_force_join_ok,
)
from utils.helpers import resolve_welcome_msg
from utils.menu_commands import get_command_map

logger = logging.getLogger(__name__)
router = Router()


def _force_join_markup(targets, db) -> object:
    return force_join_keyboard(
        channel_url=targets.channel_url,
        group_url=targets.group_url,
        db=db,
    )


async def send_force_join_gate(message, *, edit: bool = False) -> None:
    """Show Membership Required + join buttons in one message (presets + green confirm)."""
    db = SessionLocal()
    try:
        targets = load_force_join_targets(db)
        text = membership_required_text(db)
        markup = _force_join_markup(targets, db)
    finally:
        db.close()

    chat_id = getattr(getattr(message, "chat", None), "id", None)
    bot = getattr(message, "bot", None)
    if bot is not None and chat_id is not None:
        await clear_user_bot_commands(bot, chat_id)

    if edit and getattr(message, "edit_text", None):
        try:
            await message.edit_text(text, reply_markup=markup, parse_mode="HTML")
            return
        except Exception:  # noqa: BLE001
            pass

    # Strip old reply menu without leaving a visible bubble, then one gate message
    # with Membership Required text + Join / Confirm buttons attached underneath.
    try:
        scrub = await message.answer("\u2060", reply_markup=ReplyKeyboardRemove())
        try:
            await scrub.delete()
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "forcejoin:check")
async def force_join_confirm(callback: CallbackQuery) -> None:
    if not callback.from_user:
        await callback.answer()
        return

    bot = callback.bot
    user_id = callback.from_user.id
    invalidate_membership_cache(user_id)

    db = SessionLocal()
    try:
        targets = load_force_join_targets(db)
        text = membership_required_text(db)
        markup = _force_join_markup(targets, db)
    finally:
        db.close()

    if not targets.active:
        await callback.answer("Force join is off.", show_alert=True)
        return

    if not targets.has_any_target:
        await callback.answer(
            "Force join is not configured (set channel and/or group).",
            show_alert=True,
        )
        return

    result = await check_membership(bot, user_id, targets)
    if not result.ok:
        set_force_join_ok(user_id, False)
        missing = ", ".join(result.missing) or "channel/group"
        await callback.answer(
            f"Please join the {missing} first, then tap again.",
            show_alert=True,
        )
        try:
            await callback.message.edit_text(
                text,
                reply_markup=markup,
                parse_mode="HTML",
            )
        except Exception:  # noqa: BLE001
            pass
        return

    # Real membership verified — unlock this user (old + new).
    set_force_join_ok(user_id, True)
    if callback.message and callback.message.chat:
        await restore_user_bot_commands(bot, callback.message.chat.id)

    db = SessionLocal()
    try:
        config = db.query(BotConfig).first()
        welcome = resolve_welcome_msg(config, db)
        commands = get_command_map(db)
    finally:
        db.close()

    await callback.answer("Verified ✅", show_alert=False)
    try:
        await callback.message.edit_text("✅ Membership verified. Welcome!")
    except Exception:  # noqa: BLE001
        pass

    show_admin = is_admin_telegram_id(user_id)
    await answer_with_start_menu(callback.message, welcome, commands, show_admin=show_admin)
    await send_quick_reply_menu(callback.message, commands, show_admin=show_admin)
