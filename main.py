import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from aiogram.types import Update
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from admin.routes import router as admin_router
from api.docs import router as docs_router
from api.payfast import router as payfast_router
from api.v1 import router as api_v1_router
from api.web import cors_allow_origins, router as api_web_router
from api.webhooks import router as api_webhooks_router
from bot.bot_main import create_bot, setup_webhook_bot
from database.models import SessionLocal, Service, init_db
from utils.background_tasks import (
    check_order_status_job,
    expire_active_sales_job,
    expire_unpaid_checkouts_job,
    process_referral_payouts_job,
    sync_provider_stock_job,
    verify_transactions_job,
)
from utils.security import (
    SensitiveDataFilter,
    constant_time_compare,
    is_production,
    validate_environment_secrets,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Add log sanitization filter to prevent accidental leakage of secrets in logs
for handler in logging.root.handlers:
    handler.addFilter(SensitiveDataFilter())

WEBHOOK_PATH = "/telegram/webhook"

# Directories referenced by admin/routes.py and admin/templates must exist
# before StaticFiles mounts them, otherwise FastAPI raises at startup.
os.makedirs("static/uploads/announcements", exist_ok=True)
os.makedirs("admin/static", exist_ok=True)


def get_webhook_url() -> str | None:
    base_url = os.getenv("WEBHOOK_URL") or os.getenv("RAILWAY_PUBLIC_DOMAIN")
    if not base_url:
        return None
    if not base_url.startswith("http"):
        base_url = f"https://{base_url}"
    return base_url.rstrip("/") + WEBHOOK_PATH


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Validate critical secrets on startup (fails in production if insecure)
    validate_environment_secrets()

    init_db()

    # Legacy cleanup: products that used the removed Canva auto-invite mode
    # return to normal manual fulfillment while preserving their email setting.
    db = SessionLocal()
    try:
        db.query(Service).filter(Service.fulfillment_type == "canva").update(
            {Service.fulfillment_type: "manual"}, synchronize_session=False
        )
        db.commit()
    finally:
        db.close()

    try:
        from migrate_payfast_reference import run as migrate_payfast_reference
        migrate_payfast_reference()
    except Exception as e:
        logger.info(f"Migration skip (payfast_reference, likely already applied): {e}")

    try:
        from migrate_stock_modes import run as migrate_stock_modes
        migrate_stock_modes()
        logger.info("Stock mode migration ready.")
    except Exception as e:
        logger.info(f"Migration skip (stock modes, likely already applied): {e}")

    webhook_url = get_webhook_url()
    if webhook_url:
        try:
            bot, dispatcher = await setup_webhook_bot(webhook_url)
            app.state.bot = bot
            app.state.dispatcher = dispatcher
        except RuntimeError as exc:
            logger.warning("Telegram bot disabled: %s", exc)
            app.state.bot = None
            app.state.dispatcher = None
    else:
        logger.warning(
            "WEBHOOK_URL / RAILWAY_PUBLIC_DOMAIN is not set; Telegram webhook was not configured."
        )
        app.state.bot = create_bot()
        app.state.dispatcher = None

    # Background jobs: provider stock/price auto-sync, order status polling,
    # deposit verification, and referral payouts.
    background_jobs = [
        asyncio.create_task(sync_provider_stock_job()),
        asyncio.create_task(check_order_status_job()),
        asyncio.create_task(verify_transactions_job()),
        asyncio.create_task(process_referral_payouts_job()),
        asyncio.create_task(expire_unpaid_checkouts_job()),
        asyncio.create_task(expire_active_sales_job()),
    ]

    yield

    for task in background_jobs:
        task.cancel()
    if app.state.bot:
        await app.state.bot.session.close()


app = FastAPI(title="SMF SHOP", lifespan=lifespan)

# Determine secure session secret (falls back to local placeholder only in dev)
_session_secret = (os.getenv("SESSION_SECRET") or os.getenv("SECRET_KEY") or "").strip()
if not _session_secret and not is_production():
    _session_secret = "dev-insecure-session-key-local-only"

app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    same_site="lax",
    https_only=is_production(),
    max_age=7200,
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds HTTP security headers to all responses without breaking Mini App embedding or fonts."""
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"

        # Mini App / Web catalog needs to be embeddable in Telegram Desktop / Web
        if path.startswith(("/mini", "/app", "/shop", "/static/mini-app")):
            frame_ancestors = "frame-ancestors 'self' https://web.telegram.org https://k.telegram.org https://*.telegram.org"
        else:
            frame_ancestors = "frame-ancestors 'self'"
            response.headers["X-Frame-Options"] = "SAMEORIGIN"

        csp = (
            f"default-src 'self'; "
            f"script-src 'self' 'unsafe-inline' https://telegram.org; "
            f"style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            f"font-src 'self' https://fonts.gstatic.com data:; "
            f"img-src 'self' data: https: blob:; "
            f"connect-src 'self'; "
            f"{frame_ancestors};"
        )
        response.headers["Content-Security-Policy"] = csp

        if is_production():
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        return response


class AdminCSRFMiddleware(BaseHTTPMiddleware):
    """Enforces CSRF token validation on all state-changing admin POST/PUT/DELETE requests."""
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/admin") and request.method in ("POST", "PUT", "PATCH", "DELETE"):
            # Exclude /admin/login from CSRF (login has dedicated rate-limiting and lockout protection)
            if path != "/admin/login":
                expected_token = request.session.get("csrf_token")
                submitted_token = request.headers.get("x-csrf-token")
                if not submitted_token:
                    try:
                        form = await request.form()
                        submitted_token = form.get("csrf_token")
                    except Exception:
                        submitted_token = None

                if not expected_token or not submitted_token or not constant_time_compare(submitted_token, expected_token):
                    logger.warning(f"CSRF validation failed for admin path: {path}")
                    return PlainTextResponse("CSRF verification failed. Please refresh the page.", status_code=403)

        return await call_next(request)


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(AdminCSRFMiddleware)

_cors_origins = cors_allow_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _cors_origins == "*" else _cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    """In production, prevent unhandled internal error stack traces from leaking to clients."""
    logger.exception("Unhandled server error processing %s %s", request.method, request.url.path)
    if is_production():
        return JSONResponse(
            status_code=500,
            content={"detail": "An internal server error occurred. Please contact support."},
        )
    # In development, let FastAPI return normal error details for debugging
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
    )


app.mount("/admin/static", StaticFiles(directory="admin/static"), name="admin-static")
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(admin_router)
app.include_router(api_v1_router)
app.include_router(api_web_router)
app.include_router(docs_router)
app.include_router(api_webhooks_router)
app.include_router(payfast_router)


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    bot = app.state.bot
    dispatcher = app.state.dispatcher
    if not bot or not dispatcher:
        return PlainTextResponse("Bot not configured", status_code=503)
    data = await request.json()
    try:
        update = Update.model_validate(data, context={"bot": bot})
        await dispatcher.feed_update(bot, update)
    except Exception:
        # IMPORTANT: always return 200 to Telegram, even if a handler crashed.
        # If an exception bubbles up here, FastAPI returns a 500 and Telegram
        # keeps re-sending the exact same update every few seconds.
        logger.exception("Unhandled error while processing Telegram update: %s", data)
    return PlainTextResponse("ok")


@app.api_route("/mini", methods=["GET", "HEAD"])
@app.api_route("/mini/", methods=["GET", "HEAD"])
@app.get("/app", include_in_schema=False)
@app.get("/app/", include_in_schema=False)
@app.get("/shop", include_in_schema=False)
def mini_shop():
    return FileResponse(
        "static/mini-app/index.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root(request: Request):
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" in accept:
        return mini_shop()
    return {"status": "ok", "service": "smfshop", "mini_app": "/mini"}
