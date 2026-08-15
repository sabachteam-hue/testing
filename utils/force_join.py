"""Force-join (channel + group) membership gate for the Telegram bot.

Product / stock / sale broadcasts are outbound DMs and are not affected.
Only user-initiated bot commands/callbacks are blocked until the user taps
✅ I have joined AND Telegram getChatMember confirms membership for every
configured target (channel and/or group).

Old users are locked too: User.force_join_ok defaults to False until confirm.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import BotCommand, BotCommandScopeChat

from database.models import BotConfig, SessionLocal, User

logger = logging.getLogger(__name__)

_MEMBER_OK = frozenset({"creator", "administrator", "member", "restricted"})
# Short cache for membership API after confirm — not a substitute for force_join_ok.
_CACHE_TTL_SEC = 120
_ok_cache: dict[str, float] = {}  # telegram_id -> expires_at

_TME_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_+]+)", re.I)

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
]

_ADMIN_BOT_COMMANDS = [
    BotCommand(command="admin", description="🔐 Admin panel link & login"),
    BotCommand(command="adminstats", description="📊 Admin statistics"),
]


@dataclass(frozen=True)
class ForceJoinTargets:
    enabled: bool
    channel_chat: str | None
    channel_url: str | None
    group_chat: str | None
    group_url: str | None

    @property
    def has_any_target(self) -> bool:
        return bool(self.channel_chat or self.group_chat)

    @property
    def active(self) -> bool:
        return self.enabled and self.has_any_target


def _normalize_chat_ref(value: str | None) -> str | None:
    """@username, t.me link, or numeric -100… chat id for getChatMember."""
    raw = (value or "").strip()
    if not raw:
        return None
    match = _TME_RE.search(raw)
    if match:
        slug = match.group(1)
        if slug.startswith("+") or slug.lower().startswith("joinchat"):
            # Private invite link cannot be used as chat_id — admin must set numeric ID.
            return None
        return f"@{slug}"
    if raw.startswith("@"):
        return raw
    if re.fullmatch(r"-?\d+", raw):
        return raw
    if re.fullmatch(r"[A-Za-z0-9_]{4,}", raw):
        return f"@{raw}"
    return None


def _public_join_url(chat_ref: str | None, explicit_url: str | None) -> str | None:
    url = (explicit_url or "").strip()
    if url:
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if url.startswith("t.me/") or url.startswith("telegram.me/"):
            return f"https://{url}"
        if url.startswith("@"):
            return f"https://t.me/{url[1:]}"
        return url
    if chat_ref and chat_ref.startswith("@"):
        return f"https://t.me/{chat_ref[1:]}"
    return None


def load_force_join_targets(db=None) -> ForceJoinTargets:
    own = db is None
    db = db or SessionLocal()
    try:
        config = db.query(BotConfig).first()
        if not config:
            return ForceJoinTargets(False, None, None, None, None)
        channel_chat = _normalize_chat_ref(getattr(config, "force_join_channel", None))
        channel_url = _public_join_url(channel_chat, getattr(config, "force_join_channel_url", None))
        if not channel_chat and channel_url:
            channel_chat = _normalize_chat_ref(channel_url)
        group_chat = _normalize_chat_ref(getattr(config, "force_join_group", None))
        group_url = _public_join_url(group_chat, getattr(config, "force_join_group_url", None))
        if not group_chat and group_url:
            group_chat = _normalize_chat_ref(group_url)
        enabled = bool(getattr(config, "force_join_enabled", False))
        return ForceJoinTargets(
            enabled=enabled,
            channel_chat=channel_chat,
            channel_url=channel_url,
            group_chat=group_chat,
            group_url=group_url,
        )
    finally:
        if own:
            db.close()


def membership_required_text(db=None) -> str:
    """Membership Required header — uses Icon Preset `Member` when available."""
    from utils.ui_icons import label_icons

    icons = label_icons(db)
    member_icon = icons["member"]
    tick_icon = icons["tick"]
    targets = load_force_join_targets(db)
    parts: list[str] = []
    if targets.channel_chat:
        parts.append("channel")
    if targets.group_chat:
        parts.append("group")
    where = " and ".join(parts) if parts else "channel/group"
    return (
        "➖➖➖➖➖➖➖➖➖➖\n"
        f"{member_icon} <b>Membership Required</b>\n"
        "➖➖➖➖➖➖➖➖➖➖\n\n"
        f"To use this bot, please join our {where} first, "
        f"then tap {tick_icon} <b>I have joined</b>."
    )


def is_admin_telegram_id(telegram_id: str | int | None) -> bool:
    if telegram_id is None:
        return False
    admin = (os.getenv("ADMIN_ID") or os.getenv("ADMIN_TG_ID") or "").strip()
    return bool(admin) and str(telegram_id).strip() == admin


def invalidate_membership_cache(telegram_id: str | int) -> None:
    _ok_cache.pop(str(telegram_id), None)


def get_force_join_ok(telegram_id: str | int) -> bool:
    """Whether this user has already confirmed force-join (DB flag)."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == str(telegram_id)).first()
        return bool(user and getattr(user, "force_join_ok", False))
    finally:
        db.close()


