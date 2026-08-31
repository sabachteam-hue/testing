"""Export Canva login state from an already-running Chrome started with --remote-debugging-port=9222.

1) Close Chrome.
2) Start your real Chrome with:
   "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
3) Select the Chrome profile where Canva is already logged in and open canva.com.
4) Run:
   python canva_cdp_export.py

Keep the Base64 value secret.
"""
import asyncio
import base64
import json
from pathlib import Path

from playwright.async_api import async_playwright

OUT = Path("canva-session.json")
CDP_URL = "http://127.0.0.1:9222"


async def main():
    async with async_playwright() as p:
        print("Connecting to your already-running Chrome...")
        browser = await p.chromium.connect_over_cdp(CDP_URL)

        contexts = browser.contexts
        if not contexts:
            raise RuntimeError("Chrome is reachable, but no browser context was found.")

        context = contexts[0]

        canva_pages = []
        for page in context.pages:
            try:
                if "canva.com" in page.url.lower():
                    canva_pages.append(page)
            except Exception:
                pass

        if not canva_pages:
            raise RuntimeError(
                "Connected to Chrome, but no Canva tab was found. "
                "Open canva.com in this same Chrome profile and run the script again."
            )

        page = canva_pages[0]
        print("Found Canva tab:", page.url)

        # A login URL is a strong signal that the selected Chrome profile is not authenticated.
        if "/login" in page.url.lower():
            raise RuntimeError(
                "The Canva tab is on the login page. Select/open the Chrome profile "
                "where Canva Education is already logged in, then retry."
            )

        state = await context.storage_state()
        OUT.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        encoded = base64.b64encode(
            json.dumps(state, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")

        print("\nSUCCESS - Canva browser session exported.")
        print("Keep the value below PRIVATE. Do not send it in chat or screenshots.\n")
        print("CANVA_STORAGE_STATE_B64=" + encoded)
        print("\nA local canva-session.json was also created.")
        print("You can close Chrome after copying the Base64 value.")

        # Disconnect Playwright without intentionally closing the user's Chrome.
        # CDP connection is released when the Playwright context exits.


if __name__ == "__main__":
    asyncio.run(main())
