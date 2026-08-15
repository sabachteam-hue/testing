import asyncio
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from aiogram.types import Update
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from admin.routes import router as admin_router
from api.docs import router as docs_router
from api.payfast import router as payfast_router
from api.v1 import router as api_v1_router
from api.webhooks import router as api_webhooks_router
from bot.bot_main import create_bot, setup_webhook_bot
from database.models import init_db
from utils.background_tasks import (
    check_order_status_job,
    expire_active_sales_job,
    expire_unpaid_checkouts_job,
    process_referral_payouts_job,
    sync_provider_stock_job,
    verify_transactions_job,
)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
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
    init_db()
    try:
        from migrate_payfast_reference import run as migrate_payfast_reference

        migrate_payfast_reference()
    except Exception as e:
        logger.info(f"Migration skip (payfast_reference, likely already applied): {e}")
    try:
        conn = sqlite3.connect('/app/data/smm_reseller.db')
        cursor = conn.cursor()
        cursor.execute("ALTER TABLE announcements ADD COLUMN image_path TEXT")
        conn.commit()
        conn.close()
        logger.info("Migration: image_path column added successfully")
    except Exception as e:
        logger.info(f"Migration skip (likely already applied): {e}")
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
    # deposit verification, and referral payouts. These previously existed
    # as functions but were never actually started anywhere.
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
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "insecure-dev-secret-change-me"),
)
app.mount("/admin/static", StaticFiles(directory="admin/static"), name="admin-static")
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(admin_router)
app.include_router(api_v1_router)
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
        # keeps re-sending the exact same update every few seconds — this is
        # what caused messages/menus to repeat automatically. We log the full
        # traceback instead so the real bug is visible in the Railway logs,
        # while Telegram sees a 200 so it stops retrying.
        logger.exception("Unhandled error while processing Telegram update: %s", data)
    return PlainTextResponse("ok")
@app.get("/health")
def health():
    return {"status": "ok"}
@app.get("/")
def root():
    return {"status": "ok", "service": "smfshop"}
