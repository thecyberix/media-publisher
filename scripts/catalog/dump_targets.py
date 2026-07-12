from __future__ import annotations

import json
import sys
from pathlib import Path

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
        resp = request.post(
            f"{UI_BASE}/api/Documents/Targets",
            data=json.dumps([DOCUMENT_ID]),
            headers={"Content-Type": "application/json"},
        )
        print("status", resp.status)
        text = resp.text()
        print("content-type", resp.headers.get("content-type"))
        print(text[:12000])
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
