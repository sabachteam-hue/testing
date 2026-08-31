"""One-time local helper: login to Canva manually and print storage-state Base64.
Run on a computer with a screen: python canva_login_setup.py
"""
import asyncio
import base64
from pathlib import Path
from playwright.async_api import async_playwright

OUT = Path("canva-session.json")

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://www.canva.com/login")
        input("Log in to Canva fully, open your Education team, then press ENTER here... ")
        await context.storage_state(path=str(OUT))
        await browser.close()
    encoded = base64.b64encode(OUT.read_bytes()).decode("ascii")
    print("\nAdd this Railway variable (keep it secret):\n")
    print("CANVA_STORAGE_STATE_B64=" + encoded)
    print("\nThen delete canva-session.json from your computer if you do not need it.")

asyncio.run(main())
