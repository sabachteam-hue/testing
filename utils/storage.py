"""Centralized persistent storage and file path abstraction for Railway and local development.

When RAILWAY_VOLUME_MOUNT_PATH is configured (e.g. /app/data or /data), uploaded
files are saved to the persistent volume mount so they survive code pushes,
container replacements, and restarts.

Directory layout on volume:
  $RAILWAY_VOLUME_MOUNT_PATH/
    uploads/
      services/
      categories/
      payment_methods/
      announcements/
      custom_emoji/
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_LOCAL_STORAGE = Path("data")


def get_storage_root() -> Path:
    """Return the base persistent storage directory.
    Prefers RAILWAY_VOLUME_MOUNT_PATH when present and valid.
    Falls back to a safe local directory for development.
    """
    volume_path = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if volume_path:
        root = Path(volume_path)
        try:
            root.mkdir(parents=True, exist_ok=True)
            return root
        except Exception as exc:
            logger.warning(
                "Could not initialize RAILWAY_VOLUME_MOUNT_PATH '%s' (%s). Falling back to local storage.",
                volume_path,
                exc,
            )

    # Local development fallback
    _DEFAULT_LOCAL_STORAGE.mkdir(parents=True, exist_ok=True)
    return _DEFAULT_LOCAL_STORAGE


def is_persistent_volume_configured() -> bool:
    """Check if a persistent Railway volume is actively configured and accessible."""
    volume_path = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if not volume_path:
        return False
    p = Path(volume_path)
    return p.exists() and p.is_dir()


def get_upload_dir(category: str) -> Path:
    """Return and ensure the specific persistent upload directory exists.
    Categories: 'services', 'categories', 'payment_methods', 'announcements', 'custom_emoji'.
    """
    clean_cat = category.strip().replace("..", "").replace("/", "").replace("\\", "") or "misc"
    target = get_storage_root() / "uploads" / clean_cat
    target.mkdir(parents=True, exist_ok=True)
    return target


def init_storage() -> None:
    """Initialize all persistent storage subdirectories on startup."""
    categories = ["services", "categories", "payment_methods", "announcements", "custom_emoji", "claims"]
    for cat in categories:
        get_upload_dir(cat)
    if is_persistent_volume_configured():
        logger.info(
            "Persistent Railway volume active at: %s",
            os.getenv("RAILWAY_VOLUME_MOUNT_PATH"),
        )
    else:
        logger.info(
            "Using local storage at: %s (Set RAILWAY_VOLUME_MOUNT_PATH on Railway for persistent uploads)",
            get_storage_root(),
        )


def resolve_file_path(web_or_relative_path: str | None) -> Path | None:
    """Resolve a web-relative path or disk path to the actual existing file on disk.
    Searches persistent volume first, then local repository directories.
    """
    if not web_or_relative_path:
        return None
    raw = str(web_or_relative_path).strip().lstrip("/")

    # 1. Direct path check
    direct = Path(raw)
    if direct.is_file():
        return direct

    # 2. Check if it's an uploaded asset under admin/static/uploads/<cat>/<file>
    parts = Path(raw).parts
    if len(parts) >= 4 and parts[0] == "admin" and parts[1] == "static" and parts[2] == "uploads":
        category = parts[3]
        filename = parts[-1]
        vol_file = get_upload_dir(category) / filename
        if vol_file.is_file():
            return vol_file
        repo_file = Path("admin/static/uploads") / category / filename
        if repo_file.is_file():
            return repo_file

    # 3. Check static/uploads/<cat>/<file>
    if len(parts) >= 3 and parts[0] == "static" and parts[1] == "uploads":
        category = parts[2]
        filename = parts[-1]
        vol_file = get_upload_dir(category) / filename
        if vol_file.is_file():
            return vol_file
        repo_file = Path("static/uploads") / category / filename
        if repo_file.is_file():
            return repo_file

    return None

