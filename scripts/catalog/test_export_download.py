from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

DOCUMENT_ID = "a0fd45dde0412b2274eff76b"
BULGARIAN_LANGUAGE_ID = 1026
UI_BASE = "https://ea.smartcat.com"
STATE = Path(__file__).resolve().parents[1] / "smartcat-state.json"


def main() -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(STATE))
        request = context.request

        for export_type, label in [(0, "source?"), (1, "target?"), (2, "bilingual?")]:
            params = urlencode(
                {
                    "type": export_type,
                    "destination": 0,
                    "segmentExportMode": 0,
                    "withTags": "false",
                    "documentExportRequestSource": 0,
                }
            )
            body = [{"documentId": DOCUMENT_ID, "languageId": BULGARIAN_LANGUAGE_ID}]
            create = request.post(
                f"{UI_BASE}/api/Documents/ExportTasks?{params}",
                data=json.dumps(body),
                headers={"Content-Type": "application/json"},
            )
            task_id = json.loads(create.text())
            print("type", export_type, label, "task", task_id)
            for _ in range(30):
                download = request.get(f"{UI_BASE}/api/Documents/Download/{task_id}")
                print("  poll", download.status, download.headers.get("content-type"), len(download.body() or b""))
                if download.status == 200 and download.body():
                    snippet = download.body()[:300]
                    print("  body", snippet)
                    break
                time.sleep(1)
            print("---")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
