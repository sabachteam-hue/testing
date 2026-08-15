"""Editable bot menu commands (Admin → Commands).

Each command has a stable `key`, a display `name` (inline buttons), optional
`reply_name` (bottom reply keyboard), and an `icon` (plain emoji or ID|fallback
premium custom emoji).
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram.filters import BaseFilter
from aiogram.types import Message
from sqlalchemy.orm import Session

from database.models import IconPreset, MenuCommand, SessionLocal

# key, inline name, reply name (None = use inline name), default emoji, sort
COMMAND_DEFAULTS: list[tuple[str, str, str | None, str, int]] = [
    ("shop", "Shop", "Shop All", "🛍", 1),
    ("catalog", "Catalog", "Catalog", "🗂", 2),
    ("topup", "Top-up Wallet", None, "💰", 3),
    ("wallet", "Wallet", "Wallet", "👛", 4),
    ("settings", "Settings", None, "🔑", 5),
    ("profile", "Profile", None, "👤", 6),
    ("support", "Support", "Support", "💬", 7),
    ("orders", "History", "Order History", "📦", 8),
    ("refer", "Refer", None, "⭐", 9),
    ("api", "API Link", "Api", "🔗", 10),
    ("language", "Language", None, "🌐", 11),
    ("refresh", "Refresh", None, "🔄", 12),
    ("back", "Back", None, "◀️", 13),
]

DEFAULT_BY_KEY = {row[0]: row for row in COMMAND_DEFAULTS}


@dataclass(frozen=True)
class CommandView:
    key: str
    name: str
    reply_name: str
    icon: str
    sort_order: int
    is_active: bool = True


def _icon_from_preset(presets: dict[str, str], key: str, fallback: str) -> str:
    return presets.get(key) or presets.get(key.title()) or presets.get(key.upper()) or fallback


def ensure_menu_commands(db: Session) -> None:
    """Insert any missing command keys. Does not overwrite admin edits."""
    presets = {
        row.name.strip().lower(): row.combined_value
        for row in db.query(IconPreset).all()
        if row.name
    }
    existing = {row.key: row for row in db.query(MenuCommand).all()}
    changed = False
    for key, name, reply_name, emoji, sort_order in COMMAND_DEFAULTS:
        if key in existing:
            row = existing[key]
            # One-time soft rename for start-menu label.
            if key == "orders" and (row.name or "").strip() == "Orders":
                row.name = "History"
                changed = True
            continue
        icon = _icon_from_preset(presets, key, emoji)
        db.add(
            MenuCommand(
                key=key,
                name=name,
                reply_name=reply_name,
                icon=icon,
                sort_order=sort_order,
                is_active=True,
            )
        )
        changed = True
    if changed:
        db.flush()


def get_command_map(db: Session | None = None) -> dict[str, CommandView]:
    """All commands by key (defaults when DB unavailable / empty)."""
    result: dict[str, CommandView] = {}
    for key, name, reply_name, emoji, sort_order in COMMAND_DEFAULTS:
        result[key] = CommandView(
            key=key,
            name=name,
            reply_name=reply_name or name,
            icon=emoji,
            sort_order=sort_order,
        )
    if db is None:
        return result

    ensure_menu_commands(db)
    for row in db.query(MenuCommand).all():
        # Soft rename default Orders → History once for start-menu label.
        if row.key == "orders" and (row.name or "").strip() == "Orders":
            row.name = "History"
        result[row.key] = CommandView(
            key=row.key,
            name=row.name,
            reply_name=row.reply_label,
            icon=row.icon or result.get(row.key, CommandView(row.key, row.key, row.key, "✨", 0)).icon,
            sort_order=row.sort_order,
            is_active=bool(row.is_active),
        )
    return result


def menu_icons(db: Session | None = None) -> dict[str, str]:
    """Back-compat: key → icon value (ID|fallback or plain emoji)."""
    return {key: cmd.icon for key, cmd in get_command_map(db).items()}


def menu_names(db: Session | None = None) -> dict[str, str]:
    """key → inline button label."""
    return {key: cmd.name for key, cmd in get_command_map(db).items()}


def menu_reply_names(db: Session | None = None) -> dict[str, str]:
    """key → reply keyboard label (without emoji prefix)."""
    return {key: cmd.reply_name for key, cmd in get_command_map(db).items()}


def reply_match_suffixes(db: Session | None = None) -> list[str]:
    """Display names used to detect reply-keyboard presses (longest first)."""
    names = {cmd.reply_name for cmd in get_command_map(db).values() if cmd.reply_name}
    names.update(cmd.name for cmd in get_command_map(db).values() if cmd.name)
    names.add("Menu")
    return sorted(names, key=len, reverse=True)


def text_matches_command(text: str | None, key: str, db: Session | None = None) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    commands = get_command_map(db)
    best_key: str | None = None
    best_len = -1
    for cmd_key, cmd in commands.items():
        for label in {cmd.reply_name, cmd.name}:
            if not label:
                continue
            if text == label or text.endswith(label) or text.endswith(f" {label}"):
                if len(label) > best_len:
                    best_len = len(label)
                    best_key = cmd_key
    return best_key == key


def text_matches_any_menu(text: str | None, db: Session | None = None) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    if text.startswith("/"):
        return True
    return any(text.endswith(suffix) for suffix in reply_match_suffixes(db))


class MenuCommandFilter(BaseFilter):
    """Match reply-keyboard presses for a given command key (uses live DB labels)."""

    def __init__(self, key: str) -> None:
        self.key = key

    async def __call__(self, message: Message) -> bool:
        db = SessionLocal()
        try:
            return text_matches_command(message.text, self.key, db)
        finally:
            db.close()
