from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ID = "d1b6348b-541f-473a-9583-2a03d5315fef"
DOCUMENT_ID = "a0fd45dde0412b2274eff76b"
BULGARIAN_LANGUAGE_ID = 1026
UI_BASE = "https://ea.smartcat.com"
STATE = Path(__file__).resolve().parents[1] / "smartcat-state.json"


def main() -> int:
    from playwright.sync_api import sync_playwright

    probes = [
        ("GET", f"/api/Documents/Download/{DOCUMENT_ID}_{BULGARIAN_LANGUAGE_ID}", None),
        ("GET", f"/api/Documents/Download/{DOCUMENT_ID}", None),
        (
            "POST",
            "/api/Documents/ExportTasks",
            {
                "documentIds": [f"{DOCUMENT_ID}_{BULGARIAN_LANGUAGE_ID}"],
                "type": "target",
                "mode": "current",
            },
        ),
        (
            "POST",
            "/api/Documents/ExportTasks/documentLevelStage",
            {
                "documentIds": [DOCUMENT_ID],
                "languageIds": [BULGARIAN_LANGUAGE_ID],
                "type": "target",
                "mode": "current",
            },
        ),
        (
            "GET",
            f"/api/Documents/{DOCUMENT_ID}/Targets",
            None,
        ),
        (
            "POST",
            "/api/Documents/Targets",
            {"documentId": DOCUMENT_ID, "languageId": BULGARIAN_LANGUAGE_ID},
        ),
        (
            "POST",
            "/api/Documents/Targets",
            [DOCUMENT_ID],
        ),
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(STATE))
        request = context.request
        for method, path, body in probes:
            url = f"{UI_BASE}{path}"
            if method == "GET":
                resp = request.get(url)
            else:
                resp = request.post(url, data=json.dumps(body) if body else None, headers={"Content-Type": "application/json"})
            text = resp.text()[:500]
            print(method, path, "->", resp.status)
            print(text)
            print("---")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
