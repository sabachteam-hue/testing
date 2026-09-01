"""Private API used by the optional Windows Canva local worker.

The worker never receives Canva credentials or the Railway database URL. It only
claims one paid Canva order at a time and reports the result using a shared
secret token kept in Railway Variables and the local worker environment.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from database.models import Order, Service, SessionLocal
from utils.notifications import notify_channel_order_completed, notify_user_order_completed, send_admin_message
from utils.stock_manager import complete_reserved_stock

router = APIRouter(prefix="/internal/canva-worker", tags=["Canva local worker"])


class WorkerResult(BaseModel):
    order_code: str = Field(min_length=1, max_length=40)
    success: bool
    detail: str = Field(default="", max_length=1000)


def _configured_token() -> str:
    return (os.getenv("CANVA_REMOTE_WORKER_TOKEN") or "").strip()


def _require_worker_token(x_canva_worker_token: str | None) -> None:
    expected = _configured_token()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Canva local worker token is not configured",
        )
    supplied = (x_canva_worker_token or "").strip()
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid worker token")


def _local_mode_enabled() -> bool:
    return (
        os.getenv("CANVA_AUTOMATION_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
        and (os.getenv("CANVA_AUTOMATION_MODE", "railway").strip().lower() == "local")
    )


@router.post("/claim")
def claim_order(x_canva_worker_token: str | None = Header(default=None)):
    """Atomically claim the oldest paid Canva order for the Windows worker."""
    _require_worker_token(x_canva_worker_token)
    if not _local_mode_enabled():
        raise HTTPException(status_code=409, detail="CANVA_AUTOMATION_MODE is not local")

    db = SessionLocal()
    try:
        stale_before = datetime.utcnow() - timedelta(minutes=10)
        # A crashed local worker may leave an order in processing. Make it
        # claimable again only after a conservative timeout.
        stale = (
            db.query(Order)
            .join(Service, Order.service_id == Service.id)
            .filter(
                Service.fulfillment_type == "canva",
                Order.status == "canva_local_processing",
                Order.updated_at < stale_before,
            )
            .all()
        )
        for row in stale:
            row.status = "canva_local_retry"
            row.note = "Canva local worker claim expired; safely queued for retry."
        if stale:
            db.commit()

        query = (
            db.query(Order)
            .join(Service, Order.service_id == Service.id)
            .filter(
                Service.fulfillment_type == "canva",
                Order.status.in_(["manual_pending", "canva_retry", "canva_local_retry"]),
                Order.customer_email.isnot(None),
            )
            .order_by(Order.created_at.asc())
        )
        try:
            query = query.with_for_update(skip_locked=True)
        except Exception:
            pass
        order = query.first()
        if not order:
            return {"ok": True, "job": None}

        email = (order.customer_email or "").strip().lower()
        if not email:
            order.status = "manual_pending"
            order.note = "Canva local worker skipped: customer email missing."
            db.commit()
            return {"ok": True, "job": None}

        order.status = "canva_local_processing"
        order.note = "Claimed by Canva Windows local worker."
        db.commit()

        return {
            "ok": True,
            "job": {
                "order_code": order.order_code,
                "email": email,
                "team_url": (os.getenv("CANVA_TEAM_URL") or "https://www.canva.com/").strip(),
            },
        }
    finally:
        db.close()


@router.post("/result")
async def report_result(payload: WorkerResult, x_canva_worker_token: str | None = Header(default=None)):
    """Finalize or safely requeue a job after the Windows worker runs Canva UI."""
    _require_worker_token(x_canva_worker_token)
    if not _local_mode_enabled():
        raise HTTPException(status_code=409, detail="CANVA_AUTOMATION_MODE is not local")

    db = SessionLocal()
    try:
        order = (
            db.query(Order)
            .join(Service, Order.service_id == Service.id)
            .filter(Order.order_code == payload.order_code, Service.fulfillment_type == "canva")
            .first()
        )
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        email = (order.customer_email or "").strip().lower()
        detail = (payload.detail or "").strip()

        # Idempotency: if a network retry reports the same successful job twice,
        # do not consume stock or notify the customer a second time.
        if order.status == "completed":
            return {"ok": True, "status": "completed", "already_finalized": True}

        if payload.success:
            if order.status != "canva_local_processing":
                raise HTTPException(status_code=409, detail=f"Order is not claimed by local worker: {order.status}")
            service = order.service
            order.status = "completed"
            order.completed_at = datetime.utcnow()
            order.delivered_info = f"Canva Education invitation sent to: {email}"
            order.note = "Canva Education email invitation sent automatically by Windows worker."
            complete_reserved_stock(db, order.service_id, order.quantity)
            db.commit()

            # Relationships are needed by notification helpers.
            _ = order.user.telegram_id
            await notify_user_order_completed(order, service)
            await notify_channel_order_completed(order, service, db)
            return {"ok": True, "status": "completed"}

        lowered = detail.lower()
        auth_problem = any(
            marker in lowered
            for marker in (
                "auth_required",
                "login required",
                "session expired",
                "security verification",
                "captcha",
                "just a moment",
                "browser not safe",
            )
        )
        order.status = "canva_auth_required" if auth_problem else "canva_local_retry"
        order.note = f"Canva local worker: {detail or 'invite failed'}"
        db.commit()

        if auth_problem:
            await send_admin_message(
                f"⚠️ Canva local worker needs attention\n"
                f"Order: {order.order_code}\nEmail: {email}\n{detail}",
                db=db,
            )
        return {"ok": True, "status": order.status}
    finally:
        db.close()
