import os

import httpx


async def dispatch_webhook(url: str, event: str, data: dict) -> bool:
    timeout = float(os.getenv("API_TIMEOUT", "30"))
    payload = {"event": event, "data": data}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
            return 200 <= response.status_code < 300
    except httpx.HTTPError:
        return False
