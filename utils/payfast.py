"""PayFast (Pakistan) Hosted Checkout integration.

This mirrors exactly what PayFast's own official WooCommerce plugin does
(GetAccessToken -> auto-submitting PostTransaction form -> PayFast redirects
back to our own SUCCESS_URL/FAILURE_URL, and separately POSTs a
server-to-server IPN callback to our CHECKOUT_URL). We reuse the same field
names/flow so behaviour matches a real, working integration rather than a
guess.

No webhook needs to be registered anywhere in a PayFast dashboard - the
callback URL is sent fresh with every single transaction, so it always
points back at wherever this bot is currently deployed.
"""

import hashlib
import html
from dataclasses import dataclass

import httpx


@dataclass
class PayFastConfig:
    merchant_id: str
    secured_key: str
    base_url: str
    store_id: str = ""
    currency_code: str = "PKR"


async def get_payfast_token(config: PayFastConfig, amount: float, basket_id: str) -> str | None:
    """Calls GetAccessToken exactly like the official plugin does.

    Never raises - any network/HTTP/parsing error is logged and results in
    None being returned, so the caller can show a clean error page instead
    of crashing with a raw 500.
    """
    import logging

    logger = logging.getLogger(__name__)

    url = f"{config.base_url.rstrip('/')}/Ecommerce/api/Transaction/GetAccessToken"
    txn_amt = f"{amount:.2f}"
    body = (
        f"MERCHANT_ID={config.merchant_id}"
        f"&SECURED_KEY={config.secured_key}"
        f"&TXNAMT={txn_amt}"
        f"&BASKET_ID={basket_id}"
        f"&CURRENCY_CODE={config.currency_code}"
    )
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "SMFSHOP/1.0",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, content=body, headers=headers)
            logger.info("[PAYFAST] GetAccessToken status=%s body=%s", response.status_code, response.text[:500])
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        logger.error("[PAYFAST] GetAccessToken HTTP error: %s - body=%s", exc, exc.response.text[:500])
        return None
    except httpx.RequestError as exc:
        logger.error("[PAYFAST] GetAccessToken request failed (network/DNS/timeout): %s", exc)
        return None
    except ValueError as exc:
        # response.json() failed - PayFast returned non-JSON (e.g. an HTML error page)
        logger.error("[PAYFAST] GetAccessToken returned non-JSON response: %s - body=%s", exc, response.text[:500])
        return None

    token = data.get("ACCESS_TOKEN")
    if not token:
        logger.error("[PAYFAST] GetAccessToken response had no ACCESS_TOKEN: %s", data)
    return token


def build_checkout_html(
    config: PayFastConfig,
    token: str,
    amount: float,
    basket_id: str,
    order_id: str,
    order_date: str,
    store_name: str,
    description: str,
    customer_mobile: str,
    customer_email: str,
    success_url: str,
    failure_url: str,
    callback_url: str,
) -> str:
    """Renders the same auto-submitting hidden form the official plugin uses,
    so the customer lands straight on PayFast's own secure checkout page."""
    payment_url = f"{config.base_url.rstrip('/')}/Ecommerce/api/Transaction/PostTransaction"
    signature = hashlib.sha256(str(order_id).encode("utf-8")).hexdigest()
    fields = {
        "MERCHANT_ID": config.merchant_id,
        "MERCHANT_NAME": store_name,
        "TOKEN": token,
        "PROCCODE": "00",
        "TXNAMT": f"{amount:.2f}",
        "CUSTOMER_MOBILE_NO": customer_mobile,
        "CUSTOMER_EMAIL_ADDRESS": customer_email,
        "SIGNATURE": signature,
        "PLUGIN_VERSION": "SMFSHOP-1.0",
        "TXNDESC": description,
        "SUCCESS_URL": success_url,
        "FAILURE_URL": failure_url,
        "BASKET_ID": basket_id,
        "ORDER_DATE": order_date,
        "CHECKOUT_URL": callback_url,
        "TRAN_TYPE": "ECOMM_PURCHASE",
        "STORE_ID": config.store_id,
        "CURRENCY_CODE": config.currency_code,
    }
    inputs = "\n".join(
        f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(str(value))}" />'
        for key, value in fields.items()
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Redirecting to PayFast...</title></head>
<body>
<p>Redirecting you to PayFast, please wait...</p>
<form action="{html.escape(payment_url)}" method="post" id="payfast_form">
{inputs}
</form>
<script>document.getElementById('payfast_form').submit();</script>
</body></html>"""


def validate_callback_hash(basket_id: str, err_code: str, validation_hash: str, config: PayFastConfig) -> bool:
    """Official PayFast formula (Merchant Integration Guide v1.2, section 3.2.3):
    SHA256 of "basket_id|secured_key|merchant_id|err_code" (pipe-separated, in this exact order).
    """
    protocol = f"{basket_id}|{config.secured_key}|{config.merchant_id}|{err_code}"
    expected = hashlib.sha256(protocol.encode("utf-8")).hexdigest()
    return expected == (validation_hash or "").strip()
