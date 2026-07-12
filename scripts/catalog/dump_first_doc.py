from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ID = "d1b6348b-541f-473a-9583-2a03d5315fef"
SEARCH = "In Any Arena Of Life No One Has Done Anything Truly Worthwhile Without Being Devoted To What They A"
UI_BASE = "https://ea.smartcat.com"
STATE = Path(__file__).resolve().parents[1] / "smartcat-state.json"


def main() -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(STATE))
        request = context.request
        file_items = request.post(
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
        ).json()
        page_content = request.post(
            f"{UI_BASE}/api/Projects/{PROJECT_ID}/PageContent",
            data=json.dumps({"filter": None, "fileItems": file_items, "loadPreviews": False}),
            headers={"Content-Type": "application/json"},
        ).json()
        for doc in page_content["documents"].values():
            print(json.dumps(doc, indent=2))
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
