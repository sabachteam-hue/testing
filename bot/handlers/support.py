import html
import os
import re

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from database.models import BotConfig, SessionLocal
from utils.helpers import render_icon
from utils.menu_commands import MenuCommandFilter
from utils.ui_icons import build_ui_icons

router = Router()


def _whatsapp_digits(raw: str | None) -> str:
    """Keep digits only so wa.me links work (e.g. +92 300-1234567 → 923001234567)."""
    return re.sub(r"\D+", "", raw or "")


def _whatsapp_link(raw: str | None) -> str | None:
    digits = _whatsapp_digits(raw)
    if not digits:
        return None
    return f"https://wa.me/{digits}"


def build_support_text(config: BotConfig | None, db=None) -> str:
    """Support screen text with premium Icon Presets + settings-driven contacts."""
    # Prefer values set by the admin in /admin/settings. Fall back to the old
    # environment variables so nothing breaks if the admin hasn't filled them in yet.
    username = (
        (config.support_username if config and config.support_username else None)
        or os.getenv("ADMIN_USERNAME", "admin")
    )
    username = username.lstrip("@")
    tg_url = (
        (config.support_url if config and config.support_url else None)
        or f"https://t.me/{username}"
    )
    note = config.support_note if config and config.support_note else None
    whatsapp_raw = config.support_whatsapp if config else None
    whatsapp_href = _whatsapp_link(whatsapp_raw)
    whatsapp_display = _whatsapp_digits(whatsapp_raw) or (whatsapp_raw or "").strip()
    tg_channel = (config.tg_channel_url if config and config.tg_channel_url else None) or ""
    wa_channel = (config.whatsapp_channel_url if config and config.whatsapp_channel_url else None) or ""

    icons = build_ui_icons(db)
    contact_icon = render_icon(icons.get("contact"), "📞", html_mode=True)
    admin_icon = render_icon(icons.get("admin"), "👤", html_mode=True)
    telegram_icon = render_icon(icons.get("telegram"), "✈️", html_mode=True)
    whatsapp_icon = render_icon(icons.get("whatsapp"), "💬", html_mode=True)

    lines = [
        f"{contact_icon} <b>Need help? Contact support…</b>",
        "",
        f"{admin_icon} <b>ADMIN TELEGRAM:</b> <a href=\"{html.escape(tg_url)}\">@{html.escape(username)}</a>",
    ]
    if whatsapp_href:
        lines.append(
            f"{whatsapp_icon} <b>ADMIN WHATSAPP:</b> "
            f"<a href=\"{html.escape(whatsapp_href)}\">{html.escape(whatsapp_display)}</a>"
        )
    else:
        lines.append(f"{whatsapp_icon} <b>ADMIN WHATSAPP:</b> —")

    lines.extend(
        [
            "",
            "<b>Join Channels For Giveaway And Updates</b>",
            f"{telegram_icon} <b>TG CHANNEL:</b> "
            + (
                f"<a href=\"{html.escape(tg_channel)}\">{html.escape(tg_channel)}</a>"
                if tg_channel
                else "—"
            ),
            f"{whatsapp_icon} <b>WHATSAPP CHANNEL:</b> "
            + (
                f"<a href=\"{html.escape(wa_channel)}\">{html.escape(wa_channel)}</a>"
                if wa_channel
                else "—"
            ),
        ]
    )

    if note:
        lines.append("")
        lines.append(html.escape(note))

    return "\n".join(lines)


@router.message(Command("support"))
@router.message(MenuCommandFilter("support"))
async def support_command(message: Message) -> None:
    db = SessionLocal()
    try:
        config = db.query(BotConfig).first()
        text = build_support_text(config, db=db)
    finally:
        db.close()
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
