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
        captured: list[tuple[str, str, dict, str]] = []

        def on_request(request):
            if "PageContent" in request.url or "FileItemIds" in request.url:
                body = request.post_data or ""
                captured.append((request.method, request.url, dict(request.headers), body))

        def on_response(response):
            if "PageContent" in response.url:
                try:
                    body = response.text()
                except Exception:
                    body = ""
                print("=== RESPONSE ===")
                print(response.status, response.url)
                print(body[:12000])

        page = context.new_page()
        page.on("request", on_request)
        page.on("response", on_response)
        page.goto(
            f"{UI_BASE}/projects/{PROJECT_ID}/files?folderMode=true&search={SEARCH.replace(' ', '%20')}",
            wait_until="commit",
            timeout=120000,
        )
        page.wait_for_timeout(15000)

        print("=== REQUESTS ===")
        for method, url, headers, body in captured:
            print(method, url)
            print("body", body[:2000])
            print("---")

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
