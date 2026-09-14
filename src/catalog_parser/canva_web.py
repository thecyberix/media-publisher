"""Authenticated Canva access via a saved Playwright browser session.

Used when the Canva Connect API cannot export a specific design (typically
``permission_denied``). Same pattern as Smartcat: log in once, persist
``canva-state.json``, reuse the cookies in CI.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from catalog_parser.canva import CanvaError

DEFAULT_STORAGE_STATE = "canva-state.json"
DEFAULT_UI_BASE = "https://www.canva.com"
PAGE_GOTO_TIMEOUT_MS = 90_000
NETWORK_IDLE_TIMEOUT_MS = 30_000
DOWNLOAD_TIMEOUT_MS = 120_000
SPA_SETTLE_MS = 5_000
DEFAULT_BROWSER_CHANNELS = ("chrome", "msedge")
DEFAULT_BROWSER_PROFILE_RELATIVE = "credentials/canva-browser-profile"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
LOGIN_URL_PATTERN = re.compile(
    r"canva\.com/(?:login|signup)|accounts\.google\.com|login\.microsoftonline\.com",
    re.IGNORECASE,
)
PERMISSION_DENIED_PATTERN = re.compile(
    r"don.?t have permission to see this design|"
    r"not allowed to access this design|"
    r"ask the owner to share",
    re.IGNORECASE,
)
CLOUDFLARE_CHALLENGE_TIMEOUT_MS = 45_000
CLOUDFLARE_TITLE_PATTERN = re.compile(
    r"just a moment|verify you are human|designing again soon",
    re.IGNORECASE,
)


def _looks_like_login_url(url: str) -> bool:
    return bool(LOGIN_URL_PATTERN.search(url))


def _looks_like_cloudflare_challenge(*, url: str = "", title: str = "", html: str = "") -> bool:
    haystack = " ".join((url, title, html[:4000]))
    if CLOUDFLARE_TITLE_PATTERN.search(title or haystack):
        return True
    lowered = haystack.casefold()
    return "challenges.cloudflare.com" in lowered or "cf-turnstile" in lowered


def _looks_like_design_permission_denied(*, url: str = "", title: str = "", html: str = "") -> bool:
    haystack = " ".join((url, title, html[:8000]))
    return bool(PERMISSION_DENIED_PATTERN.search(haystack))


def _is_canva_design_page(url: str) -> bool:
    return "/design/" in urlparse(url).path.casefold()


def _is_usable_preview_url(url: str | None) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    return "media.canva.com" in url.casefold()


def _design_edit_url(canva_url: str, ui_base: str) -> str | None:
    from catalog_parser.canva import parse_canva_design_url

    design_id = parse_canva_design_url(canva_url)
    if not design_id:
        return None
    return f"{ui_base.rstrip('/')}/design/{design_id}/edit"


def _settle_page(page: Any) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
    except Exception:
        page.wait_for_timeout(SPA_SETTLE_MS)


def _page_title(page: Any) -> str:
    getter = getattr(page, "title", None)
    if callable(getter):
        return str(getter() or "")
    return ""


def _ensure_canva_session_page(page: Any) -> None:
    title = _page_title(page)
    html_head = ""
    try:
        html_head = page.content()[:4000]
    except Exception:
        html_head = ""
    if _looks_like_cloudflare_challenge(url=page.url, title=title, html=html_head):
        try:
            page.wait_for_function(
                "() => document.title && document.title !== 'Just a moment...'",
                timeout=CLOUDFLARE_CHALLENGE_TIMEOUT_MS,
            )
            _settle_page(page)
        except Exception as exc:
            raise CanvaError(
                "Canva Cloudflare challenge blocked the browser session. "
                "Close other Chrome windows using credentials/canva-browser-profile, "
                "then retry locally with installed Chrome "
                "(python -m catalog_parser --canva-login)."
            ) from exc
    if _looks_like_login_url(page.url):
        raise CanvaError(
            "Canva session expired. Run: python -m catalog_parser --canva-login"
        )


COOKIE_BANNER_NAMES = (
    r"Accept all cookies",
    r"Accept all",
)
SHARE_ACCEPT_NAMES = (
    r"Accept invite",
    r"Accept invitation",
    r"Accept and open",
    r"^Accept$",
    r"Open design",
    r"View design",
    r"Edit this design",
    r"Use this template",
)


def _click_named_control(page: Any, names: tuple[str, ...]) -> bool:
    for name in names:
        pattern = re.compile(name, re.IGNORECASE)
        for role in ("button", "link"):
            try:
                locator = page.get_by_role(role, name=pattern)
                if locator.count() < 1:
                    continue
                locator.first.click(timeout=5_000)
                return True
            except Exception:
                continue
    return False


def _accept_shared_design_if_needed(page: Any) -> None:
    """Click Canva's share-invite / cookie banners when the Drive link requires it.

    The Connect API cannot accept collaboration invites. A logged-in Playwright
    session can open the share URL and accept it in the UI.
    """
    _click_named_control(page, COOKIE_BANNER_NAMES)
    html = ""
    try:
        html = page.content()[:12_000]
    except Exception:
        html = ""
    haystack = f"{_page_title(page)} {html}".casefold()
    looks_like_invite = any(
        marker in haystack
        for marker in (
            "invited",
            "accept invite",
            "accept invitation",
            "don't have permission",
            "don\u2019t have permission",
            "ask the owner to share",
        )
    )
    if not looks_like_invite:
        return
    if _click_named_control(page, SHARE_ACCEPT_NAMES):
        _settle_page(page)
        _ensure_canva_session_page(page)


def _browser_channel_candidates(preferred: str | None) -> tuple[str, ...]:
    normalized = (preferred or "").strip().lower()
    if normalized in {"edge", "msedge"}:
        normalized = "msedge"
    if normalized in DEFAULT_BROWSER_CHANNELS:
        return (normalized, *(channel for channel in DEFAULT_BROWSER_CHANNELS if channel != normalized))
    return DEFAULT_BROWSER_CHANNELS


def _channel_label(channel: str) -> str:
    if channel == "msedge":
        return "Microsoft Edge"
    if channel == "chrome":
        return "Google Chrome"
    return channel


def _launch_persistent_context(
    playwright: Any,
    profile_dir: Path,
    *,
    channel: str,
    headless: bool,
) -> Any:
    profile_dir.mkdir(parents=True, exist_ok=True)
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        channel=channel,
        headless=headless,
        viewport={"width": 1440, "height": 960},
        locale="en-US",
        user_agent=DEFAULT_USER_AGENT,
        ignore_default_args=["--enable-automation"],
        args=["--disable-blink-features=AutomationControlled"],
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return context


def _launch_login_context(playwright: Any, profile_dir: Path, *, channel: str) -> Any:
    return _launch_persistent_context(
        playwright,
        profile_dir,
        channel=channel,
        headless=False,
    )


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CanvaError(
            "Playwright is required for Canva browser access. "
            "Install it with: pip install playwright && playwright install chromium"
        ) from exc
    return sync_playwright


def load_storage_state_cookies(storage_state_path: Path) -> list[dict[str, Any]]:
    if not storage_state_path.exists():
        raise CanvaError(
            f"Canva browser session not found at {storage_state_path}. "
            "Run: python -m catalog_parser --canva-login"
        )

    payload = json.loads(storage_state_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise CanvaError(
            f"Invalid Canva session file {storage_state_path}: expected a JSON object"
        )
    cookies = payload.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        raise CanvaError(
            f"Invalid Canva session file {storage_state_path}: "
            "expected a top-level 'cookies' array"
        )
    return cookies


def cookies_header(cookies: list[dict[str, Any]], *, host: str) -> str:
    host = host.lower().removeprefix("www.")
    relevant: list[str] = []
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        domain = str(cookie.get("domain", "")).lower().removeprefix(".")
        if domain and host not in domain and not host.endswith(f".{domain}"):
            continue
        relevant.append(f"{name}={value}")
    if not relevant:
        raise CanvaError(
            "Canva session file has no cookies for "
            f"{host!r}. Re-run: python -m catalog_parser --canva-login"
        )
    return "; ".join(relevant)


def ensure_storage_state_file(
    storage_state_path: Path,
    *,
    project_root: Path | None = None,
) -> Path | None:
    if storage_state_path.is_file():
        return storage_state_path

    json_blob = os.getenv("CANVA_STORAGE_STATE_JSON", "").strip()
    if not json_blob:
        return None

    if not storage_state_path.is_absolute() and project_root is not None:
        storage_state_path = project_root / storage_state_path
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    storage_state_path.write_text(json_blob, encoding="utf-8")
    return storage_state_path


def verify_session(storage_state_path: Path, *, ui_base: str = DEFAULT_UI_BASE) -> None:
    """Cookie GET of Canva home; fail if redirected to login."""
    cookies = load_storage_state_cookies(storage_state_path)
    ui_base = ui_base.rstrip("/")
    host = urlparse(ui_base).netloc or "www.canva.com"
    url = f"{ui_base}/"
    headers = {
        "Cookie": cookies_header(cookies, host=host),
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    request = urllib.request.Request(url, method="GET", headers=headers)

    class _CaptureRedirect(urllib.request.HTTPRedirectHandler):
        def __init__(self) -> None:
            self.final_url = url

        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
            self.final_url = newurl
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    capture = _CaptureRedirect()
    opener = urllib.request.build_opener(capture)
    try:
        with opener.open(request, timeout=60) as response:
            final_url = getattr(response, "geturl", lambda: capture.final_url)()
            body = response.read(16384)
            status = response.status
    except urllib.error.HTTPError as exc:
        final_url = getattr(exc, "url", None) or capture.final_url
        body = exc.read(16384) if exc.fp is not None else b""
        status = exc.code
    except urllib.error.URLError as exc:
        raise CanvaError(
            f"Canva session check could not reach {url!r}: {exc.reason}. "
            "If it keeps failing, renew with: python -m catalog_parser --canva-login"
        ) from exc

    final = str(final_url or capture.final_url)
    if status in {401, 403} or _looks_like_login_url(final):
        raise CanvaError(
            "Canva session expired. Run: python -m catalog_parser --canva-login"
        )
    snippet = body.decode("utf-8", errors="replace").casefold()
    if "log in" in snippet and "password" in snippet and "canva" in snippet:
        raise CanvaError(
            "Canva session expired (login page content). "
            "Run: python -m catalog_parser --canva-login"
        )


def _resolve_browser_profile_dir(project_root: Path | None) -> Path | None:
    raw = os.getenv("CANVA_BROWSER_PROFILE", DEFAULT_BROWSER_PROFILE_RELATIVE).strip()
    profile_dir = Path(raw or DEFAULT_BROWSER_PROFILE_RELATIVE)
    if not profile_dir.is_absolute() and project_root is not None:
        profile_dir = project_root / profile_dir
    return profile_dir if profile_dir.exists() else None


def build_canva_web_client_from_env(
    *,
    project_root: Path | None = None,
    headless: bool = True,
) -> CanvaWebClient | None:
    storage_name = os.getenv("CANVA_STORAGE_STATE", DEFAULT_STORAGE_STATE).strip()
    storage_state_path = Path(storage_name or DEFAULT_STORAGE_STATE)
    if not storage_state_path.is_absolute() and project_root is not None:
        storage_state_path = project_root / storage_state_path
    resolved = ensure_storage_state_file(
        storage_state_path,
        project_root=project_root,
    )
    profile_dir = _resolve_browser_profile_dir(project_root)
    if resolved is None and profile_dir is None:
        return None
    return CanvaWebClient(
        storage_state_path=resolved or (profile_dir / "canva-state.json"),
        headless=headless,
        browser_profile_dir=profile_dir,
        browser_channel=os.getenv("CANVA_BROWSER_CHANNEL", "").strip() or None,
    )


class CanvaWebClient:
    """Open Canva designs with a saved Playwright storage state."""

    def __init__(
        self,
        *,
        storage_state_path: Path,
        headless: bool = True,
        ui_base: str = DEFAULT_UI_BASE,
        browser_profile_dir: Path | None = None,
        browser_channel: str | None = None,
    ) -> None:
        self.storage_state_path = storage_state_path
        self.headless = headless
        self.ui_base = ui_base.rstrip("/")
        self.browser_profile_dir = browser_profile_dir
        self.browser_channel = browser_channel

    def _launch_page_context(self, playwright: Any) -> tuple[Any, Any | None]:
        last_error: Exception | None = None
        if self.browser_profile_dir is not None:
            for channel in _browser_channel_candidates(self.browser_channel):
                try:
                    context = _launch_persistent_context(
                        playwright,
                        self.browser_profile_dir,
                        channel=channel,
                        headless=self.headless,
                    )
                    return context, None
                except Exception as exc:
                    last_error = exc
                    continue
        for channel in _browser_channel_candidates(self.browser_channel):
            try:
                browser = playwright.chromium.launch(
                    channel=channel,
                    headless=self.headless,
                    ignore_default_args=["--enable-automation"],
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = browser.new_context(
                    storage_state=str(self.storage_state_path),
                    viewport={"width": 1440, "height": 960},
                    locale="en-US",
                    user_agent=DEFAULT_USER_AGENT,
                )
                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                return context, browser
            except Exception as exc:
                last_error = exc
                continue
        raise CanvaError(
            "Could not launch Chrome or Edge for Canva download. "
            f"Last error: {last_error}"
        ) from last_error

    @contextmanager
    def _playwright_page(self) -> Iterator[Any]:
        sync_playwright = _require_playwright()
        has_profile = bool(
            self.browser_profile_dir and self.browser_profile_dir.exists()
        )
        if not has_profile and not self.storage_state_path.exists():
            raise CanvaError(
                f"Canva browser session not found at {self.storage_state_path}. "
                "Run: python -m catalog_parser --canva-login"
            )

        with sync_playwright() as playwright:
            context, browser = self._launch_page_context(playwright)
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(PAGE_GOTO_TIMEOUT_MS)
            try:
                yield page
            finally:
                context.close()
                if browser is not None:
                    browser.close()

    def download_design_image(self, canva_url: str, destination: Path) -> Path:
        with self._playwright_page() as page:
            return self._download_with_page(page, canva_url, destination)

    def _download_with_page(
        self,
        page: Any,
        canva_url: str,
        destination: Path,
    ) -> Path:
        from media_publisher.sources.canva import resolve_canva_url
        from media_publisher.sources.canva_share_preview import (
            normalize_canva_share_url,
            pick_best_canva_media_url,
            preview_image_url_from_html,
        )

        resolved = resolve_canva_url(canva_url)
        view_url = normalize_canva_share_url(resolved)
        media_urls: list[str] = []

        def on_response(response: Any) -> None:
            url = getattr(response, "url", "")
            if isinstance(url, str) and "media.canva.com" in url:
                media_urls.append(url)

        page.on("response", on_response)
        page.goto(view_url, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
        _settle_page(page)
        _ensure_canva_session_page(page)
        _accept_shared_design_if_needed(page)

        if not _is_canva_design_page(page.url):
            edit_url = _design_edit_url(resolved, self.ui_base)
            if edit_url and edit_url.rstrip("/") != view_url.rstrip("/"):
                page.goto(edit_url, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
                _settle_page(page)
                _ensure_canva_session_page(page)
                _accept_shared_design_if_needed(page)

        if not _is_canva_design_page(page.url):
            raise CanvaError(
                f"Canva session did not open design {canva_url!r} "
                f"(ended on {page.url!r})"
            )

        html = page.content()
        if _looks_like_design_permission_denied(
            url=page.url,
            title=_page_title(page),
            html=html,
        ):
            raise CanvaError(
                f"Canva account is logged in but cannot open {canva_url!r}. "
                "Share the design with this account, or re-run "
                "python -m catalog_parser --canva-login with an account that "
                "can open the package designs."
            )
        preview_url: str | None = None
        try:
            candidate = preview_image_url_from_html(html)
        except Exception:
            candidate = None
        if _is_usable_preview_url(candidate):
            preview_url = candidate
        if not preview_url:
            for _ in range(3):
                preview_url = pick_best_canva_media_url(media_urls)
                if _is_usable_preview_url(preview_url):
                    break
                preview_url = None
                page.wait_for_timeout(SPA_SETTLE_MS)

        if not _is_usable_preview_url(preview_url):
            edit_url = _design_edit_url(resolved, self.ui_base)
            if edit_url and "/edit" not in urlparse(page.url).path.casefold():
                page.goto(edit_url, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
                _settle_page(page)
                _ensure_canva_session_page(page)
                for _ in range(8):
                    preview_url = pick_best_canva_media_url(media_urls)
                    if _is_usable_preview_url(preview_url):
                        break
                    preview_url = None
                    page.wait_for_timeout(SPA_SETTLE_MS)

        destination.parent.mkdir(parents=True, exist_ok=True)
        if _is_usable_preview_url(preview_url):
            response = page.request.get(preview_url, timeout=DOWNLOAD_TIMEOUT_MS)
            status = int(getattr(response, "status", 0) or 0)
            if status >= 400:
                raise CanvaError(
                    f"Canva preview download failed with HTTP {status} for {canva_url!r}"
                )
            destination.write_bytes(response.body())
            if destination.stat().st_size < 100:
                raise CanvaError(
                    f"Canva preview download was empty for {canva_url!r}"
                )
            return destination

        if _is_canva_design_page(page.url):
            try:
                canvas = page.locator("canvas").first
                if canvas.count() > 0:
                    canvas.screenshot(path=str(destination), type="jpeg", quality=90)
                else:
                    page.screenshot(path=str(destination), type="jpeg", quality=90)
            except Exception:
                page.screenshot(path=str(destination), type="jpeg", quality=90)
            if destination.is_file() and destination.stat().st_size >= 1000:
                return destination

        raise CanvaError(
            f"Could not find a Canva preview image for {canva_url!r} "
            f"(ended on {page.url!r})"
        )


def download_canva_design_via_web(
    canva_url: str,
    destination: Path,
    *,
    client: CanvaWebClient | None = None,
    project_root: Path | None = None,
) -> Path:
    resolved = client or build_canva_web_client_from_env(project_root=project_root)
    if resolved is None:
        raise CanvaError(
            "Canva browser session not found. "
            "Run: python -m catalog_parser --canva-login"
        )
    return resolved.download_design_image(canva_url, destination)


def login_interactive(
    *,
    storage_state_path: Path,
    ui_base: str = DEFAULT_UI_BASE,
    browser_profile_dir: Path | None = None,
    browser_channel: str | None = None,
) -> None:
    """Open installed Chrome or Edge (not Playwright Chromium) and save the session.

    Google blocks sign-in in automated Chromium. A persistent Chrome/Edge profile
    is closer to a normal browser. If Google still refuses, import cookies from
    the browser you already use: ``python -m catalog_parser --canva-import-session``.
    """
    sync_playwright = _require_playwright()
    ui_base = ui_base.rstrip("/")
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    profile_dir = browser_profile_dir or Path(DEFAULT_BROWSER_PROFILE_RELATIVE)

    last_error: Exception | None = None
    channels = _browser_channel_candidates(browser_channel)
    for channel in channels:
        label = _channel_label(channel)
        try:
            with sync_playwright() as playwright:
                context = _launch_login_context(
                    playwright,
                    profile_dir,
                    channel=channel,
                )
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(ui_base, wait_until="commit", timeout=PAGE_GOTO_TIMEOUT_MS)

                print(f"Opened Canva in {label} (persistent profile).")
                print("Google often blocks Playwright Chromium; this uses installed Chrome/Edge.")
                print("1. Log in with the Canva account that can open the package designs.")
                print("2. Confirm you can open a design (not a Google/Canva login page).")
                print(
                    "If Google says this browser is not secure, close the window and import "
                    "cookies from your normal Chrome instead:"
                )
                print("  python -m catalog_parser --canva-import-session canva-cookies.json")
                input("Press Enter here after you are logged in... ")

                if _looks_like_login_url(page.url):
                    context.close()
                    print()
                    print_canva_import_instructions(ui_base=ui_base)
                    raise CanvaError(
                        "Still on a Google/Canva login page. Google blocks automated "
                        "sign-in. Log in with your normal Chrome or Edge, then run "
                        "python -m catalog_parser --canva-import-session canva-cookies.json"
                    )

                context.storage_state(path=str(storage_state_path))
                context.close()
            print(f"Saved Canva session to {storage_state_path}")
            return
        except CanvaError:
            raise
        except Exception as exc:
            last_error = exc
            print(f"Could not open {label}: {exc}")
            continue

    print()
    print_canva_import_instructions(ui_base=ui_base)
    raise CanvaError(
        "Could not launch Chrome or Edge for Canva login. Install one of them, "
        "or import cookies from your normal browser with "
        "--canva-import-session. "
        f"Last error: {last_error}"
    ) from last_error


def import_browser_session_file(
    source_path: Path,
    destination_path: Path,
    *,
    ui_base: str = DEFAULT_UI_BASE,
) -> None:
    from catalog_parser.smartcat import SmartcatError
    from catalog_parser.smartcat_cookie import (
        cookies_from_import_payload,
        write_storage_state,
    )

    payload = json.loads(source_path.read_text(encoding="utf-8"))
    try:
        cookies = cookies_from_import_payload(payload)
        write_storage_state(destination_path, cookies)
    except SmartcatError as exc:
        raise CanvaError(str(exc)) from exc
    verify_session(destination_path, ui_base=ui_base)


def print_canva_import_instructions(*, ui_base: str = DEFAULT_UI_BASE) -> None:
    print(
        "Import Canva session from your normal browser (Cookie-Editor JSON):\n"
        f"  1. Log in to {ui_base.rstrip('/')} in Chrome or Edge.\n"
        "  2. Install Cookie-Editor (Moustachauve) — https://cookie-editor.com/\n"
        "  3. On a Canva page, open Cookie-Editor → Export → JSON.\n"
        "  4. Paste into a file, e.g. canva-cookies.json\n"
        "  5. Run:\n"
        "       python -m catalog_parser --canva-import-session canva-cookies.json\n"
        "     This writes canva-state.json and verifies the session.\n"
        "\n"
        "To refresh GitHub Actions later, copy the new canva-state.json into the\n"
        "CANVA_STORAGE_STATE_JSON secret."
    )
