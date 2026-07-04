from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import sync_playwright

from media_publisher.config import load_settings
from media_publisher.sources.happyscribe_web import (
    EDITOR_URL,
    _launch_context_from_session,
)

TID = "a3d34114f0f34975b0499bc58845b299"


def main() -> None:
    settings = load_settings(Path(".").resolve())
    browser_state = Path(".") / settings.happyscribe_browser_state

    with sync_playwright() as playwright:
        browser, context = _launch_context_from_session(
            playwright,
            browser_state,
            browser_channel="chrome",
            headless=False,
            accept_downloads=True,
        )
        page = context.new_page()
        page.goto(
            EDITOR_URL.format(transcription_id=TID),
            wait_until="domcontentloaded",
            timeout=180_000,
        )
        page.wait_for_function(
            "() => typeof window.openExportModal === 'function'",
            timeout=180_000,
        )

        video_step = page.request.get(
            f"https://www.happyscribe.com/transcriptions/{TID}/exports/new?operation=video_export",
            headers={
                "Turbo-Frame": "video_export_modal_content",
                "Accept": "text/html, application/xhtml+xml",
            },
        )
        html = video_step.text()
        token_match = re.search(r'name="authenticity_token" value="([^"]+)"', html)
        assert token_match, "missing csrf token"
        token = token_match.group(1)

        response = page.request.post(
            f"https://www.happyscribe.com/transcriptions/{TID}/exports/hardcode_subtitles",
            form={"authenticity_token": token},
            headers={
                "Turbo-Frame": "video_export_modal_content",
                "Accept": "text/vnd.turbo-stream.html, text/html, application/xhtml+xml",
            },
        )
        print("post status", response.status)
        body = response.text()
        out = Path("downloads/happyscribe/debug-hardcode-response.html")
        out.write_text(body, encoding="utf-8")
        print("saved response", out, len(body))
        for pattern in (r'https?://[^"\']+\.mp4[^"\']*', r'href="([^"]+)"', r'data-download-url="([^"]+)"'):
            matches = re.findall(pattern, body)
            if matches:
                print("matches", matches[:5])
        context.close()
        browser.close()


if __name__ == "__main__":
    main()
