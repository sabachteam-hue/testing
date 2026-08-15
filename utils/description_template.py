"""Build paste-ready product description HTML with Icon Preset premium emojis."""
from __future__ import annotations

import html
import re
from typing import Iterable

from database.models import IconPreset


def _preset_map(presets: Iterable[IconPreset]) -> dict[str, IconPreset]:
    return {(row.name or "").strip().lower(): row for row in presets if row.name}


def _tag(presets: dict[str, IconPreset], names: tuple[str, ...], fallback: str) -> str:
    for name in names:
        row = presets.get(name.lower())
        if row:
            return row.tg_tag
    return fallback


def format_product_name_line(service) -> str:
    """Product icon (Service.emoji) + name for the description Product Name line."""
    from utils.helpers import parse_icon, strip_html_tags

    raw_name = (getattr(service, "name", None) or "Product").strip() or "Product"
    if "<tg-emoji" in raw_name:
        return raw_name

    plain = strip_html_tags(raw_name).strip() or "Product"
    emoji_value = getattr(service, "emoji", None)
    if not emoji_value:
        return plain

    emoji_id, display = parse_icon(emoji_value, "✨")
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{html.escape(display)}</tg-emoji> {plain}'
    if display:
        return f"{display} {plain}"
    return plain


def guess_duration_from_name(name: str | None) -> str:
    """Best-effort duration from product name (e.g. CapCut 1M → 1M)."""
    text = (name or "").strip()
    if not text:
        return ""
    match = re.search(r"(?i)(?<![a-z0-9])(\d+\s*[dmy]|lifetime)\b", text)
    return match.group(1).replace(" ", "") if match else ""


def build_product_description_template(
    presets: Iterable[IconPreset],
    *,
    product_name: str = "CapCut 1M",
    duration: str = "1M",
    warranty: str = "25-30 Days",
    delivery_format: str = "Email | Password",
    product_info: str = "",
) -> tuple[str, list[str]]:
    """Return (template_html, missing_preset_names)."""
    by_name = _preset_map(presets)
    missing: list[str] = []

    def require(label: str, names: tuple[str, ...], fallback: str) -> str:
        tag = _tag(by_name, names, "")
        if tag:
            return tag
        missing.append(label)
        return fallback

    # Do not prepend "Description:" — product card already shows that label.
    duration_icon = require("Duration", ("duration",), "⏱")
    warranty_icon = require("Warranty", ("warranty",), "🛡")
    delivery_icon = require(
        "Delivery Format",
        ("delivery format", "deliveryformat", "delivery_format", "format"),
        "📩",
    )
    info_icon = require(
        "Product Info",
        ("product info", "productinfo", "product information", "product_info"),
        "✂️",
    )

    info_body = (product_info or "").strip()
    template = (
        f"Product Name: {product_name}\n"
        f"{duration_icon} Duration: {(duration or '').strip() or '—'}\n"
        f"{warranty_icon} Warranty: {(warranty or '').strip() or '—'}\n"
        f"{delivery_icon} Delivery Format: {(delivery_format or '').strip() or '—'}\n"
        f"{info_icon} Product Information:"
        + (f" {info_body}" if info_body else "")
    )
    return template, missing


def description_line_icons(presets: Iterable[IconPreset]) -> dict[str, str]:
    """Icon prefixes used at the start of each description line."""
    by_name = _preset_map(presets)
    return {
        "description": _tag(by_name, ("description",), "📝"),
        "duration": _tag(by_name, ("duration",), "⏱"),
        "warranty": _tag(by_name, ("warranty",), "🛡"),
        "delivery": _tag(
            by_name,
            ("delivery format", "deliveryformat", "delivery_format", "format"),
            "📩",
        ),
        "info": _tag(
            by_name,
            ("product info", "productinfo", "product information", "product_info"),
            "✂️",
        ),
    }
