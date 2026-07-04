"""Temporary debug script for HappyScribe export modal."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

from media_publisher.config import load_settings
from media_publisher.sources.happyscribe_web import (
    EDITOR_URL,
    EXPORT_BUTTON_PATTERN,
    _launch_persistent_context,
)

TID = "a3d34114f0f34975b0499bc58845b299"


def main() -> None:
    settings = load_settings(Path(".").resolve())
    profile = Path(".") / settings.happyscribe_browser_profile
    events: list[tuple[int, str, str]] = []

    with sync_playwright() as playwright:
        context = _launch_persistent_context(
            playwright,
            profile,
            browser_channel="chrome",
            headless=False,
            accept_downloads=True,
        )
        page = context.pages[0] if context.pages else context.new_page()

        def on_response(response) -> None:
            url = response.url
            if any(keyword in url for keyword in ("batch", "export", "video", "hardcod")):
                events.append((response.status, response.request.method, url[:220]))

        page.on("response", on_response)
        page.goto(
            EDITOR_URL.format(transcription_id=TID),
            wait_until="domcontentloaded",
            timeout=180_000,
        )
        export_button = page.get_by_role("button", name=EXPORT_BUTTON_PATTERN)
        export_button.first.wait_for(state="visible", timeout=180_000)
        has_fn = page.evaluate("typeof window.openExportModal")
        print("openExportModal type:", has_fn)
        export_button.first.click()
        page.wait_for_timeout(2_000)
        print("export_modal hidden?", page.locator("#export_modal").get_attribute("class"))
        if "hidden" in (page.locator("#export_modal").get_attribute("class") or ""):
            page.evaluate("window.openExportModal && window.openExportModal()")
            page.wait_for_timeout(2_000)
            print("after js export_modal hidden?", page.locator("#export_modal").get_attribute("class"))
        page.screenshot(path="downloads/happyscribe/debug-before-modal.png", full_page=True)
        page.locator("#export_modal:not(.hidden)").wait_for(state="visible", timeout=30_000)

        export_modal = page.locator("#export_modal:not(.hidden) #export-modal-content")
        export_modal.wait_for(state="visible", timeout=120_000)
        export_modal.locator(".animate-spin").first.wait_for(state="hidden", timeout=120_000)
        page.wait_for_timeout(2_000)

        modal_html = export_modal.inner_html()
        out = Path("downloads/happyscribe/debug-export-loaded.html")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(modal_html, encoding="utf-8")
        print(f"saved {out} ({len(modal_html)} bytes)")

        for element in export_modal.locator(
            "button, a, label, [role='button']"
        ).all():
            try:
                text = element.inner_text(timeout=200).strip().replace("\n", " | ")
                if text:
                    print("OPT", repr(text[:160]), "visible", element.is_visible())
            except Exception:
                pass

        print("--- network ---")
        for event in events:
            print(event)

        context.close()


if __name__ == "__main__":
    main()
