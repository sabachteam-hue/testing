import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)


BEP20_USDT_CONTRACT = "0x55d398326f99059ff775485246999027b3197955"
TRC20_USDT_CONTRACT = "TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj"

# Some hosting providers' IP ranges are geo-blocked by Binance (HTTP 451) -
# this includes Railway, and even Cloudflare Workers (Binance blocks whole
# cloud/datacenter IP ranges, not just one host). If BINANCE_HTTP_PROXY is
# set to a residential proxy URL (e.g. from Webshare), requests to Binance
# are routed through it instead, since Binance does allow residential IPs.
# Format: http://username:password@proxy-host:proxy-port
BINANCE_API_BASE = "https://api.binance.com"
BINANCE_HTTP_PROXY = os.getenv("BINANCE_HTTP_PROXY", "").strip() or None


def _binance_http_client(**kwargs) -> httpx.AsyncClient:
    if BINANCE_HTTP_PROXY:
        kwargs["proxy"] = BINANCE_HTTP_PROXY
    return httpx.AsyncClient(**kwargs)


@dataclass
class PaymentVerificationResult:
    verified: bool
    amount: float = 0.0
    status: str = "failed"
    reason: str | None = None
    from_address: str | None = None
    to_address: str | None = None
    contract_address: str | None = None
    raw_response: dict[str, Any] | list[Any] | None = None

    def raw_json(self) -> str:
        return json.dumps(self.raw_response or {}, default=str)


def _amount_from_token(value: str | int, decimals: str | int = 18) -> Decimal:
    return Decimal(str(value)) / (Decimal(10) ** int(decimals))


def _matches_amount(actual: Decimal, expected_amount: float) -> bool:
    return abs(actual - Decimal(str(expected_amount))) <= Decimal("0.000001")


# Free, no-API-key public BSC RPC endpoints (used in order, first one that
# responds wins). No BscScan/Etherscan key or paid plan required.
BSC_RPC_ENDPOINTS = [
    "https://bsc-dataseed.binance.org/",
    "https://bsc-dataseed1.defibit.io/",
    "https://bsc-dataseed1.ninicoin.io/",
    "https://bsc-rpc.publicnode.com",
]

# keccak256("Transfer(address,address,uint256)") - standard ERC20/BEP20 Transfer event topic
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


