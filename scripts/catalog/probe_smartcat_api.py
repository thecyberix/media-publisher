from __future__ import annotations

import json
import re
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
        request = context.request
        captured: list[tuple[str, int, str]] = []

        def on_response(response):
            url = response.url
            if "/api/" not in url:
                return
            try:
                body = response.text()[:500]
            except Exception:
                body = ""
            captured.append((url, response.status, body))

        page = context.new_page()
        page.on("response", on_response)
        page.goto(
            f"{UI_BASE}/projects/{PROJECT_ID}/files?folderMode=true&search={SEARCH.replace(' ', '%20')}",
            wait_until="commit",
            timeout=120000,
        )
        page.wait_for_timeout(15000)

        print("=== CAPTURED API RESPONSES ===")
        for url, status, body in captured:
            if status != 200:
                continue
            if any(k in url.lower() for k in ("project", "document", "file", "drive", "export", "translat")):
                print(status, url)
                print(body[:300])
                print("---")

        candidates = [
            f"/api/integration/v1/project/{PROJECT_ID}",
            f"/api/projects/{PROJECT_ID}",
            f"/api/projects/{PROJECT_ID}/documents",
            f"/api/projects/{PROJECT_ID}/files",
            f"/api/project/{PROJECT_ID}",
            f"/api/project/{PROJECT_ID}/documents",
            f"/api/v1/projects/{PROJECT_ID}",
            f"/api/v1/projects/{PROJECT_ID}/documents",
            f"/api/drive/projects/{PROJECT_ID}/files",
        ]
        print("=== DIRECT PROBES ===")
        for path in candidates:
            resp = request.get(f"{UI_BASE}{path}")
            print(resp.status, path, resp.text()[:200])

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
