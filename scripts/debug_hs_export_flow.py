from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

from media_publisher.config import load_settings
from media_publisher.sources.happyscribe_web import (
    HappyScribeWebClient,
    _launch_context_from_session,
)

TID = "a3d34114f0f34975b0499bc58845b299"


def main() -> None:
    settings = load_settings(Path(".").resolve())
    browser_state = Path(".") / settings.happyscribe_browser_state
    destination = Path("downloads/happyscribe/test-export.mp4")

    with sync_playwright() as playwright:
        browser, context = _launch_context_from_session(
            playwright,
            browser_state,
            browser_channel="chrome",
            headless=False,
            accept_downloads=True,
        )
        page = context.new_page()
        client = HappyScribeWebClient(browser_state, headless=False)
        browser2, context2, page2 = client._open_editor_page(playwright, TID)
        client._load_video_export_form(page2, TID)
        client._start_hardcoded_video_export(page2, TID)
        print("started export, waiting for link...")
        link = client._wait_for_video_download_link(page2)
        print("link", link[:120])
        response = page2.request.get(link)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.body())
        print("saved", destination, destination.stat().st_size)
        context2.close()
        if browser2 is not None:
            browser2.close()
        context.close()
        browser.close()


if __name__ == "__main__":
    main()