def set_force_join_ok(telegram_id: str | int, value: bool) -> None:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == str(telegram_id)).first()
        if not user:
            return
        user.force_join_ok = bool(value)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("Failed to set force_join_ok=%s for %s", value, telegram_id)
    finally:
        db.close()
    if not value:
        invalidate_membership_cache(telegram_id)


async def _chat_member_ok(bot: Bot, chat_id: str, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        status = getattr(member, "status", None)
        if hasattr(status, "value"):
            status = status.value
        return str(status or "").lower() in _MEMBER_OK
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        logger.warning("getChatMember failed for chat=%s user=%s: %s", chat_id, user_id, exc)
        return False
    except TelegramAPIError as exc:
        logger.warning("getChatMember API error chat=%s user=%s: %s", chat_id, user_id, exc)
        return False


@dataclass
class MembershipCheckResult:
    ok: bool
    channel_ok: bool | None  # None = not required
    group_ok: bool | None  # None = not required
    missing: list[str]


async def check_membership(bot: Bot, user_id: int, targets: ForceJoinTargets | None = None) -> MembershipCheckResult:
    targets = targets or load_force_join_targets()
    if not targets.active:
        return MembershipCheckResult(True, None, None, [])

    channel_ok: bool | None = None
    group_ok: bool | None = None
    missing: list[str] = []

    if targets.channel_chat:
        channel_ok = await _chat_member_ok(bot, targets.channel_chat, user_id)
        if not channel_ok:
            missing.append("channel")

    if targets.group_chat:
        group_ok = await _chat_member_ok(bot, targets.group_chat, user_id)
        if not group_ok:
            missing.append("group")

    ok = not missing
    if ok:
        _ok_cache[str(user_id)] = time.time() + _CACHE_TTL_SEC
    else:
        invalidate_membership_cache(user_id)
    return MembershipCheckResult(ok, channel_ok, group_ok, missing)


async def user_may_use_bot(bot: Bot, user_id: int) -> bool:
    """True if force-join is off, user is admin, or user confirmed + still a member."""
    targets = load_force_join_targets()
    if not targets.active:
        return True
    if is_admin_telegram_id(user_id):
        return True

    if not get_force_join_ok(user_id):
        return False

    key = str(user_id)
    expires = _ok_cache.get(key)
    if expires and expires > time.time():
        return True

    result = await check_membership(bot, user_id, targets)
    if not result.ok:
        set_force_join_ok(user_id, False)
        return False
    return True


async def clear_user_bot_commands(bot: Bot, chat_id: int) -> None:
    """Hide /menu /catalog etc. in this private chat until membership is confirmed."""
    try:
        await bot.set_my_commands([], scope=BotCommandScopeChat(chat_id=chat_id))
    except Exception as exc:  # noqa: BLE001
        logger.debug("clear_user_bot_commands failed for %s: %s", chat_id, exc)


async def restore_user_bot_commands(bot: Bot, chat_id: int, *, include_admin: bool | None = None) -> None:
    """Restore slash Menu for one private chat.

    include_admin=None → auto-detect from ADMIN_ID. Admins get /admin + /adminstats.
    """
    if include_admin is None:
        include_admin = is_admin_telegram_id(chat_id)
    commands = list(_DEFAULT_BOT_COMMANDS)
    if include_admin:
        commands.extend(_ADMIN_BOT_COMMANDS)
    try:
        await bot.set_my_commands(
            commands,
            scope=BotCommandScopeChat(chat_id=chat_id),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("restore_user_bot_commands failed for %s: %s", chat_id, exc)
        try:
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=chat_id))
        except Exception:  # noqa: BLE001
            pass


# Back-compat alias used by older call sites
async def user_has_membership(bot: Bot, user_id: int, *, use_cache: bool = True) -> bool:
    return await user_may_use_bot(bot, user_id)
