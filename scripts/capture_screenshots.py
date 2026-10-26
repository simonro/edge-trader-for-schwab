"""Retake the README screenshots from the DEMO server (invented data, never a real account).

    python -m companion --demo          (in another terminal; COMPANION_PORT=8791 recommended)
    python scripts/capture_screenshots.py [port]

Writes docs/screenshot-*.png. Open every image afterwards and check it shows the right content.
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "docs"
PORT = sys.argv[1] if len(sys.argv) > 1 else "8770"
BASE = f"http://127.0.0.1:{PORT}"


def main():
    OUT.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1680, "height": 1080}, device_scale_factor=2)
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_selector("text=Day trades", timeout=20000)
        page.wait_for_timeout(2000)
        page.click(".li[data-sel='NVDA']")          # the newest trade is focused by default; show NVDA
        page.wait_for_timeout(1500)
        page.screenshot(path=str(OUT / "screenshot-dashboard.png"))
        # The trail panel on the selected position (NVDA)
        page.click("button[data-trailopen='NVDA']")
        page.wait_for_timeout(1500)
        page.locator("#workspace").screenshot(path=str(OUT / "screenshot-workbench.png"))
        # A filled-in ticket is not possible in demo (no quotes); the empty ticket shows the controls.
        page.locator("#ticket").screenshot(path=str(OUT / "screenshot-ticket.png"))
        # The floating Manage window view
        m = browser.new_page(viewport={"width": 560, "height": 620}, device_scale_factor=2)
        m.goto(BASE + "/?view=manage", wait_until="networkidle")
        m.wait_for_timeout(2500)
        m.screenshot(path=str(OUT / "screenshot-manage-window.png"))
        browser.close()
    for p in sorted(OUT.glob("screenshot-*.png")):
        print("wrote", p)


if __name__ == "__main__":
    main()
