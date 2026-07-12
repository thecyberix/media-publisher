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
        request = context.request
        query = f"search={SEARCH.replace(' ', '%20')}&folderMode=true"
        resp = request.get(f"{UI_BASE}/api/Projects/{PROJECT_ID}/PageContent?{query}")
        print("status", resp.status)
        data = resp.json()
        print("top keys", list(data.keys()))
        documents = data.get("documents") or {}
        for document in documents.values():
            print("DOC", document.get("name"))
            print(json.dumps(document, indent=2)[:8000])
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
