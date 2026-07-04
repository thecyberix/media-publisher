from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from media_publisher.sources.happyscribe import (
    DEFAULT_API_BASE,
    DEFAULT_USER_AGENT,
    HappyScribeError,
    HappyScribeExport,
    _parse_export,
    burned_video_destination_path,
)

HARD_CODED_EXPORT_FORMAT = "hardcoded_subtitles"
HARD_CODED_WEB_EXPORT_PATH = "hardcode_subtitles"
WEB_EXPORT_POLL_INTERVAL_SECONDS = 5.0
WEB_EXPORT_POLL_MAX_ATTEMPTS = 360
SIGN_IN_URL = "https://www.happyscribe.com/users/sign_in"
EDITOR_URL = "https://www.happyscribe.com/transcriptions/{transcription_id}/subtitles"
EDITOR_READY_TIMEOUT_MS = 180_000
UI_ACTION_TIMEOUT_MS = 60_000
DOWNLOAD_TIMEOUT_MS = 900_000
DEFAULT_BROWSER_CHANNELS = ("chrome", "msedge", "chromium")
EXPORT_BUTTON_PATTERN = re.compile(r"export|download", re.I)
HARDCODED_OPTION_PATTERN = re.compile(
    r"hardcod|burn.*subtitle|subtitle.*video|video.*subtitle|mp4.*subtitle|embedded.*subtitle",
    re.I,
)
VIDEO_SUBTITLE_OPTION_PATTERN = re.compile(
    r"export video with subtitles|video with subtitles|hardcod|burn.*subtitle",
    re.I,
)
CONFIRM_BUTTON_PATTERN = re.compile(r"^export$|^download$|start export|create export", re.I)
EXPORT_VIDEO_BUTTON_PATTERN = re.compile(r"^export video$", re.I)


class HappyScribeWebError(RuntimeError):
    pass


@dataclass(frozen=True)
class HappyScribeWebSession:
    browser_state_path: Path


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise HappyScribeWebError(
            "Playwright is required for HappyScribe web export. "
            "Install it with: pip install -e \".[browser]\" && python -m playwright install chromium"
        ) from exc
    return sync_playwright


def _browser_channel_candidates(preferred: str | None) -> tuple[str | None, ...]:
    if preferred:
        normalized = preferred.strip().lower()
        if normalized in {"chrome", "msedge", "chromium"}:
            return (normalized, *(channel for channel in DEFAULT_BROWSER_CHANNELS if channel != normalized))
    return DEFAULT_BROWSER_CHANNELS


