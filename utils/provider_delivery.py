"""Shared parsing for provider purchase/status JSON (Shop Bot, MMOStore, etc.)."""

from __future__ import annotations

from typing import Any

FIELD_LABELS = {
    "user": "Email/Username",
    "email": "Email",
    "username": "Username",
    "password": "Password",
    "verifyEmail": "Recovery Email",
    "expiryText": "Expiry",
    "otherInfo": "Note",
}

ACCOUNT_LIST_KEYS = (
    "deliveredAccounts",
    "delivered_accounts",
    "delivered_items",
    "items",
    "accounts",
    "accountList",
    "account_list",
    "list",
    "delivered",
    "delivery",
    "stock_lines",
    "lines",
    "keys",
    "codes",
    "full_data",
    "fullData",
    "product_data",
    "productData",
)
EXCLUDED_ACCOUNT_FIELDS = {"productItemId", "deliveredAt", "unitCost", "unitCostCurrency", "unitCostVnd", "_id", "id"}

COMPLETED_STATUSES = {
    "completed",
    "complete",
    "delivered",
    "done",
    "success",
    "successful",
    "fulfilled",
    "paid",
    "active",
}

FAILED_STATUSES = {"failed", "cancelled", "canceled", "rejected", "refunded"}

STRUCTURAL_KEYS = {
    "orders",
    "order",
    "order_group",
    "orderGroup",
    "orderCode",
    "status",
    "state",
    "success",
    "ok",
    "message",
    "error",
}


def _as_dict(value: Any) -> dict | None:
    return value if isinstance(value, dict) else None


def provider_delivery_sources(response: dict) -> list[dict]:
    sources: list[dict] = [response]
    order_obj = _as_dict(response.get("order"))
    if order_obj:
        sources.append(order_obj)
    rows = response.get("orders")
    if isinstance(rows, list):
        sources.extend([row for row in rows if isinstance(row, dict)])
    data = response.get("data")
    data_obj = _as_dict(data)
    if data_obj:
        sources.append(data_obj)
        nested_order = _as_dict(data_obj.get("order"))
        if nested_order:
            sources.append(nested_order)
        nested_rows = data_obj.get("orders")
        if isinstance(nested_rows, list):
            sources.extend([row for row in nested_rows if isinstance(row, dict)])
        # Shop Bot sometimes nests again: data.data / data.result / data.list
        for nest_key in ("data", "result", "payload", "response"):
            nested = data_obj.get(nest_key)
            if isinstance(nested, dict):
                sources.append(nested)
            elif isinstance(nested, list) and nested:
                sources.append({"items": nested})
            elif isinstance(nested, str) and nested.strip():
                sources.append({"content": nested})
        for list_key in ("list", "accounts", "deliveredAccounts", "items"):
            nested_list = data_obj.get(list_key)
            if isinstance(nested_list, list) and nested_list:
                sources.append({list_key: nested_list})
    # Some APIs return data as a bare list of account strings/objects.
    elif isinstance(data, list) and data:
        sources.append({"items": data})
    elif isinstance(data, str) and data.strip():
        sources.append({"content": data})
    result = response.get("result")
    if isinstance(result, dict):
        sources.append(result)
    elif isinstance(result, list) and result:
        sources.append({"items": result})
    elif isinstance(result, str) and result.strip():
        sources.append({"content": result})
    return sources


def extract_provider_order_id(response: dict | str | int | float) -> str:
    if isinstance(response, (str, int, float)):
        text = str(response).strip()
        return text if text else ""
    if not isinstance(response, dict):
        return ""
    for source in provider_delivery_sources(response):
        order_value = source.get("order")
        if order_value not in (None, "") and not isinstance(order_value, dict):
            return str(order_value)
        for key in (
            "order_group",
            "orderGroup",
            "orderCode",
            "order_code",
            "orderId",
            "order_id",
            "request_id",
            "external_order_id",
            "id",
            "_id",
        ):
            value = source.get(key)
            if value not in (None, ""):
                return str(value)
    return ""


def extract_provider_status(response: dict | str) -> str:
    if isinstance(response, str):
        text = response.strip()
        return text if text else "submitted"
    if not isinstance(response, dict):
        return "submitted"
    for source in provider_delivery_sources(response):
        for key in ("status", "state", "order_status", "orderStatus", "payment_status", "paymentStatus"):
            value = source.get(key)
            if value not in (None, ""):
                return str(value)
    message = response.get("message")
    return str(message) if message not in (None, "") else "submitted"


def provider_status_is_completed(status: str) -> bool:
    return status.strip().lower() in COMPLETED_STATUSES


def provider_status_is_failed(status: str) -> bool:
    return status.strip().lower() in FAILED_STATUSES


def format_delivery_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return str(value)[:800]
    return str(value)[:800]


def extract_provider_delivery_items(response: dict) -> list[str]:
    for source in provider_delivery_sources(response):
        lines: list[str] = []
        for list_key in ACCOUNT_LIST_KEYS:
            items = source.get(list_key)
            if isinstance(items, list) and items:
                multiple = len(items) > 1
                for idx, item in enumerate(items, start=1):
                    if not isinstance(item, dict):
                        lines.append(f"- {format_delivery_value(item)}")
                        continue
                    if multiple:
                        lines.append(f"Account {idx}:")
                    for key, value in item.items():
                        if key in EXCLUDED_ACCOUNT_FIELDS:
                            continue
                        if value in (None, "", "string"):
                            continue
                        label = FIELD_LABELS.get(key, key.replace("_", " ").title())
                        lines.append(f"{label}: {format_delivery_value(value)}")
                if lines:
                    return lines

        delivery_keys = (
            "account",
            "accounts",
            "credentials",
            "credential",
            "email",
            "password",
            "code",
            "result",
            "content",
            "stock",
            "lines",
            "text",
            "info",
        )
        for key in delivery_keys:
            value = source.get(key)
            if value in (None, "", []):
                continue
            if isinstance(value, list):
                for item in value[:5]:
                    lines.append(f"- {format_delivery_value(item)}")
            elif isinstance(value, dict):
                if set(value.keys()) & STRUCTURAL_KEYS:
                    continue
                for inner_key, inner_value in list(value.items())[:8]:
                    if inner_value not in (None, "", []):
                        lines.append(f"- {inner_key}: {format_delivery_value(inner_value)}")
            else:
                lines.append(f"- {format_delivery_value(value)}")
        if lines:
            return lines
    return []


def provider_response_has_delivery(response: dict) -> bool:
    return bool(extract_provider_delivery_items(response))


def format_provider_delivery_note(response: dict) -> str:
    items = extract_provider_delivery_items(response)
    if items:
        return "Provider delivery:\n" + "\n".join(items)
    return ""


def merge_provider_responses(purchase_response: dict, detail_response: dict) -> dict:
    merged = dict(purchase_response)
    for key, value in detail_response.items():
        if value not in (None, "", []):
            merged[key] = value
    return merged
