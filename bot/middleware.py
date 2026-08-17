"""Bot middlewares."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

logger = logging.getLogger(__name__)


class SilenceCheckoutNotifyMiddleware(BaseMiddleware):
    """If the client leaves the PayFast payment screen (any new message/command
    or callback), expire pending PayFast checkouts immediately and silently.

    No Telegram notice is sent. Reopening the old payment link shows session closed.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        telegram_id: str | None = None
        if isinstance(event, Message) and event.from_user:
            telegram_id = str(event.from_user.id)
        elif isinstance(event, CallbackQuery) and event.from_user:
            telegram_id = str(event.from_user.id)

        # I Have Paid / paste-ID is still the payment screen — do not expire
        # the checkout just because the customer is checking status. Expiring
        # here made the later PayFast callback look like a late recovery
        # ("after the checkout expired") and raced the loading check.
        skip_expire = False
        if isinstance(event, CallbackQuery) and (event.data or "").startswith("payfast_check:"):
            skip_expire = True
        else:
            state = data.get("state")
            if state is not None:
                try:
                    current = await state.get_state()
                except Exception:  # noqa: BLE001
                    current = None
                if current == "PayFastReferenceFlow:waiting_reference":
                    skip_expire = True

        if telegram_id and not skip_expire:
            try:
                from utils.checkout_expire import expire_pending_checkouts_silently_for_telegram

                # Never block Pay with Wallet / PayFast open on sync DB expire work.
                cb_data = (event.data or "") if isinstance(event, CallbackQuery) else ""
                if cb_data.startswith("orderpay:"):
                    asyncio.create_task(
                        asyncio.to_thread(expire_pending_checkouts_silently_for_telegram, telegram_id)
                    )
                else:
                    await asyncio.to_thread(expire_pending_checkouts_silently_for_telegram, telegram_id)
            except Exception:  # noqa: BLE001
                logger.exception("[CHECKOUT-EXPIRE] Failed to silent-expire checkout for %s", telegram_id)

        return await handler(event, data)


class MaintenanceMiddleware(BaseMiddleware):
    """When Admin → Settings → Maintenance Mode is on, block all client commands
    with the maintenance notice. Admins can still use the bot."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from aiogram.types import ReplyKeyboardRemove

        from utils.force_join import is_admin_telegram_id
        from utils.maintenance import MAINTENANCE_MESSAGE, is_maintenance_enabled

        user = None
        if isinstance(event, Message) and event.from_user:
            user = event.from_user
        elif isinstance(event, CallbackQuery) and event.from_user:
            user = event.from_user
        else:
            return await handler(event, data)

        if not user or is_admin_telegram_id(user.id):
            return await handler(event, data)

        try:
            enabled = await asyncio.to_thread(is_maintenance_enabled)
        except Exception:  # noqa: BLE001
            logger.exception("[MAINTENANCE] Failed to read maintenance flag")
            enabled = False

        if not enabled:
            return await handler(event, data)

        # Global + AllPrivateChats commands are cleared while maintenance is on.
        # Do NOT set per-chat empty scopes here — those stick after restore and hide
        # Menu for clients (admins never hit this path, so they still saw Menu).
        # Reply Quick keyboard is stripped below; maintenance-off broadcast + /start|/menu
        # restore it for clients.

        if isinstance(event, CallbackQuery):
            try:
                await event.answer("Bot is on maintenance.", show_alert=True)
            except Exception:  # noqa: BLE001
                pass
            if event.message:
                await event.message.answer(
                    MAINTENANCE_MESSAGE,
                    parse_mode="HTML",
                    reply_markup=ReplyKeyboardRemove(),
                )
            return None

        if isinstance(event, Message):
            await event.answer(
                MAINTENANCE_MESSAGE,
                parse_mode="HTML",
                reply_markup=ReplyKeyboardRemove(),
            )
            return None

        return None


class ForceJoinMiddleware(BaseMiddleware):
    """Block bot commands until the user confirms channel membership.

    Allowlisted: /start, force-join confirm callbacks, admin.
    Old users stay locked until they tap ✅ I have joined (DB flag).
    Does not affect outbound product/stock/sale notifications.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from utils.force_join import (
            is_admin_telegram_id,
            load_force_join_targets,
            user_may_use_bot,
        )

        targets = load_force_join_targets()
        if not targets.active:
            return await handler(event, data)

        user = None
        if isinstance(event, Message) and event.from_user:
            user = event.from_user
            text = (event.text or "").strip()
            # Always allow /start so the gate (or welcome) can be shown.
            if text.startswith("/start"):
                return await handler(event, data)
        elif isinstance(event, CallbackQuery) and event.from_user:
            user = event.from_user
            cb = event.data or ""
            if cb.startswith("forcejoin:"):
                return await handler(event, data)
        else:
            return await handler(event, data)

        if not user or is_admin_telegram_id(user.id):
            return await handler(event, data)

        bot = data.get("bot") or getattr(event, "bot", None)
        if bot is None:
            return await handler(event, data)

        try:
            ok = await user_may_use_bot(bot, user.id)
        except Exception:  # noqa: BLE001
            logger.exception("Force-join membership check failed for %s", user.id)
            ok = False

        if ok:
            return await handler(event, data)

        # Block — show / refresh membership gate (strips reply keyboard + slash cmds)
        from bot.handlers.force_join import send_force_join_gate

        if isinstance(event, CallbackQuery):
            await event.answer("Please join the channel first.", show_alert=True)
            if event.message:
                await send_force_join_gate(event.message)
            return None

        if isinstance(event, Message):
            await send_force_join_gate(event)
            return None

        return None
