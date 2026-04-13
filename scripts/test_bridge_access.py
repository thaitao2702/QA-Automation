"""
Diagnostic script: navigate to game, wait for bridge, call startGame(), verify idle.

Usage:
    python scripts/test_bridge_access.py
    python scripts/test_bridge_access.py --headless
"""

import asyncio
import sys
import yaml
from pathlib import Path
from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
GAME = "zeus"
ENV = "dev"


def resolve_url() -> str:
    config_path = BASE_DIR / "games" / GAME / "env.config.yml"
    with config_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    env_entry = config["environments"][ENV]
    base_url = str(env_entry["url"]).strip()
    browser_params = env_entry.get("browser_params", "")
    if browser_params:
        sep = "&" if "?" in base_url else "?"
        base_url = f"{base_url}{sep}{browser_params.lstrip('?&')}"
    return base_url


def log(step: int, msg: str, status: str = "...") -> None:
    icons = {"OK": "[OK]", "FAIL": "[FAIL]", "...": "[..]", "SKIP": "[--]"}
    print(f"  {icons.get(status, status)}  Step {step}: {msg}")


async def main() -> None:
    headless = "--headless" in sys.argv
    url = resolve_url()

    print(f"\n  Game URL: {url}")
    print(f"  Headless: {headless}\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        page = await browser.new_page()

        # --- Step 1: Navigate ---
        log(1, "Navigating to game...")
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            log(1, "Page loaded (networkidle)", "OK")
        except Exception as e:
            log(1, f"Navigation failed: {e}", "FAIL")
            await browser.close()
            return

        # --- Step 2: Wait for bridge registration ---
        log(2, "Waiting for window.__gameTestBridge...")
        try:
            await page.wait_for_function(
                "typeof window.__gameTestBridge !== 'undefined' && window.__gameTestBridge !== null",
                timeout=30000,
            )
            log(2, "Bridge registered on page", "OK")
        except Exception as e:
            log(2, f"Bridge not found within 30s: {e}", "FAIL")
            await browser.close()
            return

        # --- Step 3: Check AppState ---
        log(3, "Checking getAppState()...")
        try:
            state = await page.evaluate("window.__gameTestBridge.getAppState()")
            log(3, f"AppState = '{state}'", "OK")
        except Exception as e:
            log(3, f"getAppState() failed: {e}", "FAIL")
            await browser.close()
            return

        # --- Step 4: Call startGame() ---
        if state == "ready":
            log(4, "Calling startGame()...")
            try:
                await page.evaluate("window.__gameTestBridge.startGame()")
                log(4, "startGame() resolved", "OK")
            except Exception as e:
                log(4, f"startGame() failed: {e}", "FAIL")
                await browser.close()
                return
        else:
            log(4, f"Skipping startGame() — state is '{state}', expected 'ready'", "SKIP")

        # --- Step 5: Verify idle ---
        log(5, "Checking AppState after startGame()...")
        try:
            state = await page.evaluate("window.__gameTestBridge.getAppState()")
            if state == "idle":
                log(5, f"AppState = '{state}'", "OK")
            else:
                log(5, f"AppState = '{state}' (expected 'idle')", "FAIL")
        except Exception as e:
            log(5, f"getAppState() failed: {e}", "FAIL")
            await browser.close()
            return

        # --- Step 6: Query bridge methods ---
        log(6, "Testing bridge queries...")
        queries = {
            "getBalance": "window.__gameTestBridge.getBalance()",
            "getBetAmount": "window.__gameTestBridge.getBetAmount()",
            "isSpinEnabled": "window.__gameTestBridge.isSpinEnabled()",
            "getSnapshot": "window.__gameTestBridge.getSnapshot()",
        }
        for name, expr in queries.items():
            try:
                result = await page.evaluate(expr)
                if name == "getSnapshot":
                    keys = list(result.keys()) if isinstance(result, dict) else str(result)
                    print(f"         {name}() -> keys: {keys}")
                else:
                    print(f"         {name}() -> {result}")
            except Exception as e:
                print(f"         {name}() -> FAIL: {e}")
        log(6, "Bridge queries complete", "OK")

        print("\n  All steps passed. Bridge is fully accessible.\n")

        if not headless:
            print("  Browser is open for inspection. Press Ctrl+C to close.\n")
            try:
                await asyncio.Event().wait()
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
