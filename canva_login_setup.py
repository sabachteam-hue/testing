"""Export an existing logged-in Google Chrome Canva session for Railway.

Windows usage:
  1) Log in to Canva in your normal Google Chrome first.
  2) CLOSE ALL Google Chrome windows completely.
  3) Run: python canva_login_setup.py

The script makes a temporary COPY of your Chrome profile, opens that copy,
and exports Playwright storage state. It does not automate or bypass Canva login,
2FA, CAPTCHA, or security checks.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

OUT = Path("canva-session.json")
CANVA_URL = "https://www.canva.com/"


CACHE_NAMES = {
    "Cache",
    "Code Cache",
    "GPUCache",
    "GrShaderCache",
    "ShaderCache",
    "DawnCache",
    "GraphiteDawnCache",
    "Media Cache",
    "Crashpad",
    "BrowserMetrics",
}


def chrome_user_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("LOCALAPPDATA is not available. This helper is intended for Windows.")
    return Path(local) / "Google" / "Chrome" / "User Data"


def chrome_is_running() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return "chrome.exe" in result.stdout.lower()
    except Exception:
        return False


def load_local_state(user_data: Path) -> dict:
    path = user_data / "Local State"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def available_profiles(user_data: Path, local_state: dict) -> tuple[list[str], str]:
    profile_cfg = local_state.get("profile", {}) if isinstance(local_state, dict) else {}
    info_cache = profile_cfg.get("info_cache", {}) if isinstance(profile_cfg, dict) else {}
    last_used = profile_cfg.get("last_used", "Default") if isinstance(profile_cfg, dict) else "Default"

    found: list[str] = []
    for name in info_cache.keys() if isinstance(info_cache, dict) else []:
        if (user_data / name).is_dir():
            found.append(name)

    # Fallback scan for Chrome profile folders.
    for p in user_data.iterdir() if user_data.exists() else []:
        if p.is_dir() and (p.name == "Default" or p.name.startswith("Profile ")):
            if p.name not in found:
                found.append(p.name)

    if not found:
        raise RuntimeError("No Google Chrome profile was found.")

    if last_used not in found:
        last_used = "Default" if "Default" in found else found[0]

    return found, last_used


def profile_display_name(profile_dir: str, local_state: dict) -> str:
    try:
        info = local_state["profile"]["info_cache"].get(profile_dir, {})
        friendly = info.get("name") or info.get("gaia_name")
        if friendly:
            return f"{profile_dir} ({friendly})"
    except Exception:
        pass
    return profile_dir


def choose_profile(profiles: list[str], last_used: str, local_state: dict) -> str:
    if len(profiles) == 1:
        return profiles[0]

    print("\nChrome profiles found:")
    for i, profile in enumerate(profiles, start=1):
        marker = "  <-- last used" if profile == last_used else ""
        print(f"  {i}. {profile_display_name(profile, local_state)}{marker}")

    default_index = profiles.index(last_used) + 1
    answer = input(f"\nChoose profile number [{default_index}]: ").strip()
    if not answer:
        return last_used
    try:
        idx = int(answer)
        if 1 <= idx <= len(profiles):
            return profiles[idx - 1]
    except ValueError:
        pass
    raise RuntimeError("Invalid profile selection.")


def ignore_cache_entries(_src: str, names: list[str]) -> set[str]:
    ignored = {name for name in names if name in CACHE_NAMES}
    # Lock files should never be copied from a profile.
    ignored.update({name for name in names if name.startswith("Singleton")})
    return ignored


def copy_profile(user_data: Path, profile_name: str, temp_root: Path) -> Path:
    copied_user_data = temp_root / "User Data"
    copied_user_data.mkdir(parents=True, exist_ok=True)

    # Chrome needs Local State to decrypt cookies on the same Windows user account.
    local_state = user_data / "Local State"
    if local_state.exists():
        shutil.copy2(local_state, copied_user_data / "Local State")

    src_profile = user_data / profile_name
    dst_profile = copied_user_data / profile_name
    print("\nCopying the selected Chrome profile to a temporary folder...")
    shutil.copytree(
        src_profile,
        dst_profile,
        ignore=ignore_cache_entries,
        dirs_exist_ok=False,
    )
    return copied_user_data


async def export_session(copied_user_data: Path, profile_name: str) -> None:
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(copied_user_data),
            channel="chrome",
            headless=False,
            args=[f"--profile-directory={profile_name}"],
        )

        pages = context.pages
        page = pages[0] if pages else await context.new_page()
        await page.goto(CANVA_URL, wait_until="domcontentloaded", timeout=120_000)

        print("\nA temporary Chrome window has opened.")
        print("Check that Canva is already logged in with the correct Education admin account.")
        print("Open your Canva Education team in THAT temporary Chrome window.")
        print("If Canva asks you to log in again, stop here with Ctrl+C; do not bypass any security check.")
        input("\nWhen the correct Education team is open, return here and press ENTER... ")

        # Newer Playwright versions can include IndexedDB in storage state. Fall back if unavailable.
        try:
            await context.storage_state(path=str(OUT), indexed_db=True)
        except TypeError:
            await context.storage_state(path=str(OUT))

        await context.close()


def print_secret() -> None:
    encoded = base64.b64encode(OUT.read_bytes()).decode("ascii")
    print("\nSUCCESS - Canva session exported.\n")
    print("Add this Railway variable and KEEP IT SECRET:\n")
    print("CANVA_STORAGE_STATE_B64=" + encoded)
    print("\nDo NOT send this value in screenshots or chat.")
    print("After adding it to Railway, you may delete canva-session.json from this computer.")


def main() -> None:
    if os.name != "nt":
        raise RuntimeError("This version is designed to import a local Google Chrome profile on Windows.")

    user_data = chrome_user_data_dir()
    if not user_data.exists():
        raise RuntimeError(f"Google Chrome user data was not found at: {user_data}")

    if chrome_is_running():
        print("\nGoogle Chrome is still running.")
        print("Please CLOSE ALL normal Chrome windows completely, then run this command again:")
        print("  python canva_login_setup.py")
        print("\nDo not end Chrome from Task Manager while you have unsaved work.")
        sys.exit(2)

    local_state = load_local_state(user_data)
    profiles, last_used = available_profiles(user_data, local_state)
    profile_name = choose_profile(profiles, last_used, local_state)
    print(f"\nUsing Chrome profile: {profile_display_name(profile_name, local_state)}")

    with tempfile.TemporaryDirectory(prefix="canva-chrome-copy-") as tmp:
        copied_user_data = copy_profile(user_data, profile_name, Path(tmp))
        asyncio.run(export_session(copied_user_data, profile_name))

    print_secret()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled. No Railway session variable was generated.")
    except Exception as exc:
        print(f"\nERROR: {exc}")
        print("If this persists, share only the error text - never share Canva cookies/session output.")
        sys.exit(1)
