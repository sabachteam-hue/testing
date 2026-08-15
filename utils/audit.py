"""Admin audit / activity log helpers."""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from database.models import AuditLog


def client_address(request: Request | None) -> str | None:
    if request is None:
        return None
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded[:120]
    if request.client and request.client.host:
        return str(request.client.host)[:120]
    return None


def log_admin_action(
    db: Session,
    *,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    entity_label: str | None = None,
    administrator: str = "admin",
    address: str | None = None,
    change: dict[str, Any] | str | None = None,
    request: Request | None = None,
) -> AuditLog:
    """Persist one admin-panel mutation for Dashboard activity + Audit log page."""
    change_text: str | None = None
    if isinstance(change, dict):
        try:
            change_text = json.dumps(change, default=str)[:4000]
        except Exception:  # noqa: BLE001
            change_text = str(change)[:4000]
    elif change is not None:
        change_text = str(change)[:4000]

    row = AuditLog(
        action=(action or "unknown")[:120],
        entity_type=(entity_type or None) and str(entity_type)[:80],
        entity_id=(entity_id or None) and str(entity_id)[:80],
        entity_label=(entity_label or None) and str(entity_label)[:200],
        administrator=(administrator or "admin")[:120],
        address=address or client_address(request),
        change_json=change_text,
    )
    db.add(row)
    return row


def entity_display(row: AuditLog) -> str:
    if row.entity_label:
        return row.entity_label
    parts = []
    if row.entity_type:
        parts.append(row.entity_type)
    if row.entity_id:
        parts.append(f"#{row.entity_id}")
    return " ".join(parts) if parts else "—"
