"""Fetch & cache Telegram custom emoji sticker images for the admin UI."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from aiogram import Bot

logger = logging.getLogger(__name__)

CUSTOM_EMOJI_DIR = Path("admin/static/uploads/custom_emoji")
CUSTOM_EMOJI_DIR.mkdir(parents=True, exist_ok=True)

_CACHE_EXTS = (".webp", ".png", ".gif", ".jpg", ".jpeg", ".webm")


def _bot_token() -> str:
    return (os.getenv("BOT_TOKEN") or "").strip().strip('"').strip("'")


def cached_custom_emoji_path(emoji_id: str) -> Path | None:
    eid = (emoji_id or "").strip()
    if not eid.isdigit():
        return None
    for ext in _CACHE_EXTS:
        path = CUSTOM_EMOJI_DIR / f"{eid}{ext}"
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def _guess_ext(file_path: str | None, preferred: str = ".webp") -> str:
    raw = (file_path or "").rsplit(".", 1)
    if len(raw) == 2 and raw[1]:
        ext = f".{raw[1].lower()}"
        if ext in _CACHE_EXTS or ext == ".tgs":
            return ext if ext != ".tgs" else preferred
    return preferred


async def ensure_custom_emoji_cached(emoji_id: str) -> Path | None:
    """Download a custom emoji (or its thumbnail) once; return local path."""
    eid = (emoji_id or "").strip()
    if not eid.isdigit():
        return None

    existing = cached_custom_emoji_path(eid)
    if existing:
        return existing

    token = _bot_token()
    if not token:
        logger.warning("custom emoji cache skipped: BOT_TOKEN missing")
        return None

    bot = Bot(token=token)
    try:
        stickers = await bot.get_custom_emoji_stickers(custom_emoji_ids=[eid])
        if not stickers:
            return None
        sticker = stickers[0]
        # Prefer static thumbnail so the admin panel can show a real image
        # (animated .tgs / video stickers are awkward in a plain <img>).
        thumb = getattr(sticker, "thumbnail", None)
        file_id = getattr(thumb, "file_id", None) if thumb else None
        if not file_id:
            file_id = getattr(sticker, "file_id", None)
        if not file_id:
            return None

        # Probe extension via get_file; if main file is .tgs/.webm try thumbnail.
        tg_file = await bot.get_file(file_id)
        remote_path = getattr(tg_file, "file_path", None) or ""
        lower = remote_path.lower()
        if (lower.endswith(".tgs") or lower.endswith(".webm")) and thumb and getattr(thumb, "file_id", None):
            file_id = thumb.file_id
            tg_file = await bot.get_file(file_id)
            remote_path = getattr(tg_file, "file_path", None) or ""

        ext = _guess_ext(remote_path)
        if ext == ".tgs":
            # Still no raster preview available.
            return None

        dest = CUSTOM_EMOJI_DIR / f"{eid}{ext}"
        if dest.exists() and dest.stat().st_size > 0:
            return dest

        await bot.download(file_id, destination=dest)
        if dest.is_file() and dest.stat().st_size > 0:
            return dest
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to cache custom emoji %s: %s", eid, exc)
        return None
    finally:
        await bot.session.close()
