"""Canva Education email-invite automation.

Uses a pre-authenticated Playwright storage state. It never stores a Canva
password and never attempts to bypass CAPTCHA/2FA/security challenges.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from database.models import Order, Service, SessionLocal
from utils.notifications import notify_channel_order_completed, notify_user_order_completed, send_admin_message
from utils.stock_manager import complete_reserved_stock

logger = logging.getLogger(__name__)
_LOCK = asyncio.Lock()


def enabled() -> bool:
    return os.getenv("CANVA_AUTOMATION_ENABLED", "false").lower() in {"1", "true", "yes", "on"}


def railway_worker_enabled() -> bool:
    """Run Playwright on Railway only in legacy/default railway mode.

    In local mode, canva_local_worker.py on Windows claims paid orders through
    the private worker API instead.
    """
    return enabled() and (os.getenv("CANVA_AUTOMATION_MODE", "railway").strip().lower() != "local")


def _session_path() -> Path:
    return Path(os.getenv("CANVA_SESSION_PATH", "/data/canva/session.json"))


def _bootstrap_session() -> Path:
    path = _session_path()
    if path.exists():
        return path
    encoded = (os.getenv("CANVA_STORAGE_STATE_B64") or "").strip()
    if encoded:
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = base64.b64decode(encoded)
        json.loads(raw.decode("utf-8"))  # validate before writing
        path.write_bytes(raw)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return path


async def _first_visible(page, selectors, timeout=3500):
    for kind, value in selectors:
        try:
            if kind == "role":
                loc = page.get_by_role(value[0], name=value[1], exact=False).first
            elif kind == "label":
                loc = page.get_by_label(value, exact=False).first
            elif kind == "placeholder":
                loc = page.get_by_placeholder(value, exact=False).first
            else:
                loc = page.get_by_text(value, exact=False).first
            await loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:
            continue
    return None


async def send_invite(email: str) -> tuple[bool, str]:
    """Send one Student invitation. Returns (success, detail/status)."""
    from playwright.async_api import async_playwright

    team_url = (os.getenv("CANVA_TEAM_URL") or "").strip()
    if not team_url:
        return False, "CONFIG_ERROR: CANVA_TEAM_URL is missing"
    state_path = _bootstrap_session()
    if not state_path.exists():
        return False, "AUTH_REQUIRED: Canva storage state is missing"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=str(state_path))
        page = await context.new_page()
        try:
            await page.goto(team_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1500)
            current = page.url.lower()
            if "login" in current or "signup" in current:
                return False, "AUTH_REQUIRED: Canva session expired"

            security = await _first_visible(page, [
                ("text", "Verify it's you"), ("text", "security check"),
                ("text", "two-step verification"), ("text", "captcha"),
            ], timeout=900)
            if security:
                return False, "AUTH_REQUIRED: Canva security verification required"

            invite_selectors = [
                ("role", ("button", "Invite students")),
                ("role", ("button", "Invite for free")),
                ("role", ("button", "Invite team members")),
                ("role", ("button", "Invite")),
                ("text", "Invite students"),
                ("text", "Invite for free"),
                ("text", "Invite team members"),
            ]
            invite = await _first_visible(page, invite_selectors, timeout=7000)

            # CANVA_TEAM_URL may point to Settings/People while Canva exposes
            # the Education invite CTA on the team Home page. Fall back to
            # Canva Home using the same authenticated/team context.
            if not invite:
                await page.goto("https://www.canva.com/", wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(2200)
                current = page.url.lower()
                if "login" in current or "signup" in current:
                    return False, "AUTH_REQUIRED: Canva session expired"
                invite = await _first_visible(page, invite_selectors, timeout=8000)

            if not invite:
                logger.warning("[CANVA] Invite CTA not found. url=%s title=%s", page.url, await page.title())
                return False, "UI_ERROR: Invite students / Invite for free button not found"
            await invite.click()
            await page.wait_for_timeout(900)

            email_box = await _first_visible(page, [
                ("placeholder", "Email"), ("label", "Email"),
                ("placeholder", "name or email"), ("placeholder", "email address"),
                ("placeholder", "Enter email"), ("label", "Email address"),
            ], timeout=7000)
            if not email_box:
                return False, "UI_ERROR: invitation email field not found"
            await email_box.fill(email)
            await page.wait_for_timeout(900)

            role_button = await _first_visible(page, [
                ("role", ("button", "Assign role")), ("text", "Assign role"),
            ], timeout=2500)
            if role_button:
                await role_button.click()
                student = await _first_visible(page, [
                    ("role", ("option", "Student")),
                    ("role", ("menuitem", "Student")),
                    ("text", "Student"),
                ], timeout=3500)
                if student:
                    await student.click()

            send = await _first_visible(page, [
                ("role", ("button", "Send invitations")),
                ("role", ("button", "Send invitation")),
                ("text", "Send invitations"),
            ], timeout=5000)
            if not send:
                return False, "UI_ERROR: Send invitations button not found"
            await send.click()

            # A success toast/message is preferred. If Canva closes the invite
            # dialog after a successful click, that is also accepted as UI ack.
            success = await _first_visible(page, [
                ("text", "Invitation sent"), ("text", "Invitations sent"),
                ("text", "invited"), ("text", "successfully"),
            ], timeout=6500)
            if not success:
                # Check for explicit errors before using dialog-close acknowledgement.
                error = await _first_visible(page, [
                    ("text", "Something went wrong"), ("text", "couldn't invite"),
                    ("text", "cannot invite"), ("text", "invalid email"),
                ], timeout=1000)
                if error:
                    return False, "INVITE_REJECTED: Canva rejected the invitation"
                try:
                    still_open = await send.is_visible()
                except Exception:
                    still_open = False
                if still_open:
                    return False, "UNCONFIRMED: Canva did not confirm invitation"

            # Persist any harmless session refresh Canva performed.
            await context.storage_state(path=str(state_path))
            return True, "Invitation sent"
        except Exception as exc:
            logger.exception("[CANVA] invite failed for order email=%s", email)
            return False, f"UI_ERROR: {type(exc).__name__}: {str(exc)[:180]}"
        finally:
            await context.close()
            await browser.close()


async def process_canva_orders_once() -> None:
    if not railway_worker_enabled() or _LOCK.locked():
        return
    async with _LOCK:
        db = SessionLocal()
        try:
            order = (
                db.query(Order)
                .join(Service, Order.service_id == Service.id)
                .filter(
                    Service.fulfillment_type == "canva",
                    Order.status.in_(["manual_pending", "canva_retry"]),
                    Order.customer_email.isnot(None),
                )
                .order_by(Order.created_at.asc())
                .first()
            )
            if not order:
                return
            order.status = "canva_processing"
            order.note = "Canva: sending email-specific Student invitation."
            order_id = order.id
            email = (order.customer_email or "").strip().lower()
            db.commit()
        finally:
            db.close()

        ok, detail = await send_invite(email)
        db = SessionLocal()
        try:
            order = db.get(Order, order_id)
            if not order:
                return
            service = order.service
            if ok:
                order.status = "completed"
                order.completed_at = datetime.utcnow()
                order.delivered_info = f"Canva Education invitation sent to: {email}"
                order.note = "Canva Education email invitation sent automatically."
                complete_reserved_stock(db, order.service_id, order.quantity)
                db.commit()
                # Load relationships before notifications use them outside this transaction.
                _ = order.user.telegram_id
                await notify_user_order_completed(order, service)
                await notify_channel_order_completed(order, service, db)
                logger.info("[CANVA] completed order=%s email=%s", order.order_code, email)
            else:
                auth = detail.startswith("AUTH_REQUIRED")
                order.status = "canva_auth_required" if auth else "canva_retry"
                order.note = f"Canva automation: {detail}"
                db.commit()
                if auth:
                    await send_admin_message(
                        f"⚠️ Canva authentication required\nOrder: {order.order_code}\nEmail: {email}\n{detail}",
                        db=db,
                    )
                else:
                    logger.warning("[CANVA] order=%s will retry: %s", order.order_code, detail)
        finally:
            db.close()


async def canva_invite_job() -> None:
    interval = max(15, int(os.getenv("CANVA_WORKER_SECONDS", "30")))
    while True:
        try:
            await process_canva_orders_once()
        except Exception:
            logger.exception("[CANVA] background worker iteration failed")
        await asyncio.sleep(interval)
