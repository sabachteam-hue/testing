r"""Windows Canva local worker for SMF Shop.

Run this on the Windows PC where the dedicated Canva Chrome profile is already
logged in. Railway keeps payments/orders/database; this process only performs
Canva UI invitations and reports success back to Railway.

Required environment variables (CMD examples):
  set CANVA_WORKER_SERVER_URL=https://your-domain.example
  set CANVA_REMOTE_WORKER_TOKEN=your-long-random-secret

Optional:
  set CANVA_LOCAL_PROFILE_DIR=%USERPROFILE%\CanvaAutomationProfile
  set CANVA_LOCAL_POLL_SECONDS=10

Keep normal Chrome closed while this worker owns CanvaAutomationProfile.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.async_api import async_playwright

SERVER = (os.getenv("CANVA_WORKER_SERVER_URL") or "").strip().rstrip("/")
TOKEN = (os.getenv("CANVA_REMOTE_WORKER_TOKEN") or "").strip()
PROFILE_DIR = Path(os.path.expandvars(os.getenv("CANVA_LOCAL_PROFILE_DIR", r"%USERPROFILE%\CanvaAutomationProfile")))
POLL_SECONDS = max(5, int(os.getenv("CANVA_LOCAL_POLL_SECONDS", "10")))


def _validate_config() -> None:
    if not SERVER:
        raise SystemExit("CANVA_WORKER_SERVER_URL is missing")
    parsed = urlparse(SERVER)
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise SystemExit("CANVA_WORKER_SERVER_URL must use HTTPS")
    if len(TOKEN) < 24:
        raise SystemExit("CANVA_REMOTE_WORKER_TOKEN is missing or too short")
    if not PROFILE_DIR.exists():
        raise SystemExit(
            f"Canva Chrome profile not found: {PROFILE_DIR}\n"
            "Log in once using the dedicated CanvaAutomationProfile first."
        )


def _headers() -> dict[str, str]:
    return {"X-Canva-Worker-Token": TOKEN, "User-Agent": "SMF-Canva-Local-Worker/1.0"}


def claim_job() -> dict | None:
    response = requests.post(f"{SERVER}/internal/canva-worker/claim", headers=_headers(), timeout=30)
    response.raise_for_status()
    return response.json().get("job")


def report(order_code: str, success: bool, detail: str) -> None:
    response = requests.post(
        f"{SERVER}/internal/canva-worker/result",
        headers=_headers(),
        json={"order_code": order_code, "success": success, "detail": detail[:1000]},
        timeout=30,
    )
    response.raise_for_status()


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


async def _page_has_security_block(page) -> str | None:
    title = (await page.title()).strip()
    lower_title = title.lower()
    if "just a moment" in lower_title:
        return f"AUTH_REQUIRED: Canva security page shown ({title})"
    current = page.url.lower()
    if "/login" in current or "/signup" in current:
        return "AUTH_REQUIRED: Canva login required"
    security = await _first_visible(
        page,
        [
            ("text", "Verify it's you"),
            ("text", "security check"),
            ("text", "two-step verification"),
            ("text", "captcha"),
            ("text", "browser not safe"),
        ],
        timeout=700,
    )
    if security:
        return "AUTH_REQUIRED: Canva security verification required"
    return None


async def send_canva_invite(context, email: str, team_url: str) -> tuple[bool, str]:
    page = await context.new_page()
    try:
        await page.goto(team_url or "https://www.canva.com/", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1800)
        blocked = await _page_has_security_block(page)
        if blocked:
            return False, blocked

        invite_selectors = [
            ("role", ("button", "Invite students")),
            ("role", ("button", "Invite for free")),
            ("role", ("button", "Invite team members")),
            ("role", ("button", "Invite")),
            ("text", "Invite students"),
            ("text", "Invite for free"),
        ]
        invite = await _first_visible(page, invite_selectors, timeout=6500)
        if not invite and page.url.rstrip("/") != "https://www.canva.com":
            await page.goto("https://www.canva.com/", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1800)
            blocked = await _page_has_security_block(page)
            if blocked:
                return False, blocked
            invite = await _first_visible(page, invite_selectors, timeout=7000)
        if not invite:
            return False, f"UI_ERROR: Invite button not found (url={page.url}, title={await page.title()})"
        await invite.click()

        email_box = await _first_visible(
            page,
            [
                ("placeholder", "Email"),
                ("label", "Email"),
                ("placeholder", "name or email"),
                ("placeholder", "email address"),
                ("role", ("textbox", "Email")),
            ],
            timeout=7000,
        )
        if not email_box:
            return False, "UI_ERROR: invitation email field not found"
        await email_box.fill(email)
        await page.wait_for_timeout(900)

        role_button = await _first_visible(
            page,
            [("role", ("button", "Assign role")), ("text", "Assign role")],
            timeout=3000,
        )
        if role_button:
            await role_button.click()
            student = await _first_visible(
                page,
                [
                    ("role", ("option", "Student")),
                    ("role", ("menuitem", "Student")),
                    ("text", "Student"),
                ],
                timeout=4000,
            )
            if student:
                await student.click()

        send = await _first_visible(
            page,
            [
                ("role", ("button", "Send invitations")),
                ("role", ("button", "Send invitation")),
                ("text", "Send invitations"),
                ("text", "Send invitation"),
            ],
            timeout=6500,
        )
        if not send:
            return False, "UI_ERROR: Send invitations button not found"
        await send.click()

        success = await _first_visible(
            page,
            [
                ("text", "Invitation sent"),
                ("text", "Invitations sent"),
                ("text", "invited"),
                ("text", "successfully"),
            ],
            timeout=7000,
        )
        if not success:
            error = await _first_visible(
                page,
                [
                    ("text", "Something went wrong"),
                    ("text", "couldn't invite"),
                    ("text", "cannot invite"),
                    ("text", "invalid email"),
                    ("text", "already a member"),
                ],
                timeout=1200,
            )
            if error:
                return False, "INVITE_REJECTED: Canva rejected the invitation"
            try:
                still_open = await send.is_visible()
            except Exception:
                still_open = False
            if still_open:
                return False, "UNCONFIRMED: Canva did not confirm invitation"

        return True, "Invitation sent"
    except Exception as exc:
        return False, f"UI_ERROR: {type(exc).__name__}: {str(exc)[:300]}"
    finally:
        await page.close()


async def run() -> None:
    _validate_config()
    print("SMF Canva local worker starting...")
    print(f"Server: {SERVER}")
    print(f"Chrome profile: {PROFILE_DIR}")
    print("Keep this window running while you want Canva orders processed.")
    print("Do not open normal Chrome with the same CanvaAutomationProfile at the same time.\n")

    async with async_playwright() as p:
        try:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel="chrome",
                headless=False,
                args=["--start-minimized", "--no-first-run", "--no-default-browser-check"],
                timeout=120000,
            )
        except Exception as exc:
            raise SystemExit(
                f"Could not open CanvaAutomationProfile: {exc}\n"
                "Close all Chrome windows using this profile, then try again."
            )

        try:
            while True:
                try:
                    job = await asyncio.to_thread(claim_job)
                    if not job:
                        await asyncio.sleep(POLL_SECONDS)
                        continue

                    order_code = job["order_code"]
                    email = job["email"]
                    print(f"[{time.strftime('%H:%M:%S')}] Processing {order_code} -> {email}")
                    ok, detail = await send_canva_invite(context, email, job.get("team_url") or "")
                    print(f"[{order_code}] {'SUCCESS' if ok else 'FAILED'}: {detail}")
                    await asyncio.to_thread(report, order_code, ok, detail)
                except requests.HTTPError as exc:
                    print(f"Worker API error: {exc}")
                    await asyncio.sleep(max(POLL_SECONDS, 15))
                except requests.RequestException as exc:
                    print(f"Network error: {exc}")
                    await asyncio.sleep(max(POLL_SECONDS, 15))
                except Exception as exc:
                    print(f"Worker error: {type(exc).__name__}: {exc}")
                    await asyncio.sleep(max(POLL_SECONDS, 10))
        finally:
            await context.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nCanva local worker stopped.")
