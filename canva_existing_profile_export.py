"""
FINAL Canva session exporter using your REAL existing Chrome profile.

IMPORTANT:
1) Close ALL Chrome windows before running this script.
2) Run:
      python canva_existing_profile_export.py
3) Choose the Chrome profile where Canva Education is already logged in.
4) The script opens that SAME real Chrome profile.
5) Open/confirm Canva Education if needed, then press ENTER in CMD.
6) Keep CANVA_STORAGE_STATE_B64 private.
"""

import asyncio
import base64
import json
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

LOCALAPPDATA = os.environ.get("LOCALAPPDATA")
if not LOCALAPPDATA:
    raise SystemExit("LOCALAPPDATA was not found.")

USER_DATA = Path(LOCALAPPDATA) / "Google" / "Chrome" / "User Data"
LOCAL_STATE = USER_DATA / "Local State"
OUT = Path("canva-session.json")


def load_profile_names():
    names = {}
    if LOCAL_STATE.exists():
        try:
            data = json.loads(LOCAL_STATE.read_text(encoding="utf-8"))
            cache = data.get("profile", {}).get("info_cache", {})
            for folder, meta in cache.items():
                names[folder] = meta.get("name") or folder
        except Exception:
            pass
    return names


def discover_profiles():
    names = load_profile_names()
    candidates = []
    for p in USER_DATA.iterdir():
        if not p.is_dir():
            continue
        if p.name == "Default" or p.name.startswith("Profile "):
            candidates.append((p.name, names.get(p.name, p.name)))
    candidates.sort(key=lambda x: (x[0] != "Default", x[0]))
    return candidates


async def main():
    if not USER_DATA.exists():
        raise RuntimeError(f"Chrome User Data folder not found: {USER_DATA}")

    profiles = discover_profiles()
    if not profiles:
        raise RuntimeError("No Chrome profiles were found.")

    print("\nChrome profiles found:\n")
    for i, (folder, name) in enumerate(profiles, 1):
        print(f"  {i}. {name}   [{folder}]")

    while True:
        choice = input("\nChoose the profile number where Canva is already logged in: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(profiles):
            break
        print("Please enter a valid number.")

    profile_folder, profile_name = profiles[int(choice) - 1]

    print(f"\nSelected: {profile_name} [{profile_folder}]")
    print("\nIMPORTANT: ALL Chrome windows must be closed now.")
    input("After closing Chrome completely, press ENTER to continue... ")

    async with async_playwright() as p:
        print("\nOpening your REAL Google Chrome profile...")

        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA),
            channel="chrome",
            headless=False,
            args=[
                f"--profile-directory={profile_folder}",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )

        pages = context.pages
        page = pages[0] if pages else await context.new_page()

        # Open Canva without forcing the login page.
        await page.goto("https://www.canva.com/", wait_until="domcontentloaded", timeout=60000)

        print("\nChrome opened.")
        print("If Canva is already logged in, open your Education team in this SAME window.")
        print("Do NOT log out.")
        input("When your Canva Education account/team is visibly open, press ENTER here... ")

        # Verify we are not clearly on a login page.
        current_url = page.url.lower()
        if "/login" in current_url:
            await context.close()
            raise RuntimeError(
                "This selected Chrome profile is not logged in to Canva. "
                "Run the script again and choose the correct Chrome profile."
            )

        state = await context.storage_state()
        raw = json.dumps(state, ensure_ascii=False).encode("utf-8")
        OUT.write_bytes(raw)
        encoded = base64.b64encode(raw).decode("ascii")

        print("\nSUCCESS - Canva session exported.\n")
        print("KEEP THIS VALUE PRIVATE. DO NOT SEND IT IN CHAT OR SCREENSHOTS.\n")
        print("CANVA_STORAGE_STATE_B64=" + encoded)
        print("\nA local canva-session.json was also created.")
        print("After copying the value, you may close Chrome.")

        # Wait so the printed value stays available.
        input("\nPress ENTER after you have safely copied the value... ")
        await context.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
    except Exception as exc:
        print("\nERROR:", exc)
        print(
            "\nIf the error says the profile is in use, close Chrome completely "
            "(including background Chrome processes) and run again."
        )
        sys.exit(1)
