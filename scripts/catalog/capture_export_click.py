from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ID = "d1b6348b-541f-473a-9583-2a03d5315fef"
SEARCH = "What Old Bread Does To Your Body"
UI_BASE = "https://ea.smartcat.com"
STATE = Path(__file__).resolve().parents[1] / "smartcat-state.json"


def main() -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(STATE))
        captured: list[tuple[str, str, str]] = []

        def on_request(request):
            url = request.url
            if any(k in url for k in ("ExportTasks", "Download/", "Targets", "translate")):
                captured.append((request.method, url, request.post_data or ""))

        page = context.new_page()
        page.on("request", on_request)
        page.goto(
            f"{UI_BASE}/projects/{PROJECT_ID}/files?folderMode=true&search={SEARCH.replace(' ', '%20')}",
            wait_until="commit",
            timeout=120000,
        )
        page.wait_for_timeout(8000)

        # Try clicking Bulgarian-related UI elements
        for label in ("Bulgarian", "BG", "bg"):
            loc = page.get_by_text(label, exact=False)
            if loc.count():
                print("found label", label, loc.count())
                try:
                    loc.first.click(timeout=3000)
                    page.wait_for_timeout(2000)
                except Exception as exc:
                    print("click failed", exc)

        for label in ("Download", "Export", ".srt", "SRT"):
            loc = page.locator("button, a, [role='button']").filter(has_text=label)
            if loc.count():
                print("found action", label, loc.count())
                try:
                    loc.first.click(timeout=3000)
                    page.wait_for_timeout(3000)
                except Exception as exc:
                    print("action click failed", exc)

        print("=== CAPTURED ===")
        for method, url, body in captured:
            print(method, url)
            if body:
                print(body[:2000])
            print("---")

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
