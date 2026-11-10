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
        # The empty ticket shows the controls (demo has invented quotes if you want a filled one).
        page.locator("#ticket").screenshot(path=str(OUT / "screenshot-ticket.png"))
        # The spread ticket on the invented AAPL chain: a 225/220 bull put spread about a week out
        page.click("#t-asset [data-v='SPREAD']")
        page.fill("#sp-und", "AAPL")
        page.press("#sp-und", "Enter")
        page.wait_for_selector("#sp-exp [data-exp]", timeout=10000)
        page.locator("#sp-exp [data-exp]").nth(4).click()
        page.wait_for_selector("#sp-chain tr.row[data-sym$='P00225000']", timeout=10000)
        page.click("#sp-chain tr.row[data-sym$='P00225000']")
        page.wait_for_timeout(800)
        page.locator("#sp-width button").nth(1).click()
        page.wait_for_selector("#sp-why div", timeout=10000)
        page.wait_for_timeout(1500)
        page.locator("#ticket").screenshot(path=str(OUT / "screenshot-spread-ticket.png"))
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
