"""Debug HappyScribe batch_actions export endpoint."""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

from media_publisher.config import load_settings
from media_publisher.sources.happyscribe_web import EDITOR_URL, _launch_persistent_context

TID = "a3d34114f0f34975b0499bc58845b299"
ORG = "3310225"


def main() -> None:
    settings = load_settings(Path(".").resolve())
    profile = Path(".") / settings.happyscribe_browser_profile

    with sync_playwright() as playwright:
        context = _launch_persistent_context(
            playwright,
            profile,
            browser_channel="chrome",
            headless=False,
            accept_downloads=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(
            EDITOR_URL.format(transcription_id=TID),
            wait_until="domcontentloaded",
            timeout=180_000,
        )
        page.wait_for_function(
            "() => typeof window.openExportModal === 'function'",
            timeout=180_000,
        )

        attempts = [
            (
                "export",
                "export-modal-content",
                f"https://www.happyscribe.com/v2/{ORG}/batch_actions",
                {
                    "batch_action_name": "export",
                    "transcription_hashed_ids[]": TID,
                },
            ),
            (
                "video-export-new",
                "video_export_modal_content",
                f"https://www.happyscribe.com/transcriptions/{TID}/exports/new",
                None,
            ),
            (
                "video-export-operation",
                "export_modal",
                f"https://www.happyscribe.com/transcriptions/{TID}/exports/new?operation=video_export",
                None,
            ),
        ]
        out_dir = Path("downloads/happyscribe")
        out_dir.mkdir(parents=True, exist_ok=True)
        for label, frame, url, payload in attempts:
            if payload is None:
                response = page.request.get(
                    url,
                    headers={
                        "Turbo-Frame": frame,
                        "Accept": "text/vnd.turbo-stream.html, text/html, application/xhtml+xml",
                    },
                )
            else:
                token = page.locator(
                    '#export-modal-load-form input[name="authenticity_token"]'
                ).first.get_attribute("value")
                assert token
                response = page.request.post(
                    url,
                    form={"authenticity_token": token, **payload},
                    headers={
                        "Turbo-Frame": frame,
                        "Accept": "text/vnd.turbo-stream.html, text/html, application/xhtml+xml",
                    },
                )
            body = response.text()
            path = out_dir / f"debug-batch-{label}.html"
            path.write_text(body, encoding="utf-8")
            print(label, response.status, len(body), "->", path)
            lowered = body.lower()
            for keyword in ("video", "hardcod", "mp4", "export", "download"):
                if keyword in lowered:
                    print(" ", keyword, "found")

        context.close()


if __name__ == "__main__":
    main()