def _launch_persistent_context(
    playwright,
    profile_dir: Path,
    *,
    browser_channel: str | None,
    headless: bool,
    accept_downloads: bool = False,
):
    profile_dir.mkdir(parents=True, exist_ok=True)
    launch_kwargs = {
        "user_data_dir": str(profile_dir),
        "headless": headless,
        "accept_downloads": accept_downloads,
        "viewport": {"width": 1440, "height": 960},
        "locale": "en-US",
        "ignore_default_args": ["--enable-automation"],
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if browser_channel and browser_channel != "chromium":
        launch_kwargs["channel"] = browser_channel
    return playwright.chromium.launch_persistent_context(**launch_kwargs)


def _launch_context_from_session(
    playwright,
    browser_state_path: Path,
    *,
    browser_channel: str | None,
    headless: bool,
    accept_downloads: bool = False,
):
    launch_kwargs: dict[str, Any] = {"headless": headless}
    if browser_channel and browser_channel != "chromium":
        launch_kwargs["channel"] = browser_channel
    browser = playwright.chromium.launch(**launch_kwargs)
    context = browser.new_context(
        storage_state=str(browser_state_path),
        accept_downloads=accept_downloads,
        viewport={"width": 1440, "height": 960},
        locale="en-US",
    )
    return browser, context


def save_browser_session_interactive(
    browser_state_path: Path,
    *,
    browser_profile_dir: Path,
    email: str | None = None,
    password: str | None = None,
    browser_channel: str | None = "chrome",
) -> None:
    """Open HappyScribe in a real browser profile and persist the authenticated session."""
    sync_playwright = _require_playwright()
    browser_state_path.parent.mkdir(parents=True, exist_ok=True)

    last_error: Exception | None = None
    for channel in _browser_channel_candidates(browser_channel):
        label = channel or "bundled chromium"
        try:
            with sync_playwright() as playwright:
                context = _launch_persistent_context(
                    playwright,
                    browser_profile_dir,
                    browser_channel=channel,
                    headless=False,
                )
                if context.pages:
                    page = context.pages[0]
                else:
                    page = context.new_page()

                print(f"Opened HappyScribe login using {label}.")
                print(
                    "Tip: Google sign-in often fails in automated browsers. "
                    "Prefer HappyScribe email/password, or sign in to Google in your "
                    "normal browser first and reuse that Chrome profile."
                )
                page.goto(SIGN_IN_URL, wait_until="domcontentloaded")

                if email and password:
                    page.locator('input[type="email"]').fill(email)
                    page.locator('input[type="password"]').fill(password)
                    page.locator('button[type="submit"]').click()
                    page.wait_for_load_state("networkidle", timeout=120_000)
                else:
                    print("Log in to HappyScribe in the opened browser window.")
                    print("When the dashboard loads, press Enter here to save the session.")
                    input()

                context.storage_state(path=str(browser_state_path))
                context.close()
            return
        except Exception as exc:
            last_error = exc
            continue

    raise HappyScribeWebError(
        "Could not launch a browser for HappyScribe login. "
        "Install Google Chrome or Microsoft Edge, then retry. "
        f"Last error: {last_error}"
    ) from last_error


def import_browser_session(source_path: Path, destination_path: Path) -> None:
    if not source_path.exists():
        raise HappyScribeWebError(f"Session file not found: {source_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "cookies" not in payload:
        raise HappyScribeWebError(
            f"Invalid Playwright storage state file: {source_path}. "
            "Expected JSON with a top-level 'cookies' array."
        )
    destination_path.write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )


def _cookies_header(cookies: list[dict[str, Any]]) -> str:
    return "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies)


def _turbo_frame_inner_html(html: str) -> str:
    frame_match = re.search(
        r"<turbo-frame[^>]*>(.*)</turbo-frame>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if frame_match:
        return frame_match.group(1).strip()
    body_match = re.search(
        r"<body[^>]*>(.*)</body>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if body_match:
        return body_match.group(1).strip()
    return html.strip()


class HappyScribeWebClient:
    def __init__(
        self,
        browser_state_path: Path,
        *,
        api_base: str = DEFAULT_API_BASE,
        headless: bool = True,
        browser_channel: str | None = "chrome",
        browser_profile_dir: Path | None = None,
        api_key: str | None = None,
    ) -> None:
        self.browser_state_path = browser_state_path
        self.api_base = api_base.rstrip("/")
        self.headless = headless
        self.browser_channel = browser_channel
        self.browser_profile_dir = browser_profile_dir
        self.api_key = api_key.strip() if api_key else None
        if not browser_state_path.exists():
            raise HappyScribeWebError(
                f"HappyScribe browser session not found at {browser_state_path}. "
                "Run: python -m media_publisher --happyscribe-save-session"
            )

    def _url(self, path: str) -> str:
        return f"{self.api_base}/{path.lstrip('/')}"

    def _request_with_cookies(
        self,
        method: str,
        url: str,
        cookies: list[dict[str, Any]],
        *,
        body: dict[str, Any] | None = None,
    ) -> Any:
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Cookie", _cookies_header(cookies))
        request.add_header("Content-Type", "application/json")
        request.add_header("User-Agent", DEFAULT_USER_AGENT)
        request.add_header("Accept", "application/json")
        if self.api_key:
            request.add_header("Authorization", f"Bearer {self.api_key}")

        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise HappyScribeWebError(
                f"HappyScribe web {method} {url} failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise HappyScribeWebError(f"HappyScribe web request failed: {exc.reason}") from exc

        if not payload:
            return {}
        return json.loads(payload.decode("utf-8"))

    def _download_file(self, url: str, destination: Path, cookies: list[dict[str, Any]]) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, method="GET")
        request.add_header("Cookie", _cookies_header(cookies))
        request.add_header("User-Agent", DEFAULT_USER_AGENT)

        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                destination.write_bytes(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise HappyScribeWebError(
                f"Download failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise HappyScribeWebError(f"Download failed: {exc.reason}") from exc

        return destination

    def _create_hardcoded_export(
        self,
        transcription_id: str,
        cookies: list[dict[str, Any]],
    ) -> HappyScribeExport:
        response = self._request_with_cookies(
            "POST",
            self._url("exports"),
            cookies,
            body={
                "export": {
                    "format": HARD_CODED_EXPORT_FORMAT,
                    "transcription_ids": [transcription_id],
                }
            },
        )
        if not isinstance(response, dict):
            raise HappyScribeWebError("HappyScribe web export response is invalid")
        return _parse_export(response)

    def _wait_for_export(
        self,
        export_id: str,
        cookies: list[dict[str, Any]],
    ) -> HappyScribeExport:
        for _ in range(WEB_EXPORT_POLL_MAX_ATTEMPTS):
            response = self._request_with_cookies(
                "GET",
                self._url(f"exports/{export_id}"),
                cookies,
            )
            if not isinstance(response, dict):
                raise HappyScribeWebError("HappyScribe web export response is invalid")
            export = _parse_export(response)
            if export.state == "ready" and export.download_link:
                return export
            if export.state == "failed":
                raise HappyScribeWebError(
                    f"HappyScribe web export {export_id!r} failed (state={export.state!r})"
                )
            time.sleep(WEB_EXPORT_POLL_INTERVAL_SECONDS)

        raise HappyScribeWebError(
            f"HappyScribe web export {export_id!r} did not become ready in time"
        )

    def export_video_with_subtitles_api(
        self,
        transcription_id: str,
        destination: Path,
        cookies: list[dict[str, Any]],
    ) -> Path:
        export = self._create_hardcoded_export(transcription_id, cookies)
        if not export.download_link or export.state != "ready":
            export = self._wait_for_export(export.id, cookies)
        if not export.download_link:
            raise HappyScribeWebError(
                f"HappyScribe web export {export.id!r} did not return a download link"
            )
        return self._download_file(export.download_link, destination, cookies)

    def _wait_for_editor(self, page) -> None:
        if "sign_in" in page.url:
            raise HappyScribeWebError(
                "HappyScribe browser session is not authenticated. "
                "Run: python -m media_publisher --happyscribe-save-session"
            )

        candidates = [
            page.get_by_role("button", name=EXPORT_BUTTON_PATTERN),
            page.get_by_text(EXPORT_BUTTON_PATTERN),
        ]
        for locator in candidates:
            try:
                locator.first.wait_for(state="visible", timeout=EDITOR_READY_TIMEOUT_MS)
                break
            except Exception:
                continue
        else:
            if "sign_in" in page.url:
                raise HappyScribeWebError(
                    "HappyScribe browser session expired. "
                    "Run: python -m media_publisher --happyscribe-save-session"
                )
            raise HappyScribeWebError(
                "HappyScribe subtitle editor did not finish loading. "
                "Try again with a visible browser window."
            )

        page.wait_for_function(
            "() => typeof window.openExportModal === 'function'",
            timeout=EDITOR_READY_TIMEOUT_MS,
        )

    def _video_export_step_url(self, transcription_id: str) -> str:
        return (
            "https://www.happyscribe.com/transcriptions/"
            f"{transcription_id}/exports/new?operation=video_export"
        )

    def _load_video_export_form(self, page, transcription_id: str) -> None:
        response = page.request.get(
            self._video_export_step_url(transcription_id),
            headers={
                "Turbo-Frame": "video_export_modal_content",
                "Accept": "text/html, application/xhtml+xml",
            },
        )
        if response.status >= 400:
            raise HappyScribeWebError(
                "HappyScribe video export form request failed with "
                f"HTTP {response.status}: {response.text()[:300]}"
            )

        inner_html = _turbo_frame_inner_html(response.text())
        page.evaluate(
            """
            (innerHtml) => {
              const modal = document.getElementById('video_export_modal');
              const frame = document.getElementById('video_export_modal_content');
              if (modal) {
                modal.classList.remove('hidden');
              }
              if (frame) {
                frame.innerHTML = innerHtml;
              }
            }
            """,
            inner_html,
        )
        page.locator(
            '#video_export_modal_content form[action*="hardcode_subtitles"] button'
        ).wait_for(state="attached", timeout=UI_ACTION_TIMEOUT_MS)
        page.get_by_role("button", name=EXPORT_VIDEO_BUTTON_PATTERN).wait_for(
            state="visible",
            timeout=UI_ACTION_TIMEOUT_MS,
        )

    def _start_hardcoded_video_export(self, page, transcription_id: str) -> None:
        token = page.locator(
            '#video_export_modal_content input[name="authenticity_token"]'
        ).first.get_attribute("value")
        if not token:
            raise HappyScribeWebError(
                "HappyScribe video export form is missing an authenticity token."
            )

        response = page.request.post(
            f"https://www.happyscribe.com/transcriptions/"
            f"{transcription_id}/exports/{HARD_CODED_WEB_EXPORT_PATH}",
            form={"authenticity_token": token},
            headers={
                "Turbo-Frame": "video_export_modal_content",
                "Accept": "text/vnd.turbo-stream.html, text/html, application/xhtml+xml",
            },
        )
        if response.status >= 400:
            raise HappyScribeWebError(
                "HappyScribe hardcoded video export failed with "
                f"HTTP {response.status}: {response.text()[:300]}"
            )

        inner_html = _turbo_frame_inner_html(response.text())
        page.evaluate(
            """
            (innerHtml) => {
              const modal = document.getElementById('video_export_modal');
              const frame = document.getElementById('video_export_modal_content');
              if (modal) {
                modal.classList.remove('hidden');
              }
              if (frame) {
                frame.innerHTML = innerHtml;
              }
            }
            """,
            inner_html,
        )

    def _wait_for_video_download_link(self, page) -> str:
        link_handle = page.wait_for_function(
            """
            () => {
              const frame = document.getElementById('video_export_modal_content');
              if (!frame) {
                return false;
              }
              const progress = frame.querySelector('#hardcode-subtitles-progress');
              if (progress && /failed|error/i.test(progress.innerText)) {
                throw new Error(progress.innerText.trim());
              }
              const link = frame.querySelector(
                'a[href*="media.happyscribe"], a[href*=".mp4"], a[download]'
              );
              return link?.href || false;
            }
            """,
            timeout=DOWNLOAD_TIMEOUT_MS,
        )
        href = link_handle.json_value()
        if not isinstance(href, str) or not href:
            raise HappyScribeWebError(
                "HappyScribe video export finished without a download link."
            )
        return href

    def _save_debug_artifacts(self, page, label: str) -> None:
        debug_dir = Path("downloads/happyscribe")
        debug_dir.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(debug_dir / f"export-failure-{label}.png"), full_page=True)
        except Exception:
            pass

    def _open_editor_page(
        self,
        playwright,
        transcription_id: str,
        *,
        browser_channel: str | None = None,
    ):
        channel = browser_channel or self.browser_channel
        editor_url = EDITOR_URL.format(transcription_id=transcription_id)
        browser = None
        page = None
        profile_error: Exception | None = None

        try:
            browser, context = _launch_context_from_session(
                playwright,
                self.browser_state_path,
                browser_channel=channel,
                headless=self.headless,
                accept_downloads=True,
            )
            page = context.new_page()
        except Exception as exc:
            profile_error = exc
            context = None

        if page is None and self.browser_profile_dir and self.browser_profile_dir.exists():
            try:
                context = _launch_persistent_context(
                    playwright,
                    self.browser_profile_dir,
                    browser_channel=channel,
                    headless=self.headless,
                    accept_downloads=True,
                )
                page = context.pages[0] if context.pages else context.new_page()
                browser = None
            except Exception as exc:
                profile_error = exc
                context = None

        if context is None or page is None:
            raise HappyScribeWebError(
                "Could not launch a browser for HappyScribe export. "
                f"Last error: {profile_error}"
            ) from profile_error

        page.goto(editor_url, wait_until="domcontentloaded", timeout=EDITOR_READY_TIMEOUT_MS)
        self._wait_for_editor(page)
        return browser, context, page

    def _close_editor_context(self, browser, context) -> None:
        context.storage_state(path=str(self.browser_state_path))
        context.close()
        if browser is not None:
            browser.close()

    def _export_via_ui(
        self,
        transcription_id: str,
        destination: Path,
        cookies: list[dict[str, Any]],
    ) -> Path:
        sync_playwright = _require_playwright()
        destination.parent.mkdir(parents=True, exist_ok=True)

        last_error: Exception | None = None
        for channel in _browser_channel_candidates(self.browser_channel):
            try:
                with sync_playwright() as playwright:
                    browser, context, page = self._open_editor_page(
                        playwright,
                        transcription_id,
                        browser_channel=channel,
                    )

                    self._load_video_export_form(page, transcription_id)
                    self._start_hardcoded_video_export(page, transcription_id)

                    download_link = self._wait_for_video_download_link(page)
                    response = page.request.get(download_link)
                    if response.status >= 400:
                        raise HappyScribeWebError(
                            f"HappyScribe video download failed with HTTP {response.status}"
                        )
                    destination.write_bytes(response.body())
                    self._close_editor_context(browser, context)
                return destination
            except Exception as exc:
                last_error = exc
                try:
                    self._save_debug_artifacts(page, transcription_id[:8])
                except Exception:
                    pass
                continue

        raise HappyScribeWebError(
            f"HappyScribe UI export failed: {last_error}"
        ) from last_error

    def _load_session_cookies(self) -> list[dict[str, Any]]:
        sync_playwright = _require_playwright()
        last_error: Exception | None = None
        for channel in _browser_channel_candidates(self.browser_channel):
            try:
                with sync_playwright() as playwright:
                    browser, context = _launch_context_from_session(
                        playwright,
                        self.browser_state_path,
                        browser_channel=channel,
                        headless=True,
                    )
                    cookies = context.cookies()
                    context.close()
                    browser.close()
                if cookies:
                    return cookies
            except Exception as exc:
                last_error = exc
                continue
        raise HappyScribeWebError(
            f"Could not load HappyScribe browser session cookies: {last_error}"
        ) from last_error

    def export_video_with_subtitles(
        self,
        transcription_id: str,
        destination: Path,
    ) -> Path:
        self._load_session_cookies()
        return self._export_via_ui(transcription_id, destination, [])


def export_video_with_subtitles_web(
    transcription_id: str,
    destination: Path,
    *,
    browser_state_path: Path,
    headless: bool = False,
    browser_channel: str | None = "chrome",
    browser_profile_dir: Path | None = None,
    api_key: str | None = None,
) -> Path:
    client = HappyScribeWebClient(
        browser_state_path,
        headless=headless,
        browser_channel=browser_channel,
        browser_profile_dir=browser_profile_dir,
        api_key=api_key,
    )
    return client.export_video_with_subtitles(transcription_id, destination)


def export_video_for_transcription_name(
    transcription_id: str,
    transcription_name: str,
    download_dir: Path,
    *,
    browser_state_path: Path,
    headless: bool = False,
    browser_channel: str | None = "chrome",
    browser_profile_dir: Path | None = None,
    api_key: str | None = None,
) -> Path:
    destination = burned_video_destination_path(download_dir, transcription_name)
    return export_video_with_subtitles_web(
        transcription_id,
        destination,
        browser_state_path=browser_state_path,
        headless=headless,
        browser_channel=browser_channel,
        browser_profile_dir=browser_profile_dir,
        api_key=api_key,
    )
