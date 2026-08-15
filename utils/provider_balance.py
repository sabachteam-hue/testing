"""Sync upstream provider wallet balance + low-balance admin alerts.

Best-effort only: must never break product/price/stock sync.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from database.models import Provider, Service
from utils.notifications import send_admin_message
from utils.provider_api import ProviderApiError, fetch_provider_account

logger = logging.getLogger(__name__)


def low_balance_threshold_usd() -> float:
    raw = (os.getenv("PROVIDER_LOW_BALANCE_USD") or "2").strip()
    try:
        value = float(raw)
    except ValueError:
        value = 2.0
    return max(value, 0.01)


def provider_bot_label(provider: Provider) -> str:
    for val in (provider.telegram_bot, provider.api_username, provider.name):
        text = (val or "").strip()
        if text:
            if text.startswith("@") or "t.me/" in text:
                return text
            return f"@{text.lstrip('@')}"
    return provider.name


def provider_has_imported_products(db, provider_id: int) -> bool:
    return (
        db.query(Service)
        .filter(Service.provider_id == provider_id, Service.is_deleted.is_(False))
        .count()
        > 0
    )


async def sync_provider_balance(db, provider: Provider) -> tuple[float | None, str | None, str | None]:
    """Refresh api_balance / api_username. Never raises — returns (balance, username, err)."""
    if provider.type != "api" or not (provider.api_url or provider.balance_url):
        return getattr(provider, "api_balance", None), getattr(provider, "api_username", None), None

    try:
        info = await fetch_provider_account(provider)
    except ProviderApiError as exc:
        logger.warning("[PROVIDER-BALANCE] %s: %s", provider.name, exc)
        return provider.api_balance, provider.api_username, str(exc)[:220]
    except Exception as exc:  # noqa: BLE001
        logger.exception("[PROVIDER-BALANCE] unexpected error for %s", provider.name)
        return provider.api_balance, provider.api_username, str(exc)[:220]

    try:
        balance = info.get("balance")
        username = info.get("username")

        if balance is not None:
            provider.api_balance = float(balance)
            provider.balance_synced_at = datetime.utcnow()
            if provider.api_balance >= low_balance_threshold_usd():
                provider.low_balance_alert_active = False
        if username:
            provider.api_username = str(username).strip()[:120]

        db.add(provider)
        await maybe_notify_low_balance(db, provider)
        return provider.api_balance, provider.api_username, None
    except Exception as exc:  # noqa: BLE001
        logger.exception("[PROVIDER-BALANCE] failed to store balance for %s", provider.name)
        return provider.api_balance, provider.api_username, str(exc)[:120]


async def maybe_notify_low_balance(db, provider: Provider) -> None:
    """DM admin when an API provider with imported products drops below threshold.

    Never raises — notification failures are logged only.
    """
    try:
        balance = provider.api_balance
        if balance is None:
            return
        threshold = low_balance_threshold_usd()
        if balance >= threshold:
            return
        if not provider_has_imported_products(db, provider.id):
            return
        if provider.low_balance_alert_active:
            return

        bot_label = provider_bot_label(provider)
        product_count = (
            db.query(Service)
            .filter(Service.provider_id == provider.id, Service.is_deleted.is_(False))
            .count()
        )
        text = (
            "⚠️ <b>Low provider API balance</b>\n\n"
            f"Bot: <b>{bot_label}</b>\n"
            f"Provider: {provider.name}\n"
            f"Balance: <b>${balance:.2f}</b> USDT\n"
            f"Alert threshold: ${threshold:.2f}\n"
            f"Imported products in shop: {product_count}\n\n"
            "Top up the upstream bot wallet or disable products until balance is restored."
        )
        await send_admin_message(text, db=db, parse_mode="HTML")
        provider.low_balance_alert_active = True
        db.add(provider)
        logger.info("[PROVIDER-BALANCE] Low balance alert sent for %s ($%.2f)", provider.name, balance)
    except Exception:  # noqa: BLE001
        logger.exception("[PROVIDER-BALANCE] low-balance notify failed for %s", provider.name)