async def _bsc_rpc_call(client: httpx.AsyncClient, method: str, params: list) -> Any:
    last_error: Exception | None = None
    for endpoint in BSC_RPC_ENDPOINTS:
        try:
            response = await client.post(
                endpoint,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            response.raise_for_status()
            body = response.json()
            if "error" in body and body["error"]:
                last_error = RuntimeError(str(body["error"]))
                continue
            return body.get("result")
        except Exception as exc:  # noqa: BLE001 - try next endpoint on any failure
            last_error = exc
            continue
    raise RuntimeError(f"All BSC RPC endpoints failed: {last_error}")


def _topic_to_address(topic: str) -> str:
    return "0x" + topic[-40:]


async def verify_bep20_payment(tx_hash: str, expected_amount: float, admin_address: str) -> PaymentVerificationResult:
    # Verifies directly against BNB Smart Chain via free public RPC nodes instead
    # of BscScan/Etherscan (which now require a paid plan for BSC access). No API
    # key, no rate-limit paywall.
    confirmations_required = int(os.getenv("REQUIRED_CONFIRMATIONS", "2"))
    tx_hash = tx_hash.strip()
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            receipt = await _bsc_rpc_call(client, "eth_getTransactionReceipt", [tx_hash])
        except Exception as exc:  # noqa: BLE001
            return PaymentVerificationResult(False, 0.0, "failed", f"BSC RPC error: {exc}")

        if not receipt:
            return PaymentVerificationResult(False, 0.0, "pending", "Transaction not found yet (may still be broadcasting)")

        if receipt.get("status") != "0x1":
            return PaymentVerificationResult(False, 0.0, "failed", "Transaction failed on-chain", raw_response=receipt)

        logs = receipt.get("logs", [])
        matching_log = None
        for log in logs:
            if str(log.get("address", "")).lower() != BEP20_USDT_CONTRACT.lower():
                continue
            topics = log.get("topics", [])
            if not topics or topics[0].lower() != TRANSFER_TOPIC:
                continue
            matching_log = log
            break

        if not matching_log:
            return PaymentVerificationResult(False, 0.0, "failed", "No USDT (BEP20) transfer found in this transaction", raw_response=receipt)

        topics = matching_log["topics"]
        from_address = _topic_to_address(topics[1])
        to_address = _topic_to_address(topics[2])
        amount = _amount_from_token(int(matching_log["data"], 16), 18)

        if to_address.lower() != admin_address.lower():
            return PaymentVerificationResult(False, float(amount), "failed", "Recipient wallet does not match", raw_response=matching_log)
        if not _matches_amount(amount, expected_amount):
            return PaymentVerificationResult(False, float(amount), "failed", "Amount does not match", raw_response=matching_log)

        try:
            latest_block_hex = await _bsc_rpc_call(client, "eth_blockNumber", [])
            latest_block = int(latest_block_hex, 16)
            tx_block = int(receipt["blockNumber"], 16)
            confirmations = latest_block - tx_block
        except Exception:  # noqa: BLE001
            # Fail-safe, not fail-open: if we can't confirm confirmations right now,
            # treat it as still pending rather than auto-crediting. The user (or a
            # retry/admin check) can simply try again shortly.
            return PaymentVerificationResult(False, float(amount), "pending", "Could not confirm block depth, please retry shortly", raw_response=matching_log)

        if confirmations < confirmations_required:
            return PaymentVerificationResult(False, float(amount), "pending", "Waiting for confirmations", raw_response=matching_log)

        return PaymentVerificationResult(
            True,
            float(amount),
            "verified",
            from_address=from_address,
            to_address=to_address,
            contract_address=BEP20_USDT_CONTRACT,
            raw_response=matching_log,
        )


async def verify_trc20_payment(tx_hash: str, expected_amount: float, admin_address: str) -> PaymentVerificationResult:
    headers = {}
    api_key = os.getenv("TRONSCAN_API_KEY")
    if api_key:
        headers["TRON-PRO-API-KEY"] = api_key
    url = "https://apilist.tronscanapi.com/api/transaction-info"
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        response = await client.get(url, params={"hash": tx_hash})
        response.raise_for_status()
        body = response.json()

    if not isinstance(body, dict):
        return PaymentVerificationResult(False, 0.0, "failed", "Unexpected TronScan API response", raw_response={"raw": str(body)})

    transfers = body.get("trc20TransferInfo") or body.get("tokenTransferInfo") or []
    if isinstance(transfers, dict):
        transfers = [transfers]
    if not isinstance(transfers, list):
        transfers = []

    for transfer in transfers:
        if not isinstance(transfer, dict):
            continue
        contract = transfer.get("contract_address") or transfer.get("contractAddress") or transfer.get("tokenId")
        to_address = transfer.get("to_address") or transfer.get("to_address_tag") or transfer.get("toAddress")
        from_address = transfer.get("from_address") or transfer.get("fromAddress")
        raw_amount = transfer.get("amount_str") or transfer.get("amount") or transfer.get("quant") or "0"
        decimals = transfer.get("decimals") or transfer.get("tokenDecimal") or 6
        amount = _amount_from_token(raw_amount, decimals)
        confirmed = bool(body.get("confirmed", body.get("contractRet") == "SUCCESS"))
        if contract and str(contract) != TRC20_USDT_CONTRACT:
            continue
        if to_address != admin_address:
            return PaymentVerificationResult(False, float(amount), "failed", "Recipient wallet does not match", raw_response=body)
        if not _matches_amount(amount, expected_amount):
            return PaymentVerificationResult(False, float(amount), "failed", "Amount does not match", raw_response=body)
        if not confirmed:
            return PaymentVerificationResult(False, float(amount), "pending", "Waiting for confirmation", raw_response=body)
        return PaymentVerificationResult(
            True,
            float(amount),
            "verified",
            from_address=from_address,
            to_address=to_address,
            contract_address=TRC20_USDT_CONTRACT,
            raw_response=body,
        )

    return PaymentVerificationResult(False, 0.0, "failed", "Transaction not found", raw_response=body)


async def verify_binance_payment(tx_hash: str, expected_amount: float, admin_address: str = "") -> PaymentVerificationResult:
    """Binance Pay (UID/Pay-ID transfers) don't produce a blockchain tx hash —
    only an internal Binance transaction/order id. Those never show up in the
    on-chain deposit history endpoint, which is why this used to always fall
    back to manual review. We now check Binance's own Pay transaction history
    first (matches the reference the customer sends after paying), and only
    fall back to the on-chain deposit lookup for admins who actually configured
    "Binance" as an on-chain deposit address instead of a Pay ID."""
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        return PaymentVerificationResult(False, 0.0, "failed", "BINANCE_API_KEY and BINANCE_API_SECRET are required")

    reference = tx_hash.strip()

    pay_result = await _verify_binance_pay_transaction(reference, expected_amount, api_key, api_secret)
    if pay_result.status != "not_found":
        return pay_result

    # Fallback: some admins configure "Binance" as a regular on-chain deposit
    # address (BEP20/TRC20 hosted on Binance) rather than a Binance Pay ID.
    return await _verify_binance_onchain_deposit(reference, expected_amount, api_key, api_secret)


async def _verify_binance_pay_transaction(
    reference: str, expected_amount: float, api_key: str, api_secret: str
) -> PaymentVerificationResult:
    timestamp = int(time.time() * 1000)
    params = {"timestamp": timestamp, "recvWindow": 5000, "limit": 100}
    query = urlencode(params)
    signature = hmac.new(api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    url = f"{BINANCE_API_BASE}/sapi/v1/pay/transactions?{query}&signature={signature}"
    headers = {"X-MBX-APIKEY": api_key}

    try:
        async with _binance_http_client(timeout=30, headers=headers) as client:
            response = await client.get(url)
            response.raise_for_status()
            body = response.json()
    except Exception as exc:  # noqa: BLE001
        return PaymentVerificationResult(False, 0.0, "failed", f"Binance Pay API error: {exc}")

    records = body.get("data") if isinstance(body, dict) else body
    if not isinstance(records, list):
        error_message = body.get("message") if isinstance(body, dict) else "Unexpected Binance Pay API response"
        return PaymentVerificationResult(False, 0.0, "failed", f"Binance Pay API error: {error_message}", raw_response=body if isinstance(body, dict) else {"raw": str(body)})

    for record in records:
        if not isinstance(record, dict):
            continue
        record_ids = {
            str(record.get("transactionId") or ""),
            str(record.get("orderId") or ""),
            str(record.get("tranId") or ""),
            str(record.get("orderNo") or ""),
        }
        record_ids.discard("")
        if reference not in record_ids and reference.lower() not in {r.lower() for r in record_ids}:
            continue

        currency = str(record.get("currency") or record.get("orderCurrency") or "").upper()
        raw_amount = record.get("amount") or record.get("orderAmount") or "0"
        try:
            amount = Decimal(str(raw_amount))
        except Exception:  # noqa: BLE001
            amount = Decimal("0")

        if currency and currency != "USDT":
            return PaymentVerificationResult(False, float(amount), "failed", f"Payment currency was {currency}, not USDT", raw_response=record)
        if not _matches_amount(amount, expected_amount):
            return PaymentVerificationResult(False, float(amount), "failed", "Amount does not match", raw_response=record)

        return PaymentVerificationResult(
            True,
            float(amount),
            "verified",
            from_address=str(record.get("payerId") or record.get("counterParty") or ""),
            to_address="BINANCE_PAY",
            contract_address="BINANCE_PAY",
            raw_response=record,
        )

    return PaymentVerificationResult(False, 0.0, "not_found", "Not found in Binance Pay transaction history", raw_response=body if isinstance(body, dict) else None)


async def _verify_binance_onchain_deposit(
    tx_hash: str, expected_amount: float, api_key: str, api_secret: str
) -> PaymentVerificationResult:
    timestamp = int(time.time() * 1000)
    params = {"coin": "USDT", "txId": tx_hash, "timestamp": timestamp}
    query = urlencode(params)
    signature = hmac.new(api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    url = f"{BINANCE_API_BASE}/sapi/v1/capital/deposit/hisrec?{query}&signature={signature}"
    headers = {"X-MBX-APIKEY": api_key}
    try:
        async with _binance_http_client(timeout=30, headers=headers) as client:
            response = await client.get(url)
            response.raise_for_status()
            body = response.json()
    except Exception as exc:  # noqa: BLE001
        return PaymentVerificationResult(False, 0.0, "failed", f"Binance API error: {exc}")

    if isinstance(body, list):
        records = body
    elif isinstance(body, dict):
        records = body.get("data", [])
        if not isinstance(records, list):
            error_message = body.get("msg") or body.get("message") or "Unexpected Binance API response"
            return PaymentVerificationResult(False, 0.0, "failed", f"Binance API error: {error_message}", raw_response=body)
    else:
        records = []

    for record in records:
        if not isinstance(record, dict):
            continue
        record_tx = str(record.get("txId") or record.get("txID") or record.get("transactionId") or "")
        if record_tx and record_tx.lower() != tx_hash.lower():
            continue
        amount = Decimal(str(record.get("amount", "0")))
        status = int(record.get("status", -1)) if str(record.get("status", "")).lstrip("-").isdigit() else record.get("status")
        if not _matches_amount(amount, expected_amount):
            return PaymentVerificationResult(False, float(amount), "failed", "Amount does not match", raw_response=record)
        if status != 1:
            return PaymentVerificationResult(False, float(amount), "pending", "Binance deposit is not confirmed yet", raw_response=record)
        return PaymentVerificationResult(
            True,
            float(amount),
            "verified",
            from_address=record.get("address"),
            to_address=record.get("address"),
            contract_address="BINANCE_EXCHANGE",
            raw_response=record,
        )

    return PaymentVerificationResult(False, 0.0, "failed", "Binance deposit transaction not found", raw_response=body)


async def verify_bybit_payment(tx_hash: str, expected_amount: float, admin_address: str = "") -> PaymentVerificationResult:
    """Bybit Pay (UID / email / phone internal transfers) mirror Binance Pay:

    1. Off-chain internal deposit history (`/v5/asset/deposit/query-internal-record`)
       — this is what customers get as an order/tx ID after paying a Pay ID.
    2. Universal transfer list (UID→UID API transfers) when the reference is a transferId.
    3. On-chain deposit history as a fallback for admins who use a Bybit deposit address.
    """
    api_key = os.getenv("BYBIT_API_KEY", "")
    api_secret = os.getenv("BYBIT_API_SECRET", "")
    if not api_key or not api_secret:
        return PaymentVerificationResult(False, 0.0, "failed", "BYBIT_API_KEY and BYBIT_API_SECRET are required")

    reference = tx_hash.strip()

    pay_result = await _verify_bybit_internal_deposit(reference, expected_amount, api_key, api_secret)
    if pay_result.status != "not_found":
        return pay_result

    transfer_result = await _verify_bybit_universal_transfer(reference, expected_amount, api_key, api_secret)
    if transfer_result.status != "not_found":
        return transfer_result

    return await _verify_bybit_onchain_deposit(reference, expected_amount, api_key, api_secret)


def _bybit_headers(api_key: str, api_secret: str, query: str) -> dict[str, str]:
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    payload = f"{timestamp}{api_key}{recv_window}{query}"
    signature = hmac.new(api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN": signature,
    }


async def _bybit_get(path: str, params: dict, api_key: str, api_secret: str) -> dict | list | Any:
    query = urlencode(params)
    headers = _bybit_headers(api_key, api_secret, query)
    url = f"https://api.bybit.com{path}?{query}" if query else f"https://api.bybit.com{path}"
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


async def _verify_bybit_internal_deposit(
    reference: str, expected_amount: float, api_key: str, api_secret: str
) -> PaymentVerificationResult:
    """Bybit Pay / off-chain UID transfers — status: 1=Processing, 2=Success, 3=Failed."""
    params = {"coin": "USDT", "txID": reference, "limit": 50}
    try:
        body = await _bybit_get("/v5/asset/deposit/query-internal-record", params, api_key, api_secret)
    except Exception as exc:  # noqa: BLE001
        return PaymentVerificationResult(False, 0.0, "failed", f"Bybit Pay API error: {exc}")

    if not isinstance(body, dict):
        return PaymentVerificationResult(False, 0.0, "failed", "Unexpected Bybit Pay API response", raw_response={"raw": str(body)})

    if int(body.get("retCode") or 0) != 0:
        return PaymentVerificationResult(
            False,
            0.0,
            "failed",
            f"Bybit Pay API error: {body.get('retMsg') or body.get('retCode')}",
            raw_response=body,
        )

    result = body.get("result") or {}
    records = result.get("rows") if isinstance(result, dict) else []
    if not isinstance(records, list):
        records = []

    # If filtering by txID returned nothing, scan recent USDT internal deposits.
    if not records:
        try:
            body = await _bybit_get(
                "/v5/asset/deposit/query-internal-record",
                {"coin": "USDT", "limit": 50},
                api_key,
                api_secret,
            )
            result = body.get("result") if isinstance(body, dict) else {}
            records = (result or {}).get("rows") if isinstance(result, dict) else []
            if not isinstance(records, list):
                records = []
        except Exception as exc:  # noqa: BLE001
            return PaymentVerificationResult(False, 0.0, "failed", f"Bybit Pay API error: {exc}")

    reference_l = reference.lower()
    for record in records:
        if not isinstance(record, dict):
            continue
        record_ids = {
            str(record.get("txID") or ""),
            str(record.get("txId") or ""),
            str(record.get("id") or ""),
        }
        record_ids.discard("")
        if reference not in record_ids and reference_l not in {r.lower() for r in record_ids}:
            continue

        coin = str(record.get("coin") or "").upper()
        try:
            amount = Decimal(str(record.get("amount") or "0"))
        except Exception:  # noqa: BLE001
            amount = Decimal("0")

        if coin and coin != "USDT":
            return PaymentVerificationResult(False, float(amount), "failed", f"Payment coin was {coin}, not USDT", raw_response=record)
        if not _matches_amount(amount, expected_amount):
            return PaymentVerificationResult(False, float(amount), "failed", "Amount does not match", raw_response=record)

        status = record.get("status")
        status_int = int(status) if str(status).lstrip("-").isdigit() else -1
        if status_int == 1:
            return PaymentVerificationResult(False, float(amount), "pending", "Bybit Pay transfer is still processing", raw_response=record)
        if status_int == 3:
            return PaymentVerificationResult(False, float(amount), "failed", "Bybit Pay transfer failed", raw_response=record)
        if status_int != 2:
            return PaymentVerificationResult(False, float(amount), "pending", "Bybit Pay transfer is not credited yet", raw_response=record)

        return PaymentVerificationResult(
            True,
            float(amount),
            "verified",
            from_address=str(record.get("fromMemberId") or record.get("address") or ""),
            to_address="BYBIT_PAY",
            contract_address="BYBIT_PAY",
            raw_response=record,
        )

    return PaymentVerificationResult(False, 0.0, "not_found", "Not found in Bybit Pay / internal deposit history", raw_response=body if isinstance(body, dict) else None)


async def _verify_bybit_universal_transfer(
    reference: str, expected_amount: float, api_key: str, api_secret: str
) -> PaymentVerificationResult:
    params = {"transferId": reference, "coin": "USDT", "limit": 50}
    try:
        body = await _bybit_get("/v5/asset/transfer/query-universal-transfer-list", params, api_key, api_secret)
    except Exception as exc:  # noqa: BLE001
        return PaymentVerificationResult(False, 0.0, "failed", f"Bybit transfer API error: {exc}")

    if not isinstance(body, dict):
        return PaymentVerificationResult(False, 0.0, "not_found", "Unexpected Bybit transfer API response")

    if int(body.get("retCode") or 0) != 0:
        # Invalid transferId → treat as not found so other verifiers can run.
        return PaymentVerificationResult(False, 0.0, "not_found", str(body.get("retMsg") or "transfer not found"), raw_response=body)

    result = body.get("result") or {}
    records = result.get("list") if isinstance(result, dict) else []
    if not isinstance(records, list) or not records:
        return PaymentVerificationResult(False, 0.0, "not_found", "Not found in Bybit universal transfers", raw_response=body)

    for record in records:
        if not isinstance(record, dict):
            continue
        record_id = str(record.get("transferId") or "")
        if record_id and record_id.lower() != reference.lower():
            continue
        coin = str(record.get("coin") or "").upper()
        try:
            amount = Decimal(str(record.get("amount") or "0"))
        except Exception:  # noqa: BLE001
            amount = Decimal("0")
        if coin and coin != "USDT":
            return PaymentVerificationResult(False, float(amount), "failed", f"Transfer coin was {coin}, not USDT", raw_response=record)
        if not _matches_amount(amount, expected_amount):
            return PaymentVerificationResult(False, float(amount), "failed", "Amount does not match", raw_response=record)
        status = str(record.get("status") or "").upper()
        if status in {"PENDING", "STATUS_UNKNOWN"}:
            return PaymentVerificationResult(False, float(amount), "pending", "Bybit transfer is still pending", raw_response=record)
        if status == "FAILED":
            return PaymentVerificationResult(False, float(amount), "failed", "Bybit transfer failed", raw_response=record)
        if status and status != "SUCCESS":
            return PaymentVerificationResult(False, float(amount), "pending", f"Bybit transfer status: {status}", raw_response=record)
        return PaymentVerificationResult(
            True,
            float(amount),
            "verified",
            from_address=str(record.get("fromMemberId") or ""),
            to_address=str(record.get("toMemberId") or "BYBIT_TRANSFER"),
            contract_address="BYBIT_TRANSFER",
            raw_response=record,
        )

    return PaymentVerificationResult(False, 0.0, "not_found", "Not found in Bybit universal transfers", raw_response=body)


async def _verify_bybit_onchain_deposit(
    tx_hash: str, expected_amount: float, api_key: str, api_secret: str
) -> PaymentVerificationResult:
    params = {"coin": "USDT", "txID": tx_hash}
    try:
        body = await _bybit_get("/v5/asset/deposit/query-record", params, api_key, api_secret)
    except Exception as exc:  # noqa: BLE001
        return PaymentVerificationResult(False, 0.0, "failed", f"Bybit API error: {exc}")

    result = body.get("result", {}) if isinstance(body, dict) else {}
    if not isinstance(result, dict):
        result = {}
    records = result.get("rows") or result.get("list") or []
    if not isinstance(records, list):
        error_message = body.get("retMsg") if isinstance(body, dict) else "Unexpected Bybit API response"
        return PaymentVerificationResult(False, 0.0, "failed", f"Bybit API error: {error_message}", raw_response=body)

    for record in records:
        if not isinstance(record, dict):
            continue
        record_tx = str(record.get("txID") or record.get("txId") or record.get("txHash") or "")
        if record_tx and record_tx.lower() != tx_hash.lower():
            continue
        amount = Decimal(str(record.get("amount", "0")))
        status = str(record.get("status", "")).lower()
        if not _matches_amount(amount, expected_amount):
            return PaymentVerificationResult(False, float(amount), "failed", "Amount does not match", raw_response=record)
        if status not in {"3", "success", "credited", "completed"}:
            return PaymentVerificationResult(False, float(amount), "pending", "Bybit deposit is not credited yet", raw_response=record)
        return PaymentVerificationResult(
            True,
            float(amount),
            "verified",
            from_address=record.get("fromAddress"),
            to_address=record.get("toAddress"),
            contract_address="BYBIT_EXCHANGE",
            raw_response=record,
        )

    return PaymentVerificationResult(False, 0.0, "failed", "Bybit deposit transaction not found", raw_response=body)


async def verify_payment(network: str, tx_hash: str, expected_amount: float, admin_address: str) -> PaymentVerificationResult:
    try:
        normalized_network = network.upper()
        if normalized_network == "TRC20":
            return await verify_trc20_payment(tx_hash, expected_amount, admin_address)
        if normalized_network == "BINANCE":
            return await verify_binance_payment(tx_hash, expected_amount, admin_address)
        if normalized_network == "BYBIT":
            return await verify_bybit_payment(tx_hash, expected_amount, admin_address)
        return await verify_bep20_payment(tx_hash, expected_amount, admin_address)
    except Exception as exc:  # noqa: BLE001
        # A bug or an unexpected API response here must never leave the user
        # with total silence - always fall through to a normal "not verified
        # yet, sent for review" reply instead of the message getting swallowed.
        logger.exception("verify_payment crashed for network=%s tx=%s", network, tx_hash)
        return PaymentVerificationResult(False, 0.0, "failed", f"Verification error: {exc}")
