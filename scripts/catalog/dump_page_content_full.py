from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ID = "d1b6348b-541f-473a-9583-2a03d5315fef"
SEARCH = "What Old Bread Does To Your Body"
DOCUMENT_ID = "a0fd45dde0412b2274eff76b"
UI_BASE = "https://ea.smartcat.com"
STATE = Path(__file__).resolve().parents[1] / "smartcat-state.json"
OUT = Path(__file__).resolve().parents[1] / "output" / "page_content_sample.json"


def main() -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(STATE))
        request = context.request
        file_items_resp = request.post(
            f"{UI_BASE}/api/Projects/{PROJECT_ID}/FileItemIds",
            data=json.dumps(
                {
                    "isFolderMode": True,
                    "orderBy": 0,
                    "desc": False,
                    "filter": {
                        "searchName": SEARCH,
                        "createdByAccountUserIds": [],
                        "targetLanguageIds": [],
                        "documentTargetStatuses": [],
                        "stageNumbersWithNoAssignments": [],
                        "stageNumbersWithIncompleteState": [],
                        "creationDateFrom": None,
                        "creationDateTo": None,
                    },
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        file_items = file_items_resp.json()
        page_content_resp = request.post(
            f"{UI_BASE}/api/Projects/{PROJECT_ID}/PageContent",
            data=json.dumps(
                {
                    "filter": None,
                    "fileItems": file_items,
                    "loadPreviews": False,
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        data = page_content_resp.json()
        OUT.write_text(json.dumps(data, indent=2), encoding="utf-8")
        doc = data["documents"][DOCUMENT_ID]
        print("keys", sorted(doc.keys()))
        for key in sorted(doc.keys()):
            value = doc[key]
            if isinstance(value, (dict, list)):
                snippet = json.dumps(value)[:200]
            else:
                snippet = repr(value)
            print(key, "->", snippet)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
