from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlencode

DOCUMENT_ID = "a0fd45dde0412b2274eff76b"
BULGARIAN_LANGUAGE_ID = 1026
UI_BASE = "https://ea.smartcat.com"
STATE = Path(__file__).resolve().parents[1] / "smartcat-state.json"

BODY_VARIANTS = [
    [{"documentId": DOCUMENT_ID, "languageId": BULGARIAN_LANGUAGE_ID}],
    [{"documentId": DOCUMENT_ID, "targetLanguageId": BULGARIAN_LANGUAGE_ID}],
    [{"id": DOCUMENT_ID, "languageId": BULGARIAN_LANGUAGE_ID}],
    [f"{DOCUMENT_ID}_{BULGARIAN_LANGUAGE_ID}"],
]


def main() -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(STATE))
        request = context.request

        for body in BODY_VARIANTS:
            for export_type in range(0, 4):
                for destination in range(0, 4):
                    params = urlencode(
                        {
                            "type": export_type,
                            "destination": destination,
                            "segmentExportMode": 0,
                            "withTags": "false",
                            "documentExportRequestSource": 0,
                        }
                    )
                    resp = request.post(
                        f"{UI_BASE}/api/Documents/ExportTasks?{params}",
                        data=json.dumps(body),
                        headers={"Content-Type": "application/json"},
                    )
                    text = resp.text()[:300]
                    if resp.status not in (400, 404, 405):
                        print("HIT", resp.status, "type", export_type, "dest", destination)
                        print("body variant", body)
                        print(text)
                        print("---")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
