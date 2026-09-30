"""Temporary Facebook Page photo publishing via Playwright.

Used while the Meta app is in Development mode: Graph API photo posts are only
visible to app roles, but the Page composer creates public posts. Remove this
path after App Review makes Graph posts public.
"""
from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlparse

DEFAULT_STORAGE_STATE = "credentials/facebook-browser-state.json"
DEFAULT_BROWSER_PROFILE = "credentials/facebook-browser-profile"
DEFAULT_BROWSER_CHANNELS = ("chrome", "msedge")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
PAGE_GOTO_TIMEOUT_MS = 45_000
NETWORK_IDLE_TIMEOUT_MS = 15_000
SPA_SETTLE_MS = 2_000
POST_TIMEOUT_MS = 60_000
LOGIN_URL_PATTERN = re.compile(
    # Business Suite uses /business/loginpage/?next=...composer... (not /login).
    r"facebook\.com/(?:login|checkpoint)|loginpage|m\.facebook\.com/login",
    re.IGNORECASE,
)
LOGIN_MODAL_PATTERN = re.compile(
    r"log\s*in|email or phone|create new account|forgot password",
    re.IGNORECASE,
)
AUTH_COOKIE_NAMES = frozenset({"c_user", "xs"})
POST_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?facebook\.com/[^\s\"']+"
    r"(?:/posts/\d+|/photos/[^\"'\s]+|/permalink/\d+|story_fbid=\d+|fbid=\d+)",
    re.IGNORECASE,
)
COOKIE_BANNER_NAMES = (
    r"Allow all cookies",
    r"Accept all",
    r"Accept All",
    r"Decline optional cookies",
)
COMPOSER_OPEN_NAMES = (
    r"Add photo/video",
    r"Add photos/videos",
    r"Photo/video",
    r"Photo / video",
    r"Add photo",
    r"^Photo$",
    r"Photos/videos",
    r"Create post",
    r"Create a post",
)
BUSINESS_SUITE_COMPOSER_URL = "https://business.facebook.com/latest/composer/"
BUSINESS_SUITE_HOME_URL = "https://business.facebook.com/"
# From a working Business Suite composer URL for SadhguruBulgarian (override via env).
DEFAULT_BUSINESS_ASSET_ID = "108518418983337"
DEFAULT_BUSINESS_ID = "2391168584397645"
POST_BUTTON_NAMES = (
    r"^Publish$",
    r"^Post$",
    r"^Share$",
    r"^Schedule$",
)
SCHEDULE_TOGGLE_NAMES = (
    r"Set date and time",
    r"Schedule",
)
SCHEDULE_MENU_NAMES = (
    r"^Schedule$",
    r"Schedule post",
    r"Schedule Post",
)
SCHEDULE_CONFIRM_NAMES = (
    r"^Schedule$",
    r"Schedule post",
    r"Schedule Post",
    r"^Publish$",
)
POST_OPTIONS_NAMES = (
    r"See more",
    r"Post options",
    r"More options",
    r"Open post options",
)


class FacebookWebError(RuntimeError):
    pass


def facebook_photo_via_browser_enabled(*, project_root: Path | None = None) -> bool:
    """True when a Facebook Playwright session is available.

    Enabled by GitHub secret ``FACEBOOK_BROWSER_STATE_JSON``, or a local
    ``credentials/facebook-browser-state.json`` with auth cookies (after
    ``--facebook-browser-login`` / import). No separate feature flag.
    """
    if os.getenv("FACEBOOK_BROWSER_STATE_JSON", "").strip():
        return True
    state_path = resolve_facebook_browser_state_path(project_root=project_root)
    return storage_state_has_auth_cookies(state_path)


def _truthy_headless_default() -> bool:
    """Headed locally; headless in GitHub Actions unless overridden."""
    raw = os.getenv("FACEBOOK_BROWSER_HEADLESS", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return os.getenv("GITHUB_ACTIONS", "").strip().lower() in {"1", "true"}


def _browser_channel_candidates(preferred: str | None) -> tuple[str | None, ...]:
    """Ordered browser channels; ``None`` means Playwright-bundled Chromium."""
    normalized = (preferred or "").strip().lower()
    if normalized in {"", "chromium", "bundled", "none"}:
        # CI / explicit Chromium: try bundled first, then installed Chrome/Edge.
        return (None, "chrome", "msedge")
    if normalized in {"edge", "msedge"}:
        normalized = "msedge"
    if normalized in DEFAULT_BROWSER_CHANNELS:
        return (
            normalized,
            *(channel for channel in DEFAULT_BROWSER_CHANNELS if channel != normalized),
            None,
        )
    return (*DEFAULT_BROWSER_CHANNELS, None)


def _channel_label(channel: str | None) -> str:
    if channel is None:
        return "Chromium"
    if channel == "msedge":
        return "Microsoft Edge"
    if channel == "chrome":
        return "Google Chrome"
    return channel


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise FacebookWebError(
            "Playwright is required for Facebook browser publishing. "
            "Install it with: pip install playwright && playwright install chromium"
        ) from exc
    return sync_playwright


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _resolve_path(raw: str | Path, *, project_root: Path | None) -> Path:
    path = Path(raw)
    if not path.is_absolute() and project_root is not None:
        return project_root / path
    return path


def resolve_facebook_browser_state_path(
    *,
    project_root: Path | None = None,
    override: str | Path | None = None,
) -> Path:
    raw = override or os.getenv("FACEBOOK_BROWSER_STATE", DEFAULT_STORAGE_STATE).strip()
    return _resolve_path(raw or DEFAULT_STORAGE_STATE, project_root=project_root)


def resolve_facebook_browser_profile_dir(
    *,
    project_root: Path | None = None,
    override: str | Path | None = None,
) -> Path:
    raw = (
        override
        or os.getenv("FACEBOOK_BROWSER_PROFILE", DEFAULT_BROWSER_PROFILE).strip()
    )
    return _resolve_path(raw or DEFAULT_BROWSER_PROFILE, project_root=project_root)


def resolve_facebook_browser_channel() -> str | None:
    """Preferred browser channel.

    Empty / ``chromium`` / ``bundled`` / ``none`` → Playwright Chromium.
    Unset defaults to Chrome locally and Chromium under ``GITHUB_ACTIONS``.
    """
    if "FACEBOOK_BROWSER_CHANNEL" in os.environ:
        raw = os.environ["FACEBOOK_BROWSER_CHANNEL"].strip().lower()
        if raw in {"", "chromium", "bundled", "none"}:
            return None
        if raw in {"edge", "msedge"}:
            return "msedge"
        return raw
    if os.getenv("GITHUB_ACTIONS", "").strip().lower() in {"1", "true"}:
        return None
    return "chrome"


def resolve_facebook_browser_proxy(
    *,
    override: str | dict[str, str] | None = None,
) -> dict[str, str] | None:
    """Playwright ``proxy=`` dict from env (Webshare / HTTP proxies).

    Preferred forms (Chromium ignores ``user:pass@`` embedded in the server URL)::

        FACEBOOK_BROWSER_PROXY=http://USER:PASS@p.webshare.io:80
        # or
        FACEBOOK_BROWSER_PROXY_SERVER=http://p.webshare.io:80
        FACEBOOK_BROWSER_PROXY_USERNAME=USER
        FACEBOOK_BROWSER_PROXY_PASSWORD=PASS

    Returns ``None`` when unset.
    """
    if isinstance(override, dict):
        server = str(override.get("server") or "").strip()
        if not server:
            return None
        out: dict[str, str] = {"server": server}
        user = str(override.get("username") or "").strip()
        password = str(override.get("password") or "")
        if user:
            out["username"] = user
            out["password"] = password
        return out

    raw = (override if isinstance(override, str) else None) or os.getenv(
        "FACEBOOK_BROWSER_PROXY", ""
    ).strip()
    server = os.getenv("FACEBOOK_BROWSER_PROXY_SERVER", "").strip()
    username = os.getenv("FACEBOOK_BROWSER_PROXY_USERNAME", "").strip()
    password = os.getenv("FACEBOOK_BROWSER_PROXY_PASSWORD", "")

    if raw:
        parsed = urlparse(raw if "://" in raw else f"http://{raw}")
        if not parsed.hostname:
            raise FacebookWebError(
                f"Invalid FACEBOOK_BROWSER_PROXY (missing host): {raw!r}"
            )
        scheme = (parsed.scheme or "http").lower()
        if scheme not in {"http", "https", "socks5"}:
            raise FacebookWebError(
                f"Unsupported FACEBOOK_BROWSER_PROXY scheme {scheme!r}; "
                "use http:// or socks5://"
            )
        port = parsed.port
        if port is None:
            port = 443 if scheme == "https" else 80
        server = f"{scheme}://{parsed.hostname}:{port}"
        if parsed.username is not None:
            username = unquote(parsed.username)
        if parsed.password is not None:
            password = unquote(parsed.password)

    if not server:
        return None

    if "://" not in server:
        server = f"http://{server}"

    result: dict[str, str] = {"server": server}
    if username:
        result["username"] = username
        result["password"] = password
    return result


def _proxy_log_label(proxy: dict[str, str] | None) -> str:
    if not proxy:
        return "none"
    server = proxy.get("server", "")
    user = proxy.get("username")
    if user:
        return f"{server} (user={user})"
    return server


def resolve_facebook_business_asset_id() -> str:
    return (
        os.getenv("FACEBOOK_BUSINESS_ASSET_ID", "").strip()
        or DEFAULT_BUSINESS_ASSET_ID
    )


def resolve_facebook_business_id() -> str:
    return (
        os.getenv("FACEBOOK_BUSINESS_ID", "").strip()
        or DEFAULT_BUSINESS_ID
    )


def business_suite_composer_url(
    *,
    asset_id: str | None = None,
    business_id: str | None = None,
) -> str:
    """Direct composer URL — one Business Suite load (avoids home→composer OOM)."""
    asset = (asset_id or resolve_facebook_business_asset_id()).strip()
    business = (business_id or resolve_facebook_business_id()).strip()
    if not asset:
        raise FacebookWebError(
            "FACEBOOK_BUSINESS_ASSET_ID is required for Business Suite publishing"
        )
    query = f"asset_id={asset}"
    if business:
        query += f"&business_id={business}"
    return f"{BUSINESS_SUITE_COMPOSER_URL}?{query}"


def page_feed_url(page_username: str) -> str:
    username = page_username.strip().lstrip("@")
    if not username:
        raise FacebookWebError("META_PAGE_USERNAME is required for browser publishing")
    return f"https://www.facebook.com/{username}"


def _looks_like_login_url(url: str) -> bool:
    return bool(LOGIN_URL_PATTERN.search(url))


def storage_state_has_auth_cookies(storage_state_path: Path) -> bool:
    """True when the Playwright storage-state includes Facebook session cookies."""
    if not storage_state_path.is_file():
        return False
    try:
        payload = json.loads(storage_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    cookies = payload.get("cookies") if isinstance(payload, dict) else None
    if not isinstance(cookies, list):
        return False
    names = {
        str(cookie.get("name"))
        for cookie in cookies
        if isinstance(cookie, dict) and cookie.get("name")
    }
    return AUTH_COOKIE_NAMES.issubset(names)


def _page_shows_login_wall(page: Any) -> bool:
    # URL-only: full DOM queries on Business Suite are too slow/fragile.
    return _looks_like_login_url(page.url)


def _settle_page(page: Any) -> None:
    # Prefer a short load wait — Business Suite never goes network-idle.
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception:
        pass
    page.wait_for_timeout(SPA_SETTLE_MS)


def _launch_persistent_context(
    playwright: Any,
    profile_dir: Path,
    *,
    channel: str | None,
    headless: bool,
    timezone_id: str | None = None,
    proxy: dict[str, str] | None = None,
) -> Any:
    profile_dir.mkdir(parents=True, exist_ok=True)
    launch_kwargs: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": headless,
        "viewport": {"width": 1280, "height": 900},
        "locale": "en-US",
        "user_agent": DEFAULT_USER_AGENT,
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-extensions",
        ],
    }
    if timezone_id:
        launch_kwargs["timezone_id"] = timezone_id
    if channel:
        launch_kwargs["channel"] = channel
    if proxy:
        launch_kwargs["proxy"] = proxy
    context = playwright.chromium.launch_persistent_context(**launch_kwargs)
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    _install_business_suite_resource_blocks(context)
    return context


def _install_business_suite_resource_blocks(context: Any) -> None:
    """Abort only heavy media/fonts so the composer renderer is less likely to freeze."""

    def _handler(route: Any) -> None:
        request = route.request
        resource_type = (request.resource_type or "").lower()
        if resource_type in {"media", "font"}:
            route.abort()
            return
        route.continue_()

    try:
        context.route("**/*", _handler)
    except Exception:
        pass


def _launch_storage_context(
    playwright: Any,
    storage_state_path: Path,
    *,
    channel: str | None,
    headless: bool,
    timezone_id: str | None = None,
    proxy: dict[str, str] | None = None,
) -> tuple[Any, Any]:
    launch_kwargs: dict[str, Any] = {
        "headless": headless,
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--disable-background-networking",
            "--js-flags=--max-old-space-size=2048",
        ],
    }
    if channel:
        launch_kwargs["channel"] = channel
    # Chromium: proxy must be set at launch (per-context alone is ignored).
    if proxy:
        launch_kwargs["proxy"] = proxy
    browser = playwright.chromium.launch(**launch_kwargs)
    context_kwargs: dict[str, Any] = {
        "storage_state": str(storage_state_path),
        "viewport": {"width": 1280, "height": 900},
        "locale": "en-US",
        "user_agent": DEFAULT_USER_AGENT,
    }
    if timezone_id:
        context_kwargs["timezone_id"] = timezone_id
    context = browser.new_context(**context_kwargs)
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    _install_business_suite_resource_blocks(context)
    return browser, context


def _click_named_control(
    page: Any,
    names: tuple[str, ...],
    *,
    timeout_ms: int = 5_000,
    no_wait_after: bool = False,
) -> bool:
    for name in names:
        pattern = re.compile(name, re.IGNORECASE)
        for role in ("button", "link"):
            try:
                locator = page.get_by_role(role, name=pattern)
                if locator.count() < 1:
                    continue
                locator.first.click(timeout=timeout_ms, no_wait_after=no_wait_after)
                return True
            except Exception:
                continue
    return False


def _dismiss_cookie_banners(page: Any) -> None:
    # Short timeouts + no_wait_after: Business Suite often has no banner, and a
    # broad "Accept all" match can otherwise wait on a long navigation.
    _click_named_control(
        page,
        COOKIE_BANNER_NAMES,
        timeout_ms=1_500,
        no_wait_after=True,
    )


def _screenshot_on_failure(page: Any, destination: Path | None) -> None:
    if destination is None:
        return
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(destination), full_page=True)
    except Exception:
        pass


def save_browser_session_interactive(
    browser_state_path: Path,
    *,
    browser_profile_dir: Path,
    page_username: str,
    browser_channel: str | None = "chrome",
    proxy: dict[str, str] | None = None,
) -> None:
    """Open Business Suite in Chrome/Edge; operator logs in and saves the session."""
    sync_playwright = _require_playwright()
    browser_state_path.parent.mkdir(parents=True, exist_ok=True)
    start_url = business_suite_composer_url()
    proxy = proxy if proxy is not None else resolve_facebook_browser_proxy()

    last_error: Exception | None = None
    for channel in _browser_channel_candidates(browser_channel):
        label = _channel_label(channel)
        try:
            with sync_playwright() as playwright:
                context = _launch_persistent_context(
                    playwright,
                    browser_profile_dir,
                    channel=channel,
                    headless=False,
                    proxy=proxy,
                )
                page = context.pages[0] if context.pages else context.new_page()
                print(f"Opened Meta Business Suite login using {label}.")
                print(f"Proxy: {_proxy_log_label(proxy)}")
                print(f"1. Log in as a Page admin for {page_username!r}.")
                print("2. Confirm the Page is selected and the composer loads.")
                print("3. When ready, press Enter here to save the session.")
                page.goto(start_url, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
                _settle_page(page)
                _dismiss_cookie_banners(page)
                input()
                if _page_shows_login_wall(page):
                    raise FacebookWebError(
                        "Still on a Facebook login/checkpoint page. "
                        "Finish login, then press Enter again after re-running."
                    )
                context.storage_state(path=str(browser_state_path))
                context.close()
            print(f"Saved Facebook browser session to {browser_state_path}.")
            return
        except FacebookWebError:
            raise
        except Exception as exc:
            last_error = exc
            continue

    raise FacebookWebError(
        "Could not launch Chrome or Edge for Facebook login. "
        "Install Google Chrome or Microsoft Edge, then retry. "
        f"Last error: {last_error}"
    ) from last_error


def verify_facebook_browser_proxy(
    *,
    proxy: dict[str, str] | None = None,
    browser_channel: str | None = None,
    headless: bool = True,
    check_url: str = "https://api.ipify.org/?format=json",
) -> str:
    """Launch Chromium through the configured proxy and return the egress IP.

    Raises ``FacebookWebError`` when no proxy is configured or the check fails.
    """
    resolved = proxy if proxy is not None else resolve_facebook_browser_proxy()
    if not resolved:
        raise FacebookWebError(
            "No proxy configured. Set FACEBOOK_BROWSER_PROXY="
            "http://USER:PASS@p.webshare.io:80 (Webshare Proxy List credentials)."
        )
    sync_playwright = _require_playwright()
    channel = (
        browser_channel
        if browser_channel is not None
        else resolve_facebook_browser_channel()
    )
    last_error: Exception | None = None
    for candidate in _browser_channel_candidates(channel):
        try:
            with sync_playwright() as playwright:
                launch_kwargs: dict[str, Any] = {
                    "headless": headless,
                    "proxy": resolved,
                }
                if candidate:
                    launch_kwargs["channel"] = candidate
                browser = playwright.chromium.launch(**launch_kwargs)
                try:
                    page = browser.new_page()
                    print(f"Proxy check via {_proxy_log_label(resolved)}…", flush=True)
                    page.goto(check_url, wait_until="domcontentloaded", timeout=30_000)
                    body = (page.inner_text("body") or "").strip()
                finally:
                    browser.close()
            ip = body
            try:
                payload = json.loads(body)
                if isinstance(payload, dict) and payload.get("ip"):
                    ip = str(payload["ip"])
            except json.JSONDecodeError:
                pass
            if not ip:
                raise FacebookWebError("Proxy check returned an empty body")
            print(f"Egress IP: {ip}", flush=True)
            return ip
        except FacebookWebError:
            raise
        except Exception as exc:
            last_error = exc
            continue
    raise FacebookWebError(
        f"Proxy check failed. Last error: {last_error}"
    ) from last_error


def _normalize_same_site(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "Lax"
    normalized = value.strip().lower().replace("_", " ")
    if normalized in {"none", "no restriction", "no_restriction"}:
        return "None"
    if normalized == "strict":
        return "Strict"
    return "Lax"


def _normalize_browser_cookie(raw: dict[str, Any]) -> dict[str, Any] | None:
    name = raw.get("name")
    value = raw.get("value")
    if not isinstance(name, str) or not isinstance(value, str) or not name:
        return None

    domain = raw.get("domain")
    if not isinstance(domain, str) or not domain.strip():
        return None

    path = raw.get("path")
    if not isinstance(path, str) or not path:
        path = "/"

    expires = raw.get("expires")
    if expires is None and raw.get("expirationDate") is not None:
        expiration = raw.get("expirationDate")
        if isinstance(expiration, (int, float)):
            expires = float(expiration)
    if isinstance(expires, (int, float)) and expires > 0:
        expires_value = float(expires)
    else:
        expires_value = -1.0

    return {
        "name": name,
        "value": value,
        "domain": domain.strip(),
        "path": path,
        "expires": expires_value,
        "httpOnly": bool(raw.get("httpOnly", False)),
        "secure": bool(raw.get("secure", True)),
        "sameSite": _normalize_same_site(raw.get("sameSite")),
    }


def cookies_from_import_payload(payload: Any) -> list[dict[str, Any]]:
    """Accept Playwright storage-state or Cookie-Editor JSON array."""
    if isinstance(payload, dict):
        if isinstance(payload.get("cookies"), list):
            raw_cookies = payload["cookies"]
        else:
            raise FacebookWebError(
                "Expected a JSON array of cookies (Cookie-Editor export) or a "
                "Playwright storage-state object with a top-level 'cookies' array."
            )
    elif isinstance(payload, list):
        raw_cookies = payload
    else:
        raise FacebookWebError(
            "Expected a JSON array of cookies (Cookie-Editor export) or a "
            "Playwright storage-state object."
        )

    cookies: list[dict[str, Any]] = []
    for raw in raw_cookies:
        if not isinstance(raw, dict):
            continue
        normalized = _normalize_browser_cookie(raw)
        if normalized is not None:
            cookies.append(normalized)
    if not cookies:
        raise FacebookWebError("No usable cookies found in the import file.")
    return cookies


def import_browser_session(source_path: Path, destination_path: Path) -> None:
    if not source_path.exists():
        raise FacebookWebError(f"Session file not found: {source_path}")
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    cookies = cookies_from_import_payload(payload)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.write_text(
        json.dumps({"cookies": cookies, "origins": []}, indent=2) + "\n",
        encoding="utf-8",
    )


def _ensure_session_ready(page: Any) -> None:
    if _page_shows_login_wall(page):
        raise FacebookWebError(
            "Facebook browser session is logged out (login wall). "
            "Re-export cookies from a normal Chrome tab where you are logged in "
            "(Cookie-Editor → Export → JSON), then run:\n"
            "  python -m media_publisher --facebook-browser-import-session storage-state.json\n"
            "If credentials/facebook-browser-profile exists from a failed login, delete it "
            "so the imported cookies are used."
        )


def _caption_text_present(page: Any, caption: str) -> bool:
    """True if any composer textbox currently shows the start of ``caption``."""
    text = (caption or "").strip()
    if not text:
        return False
    probe = text[: min(24, len(text))]

    def _check(target: Any) -> bool:
        try:
            return bool(
                target.evaluate(
                    """(probe) => {
                      const nodes = document.querySelectorAll(
                        '[contenteditable="true"], textarea, [role="textbox"]'
                      );
                      for (const el of Array.from(nodes).slice(0, 40)) {
                        const role = (el.getAttribute('role') || '').toLowerCase();
                        if (role === 'combobox') continue;
                        const t = (el.innerText || el.textContent || el.value || '')
                          .replace(/\\u00a0/g, ' ')
                          .trim();
                        if (t.includes(probe)) return true;
                      }
                      return false;
                    }""",
                    probe,
                )
            )
        except Exception:
            return False

    if _check(page):
        return True
    try:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            if _check(frame):
                return True
    except Exception:
        pass
    return False


def _iter_page_and_frames(page: Any) -> list[Any]:
    targets = [page]
    try:
        for frame in page.frames:
            if frame != page.main_frame:
                targets.append(frame)
    except Exception:
        pass
    return targets


def _activate_caption_surface(page: Any) -> None:
    """Click common caption placeholders so Lexical mounts the editor."""
    patterns = (
        r"Write something",
        r"Start typing",
        r"What.?s on your mind",
        r"Create a post",
        r"Say something",
        r"Напишете",
        r"Какво мислите",
    )
    for target in _iter_page_and_frames(page):
        for pattern in patterns:
            try:
                loc = target.get_by_text(re.compile(pattern, re.I))
                if loc.count() > 0:
                    loc.first.click(timeout=2_000, no_wait_after=True)
                    page.wait_for_timeout(400)
                    return
            except Exception:
                continue
            try:
                token = pattern.split()[0].replace(r".?", "")
                loc = target.locator(
                    f'[aria-placeholder*="{token}" i], [placeholder*="{token}" i]'
                )
                if loc.count() > 0:
                    loc.first.click(timeout=2_000, no_wait_after=True)
                    page.wait_for_timeout(400)
                    return
            except Exception:
                continue
    try:
        page.evaluate(
            """() => {
              const main = document.querySelector('[role="main"]') || document.body;
              const r = main.getBoundingClientRect();
              const x = r.left + Math.min(r.width * 0.45, 420);
              const y = r.top + Math.min(260, r.height * 0.35);
              const el = document.elementFromPoint(x, y);
              if (el && el.click) el.click();
            }"""
        )
        page.wait_for_timeout(400)
    except Exception:
        pass


def _pick_caption_box(page: Any) -> Any | None:
    """Return a Playwright locator for the caption editor, if present."""
    selectors = (
        '[data-lexical-editor="true"][contenteditable="true"]',
        'div[role="textbox"][contenteditable="true"]',
        'div[aria-placeholder*="Write" i][contenteditable="true"]',
        'div[aria-placeholder*="Start" i][contenteditable="true"]',
        'div[aria-placeholder*="what" i][contenteditable="true"]',
        'div[aria-placeholder*="Say" i][contenteditable="true"]',
        'div[aria-placeholder*="Напиш" i][contenteditable="true"]',
        'p[contenteditable="true"]',
        'div[contenteditable="true"]',
        "textarea",
    )
    best = None
    best_score = -999
    for target in _iter_page_and_frames(page):
        for sel in selectors:
            try:
                locator = target.locator(sel)
                count = locator.count()
            except Exception:
                continue
            for i in range(min(count, 12)):
                box = locator.nth(i)
                try:
                    if not box.is_visible(timeout=400):
                        continue
                    role = (box.get_attribute("role") or "").casefold()
                    if role == "combobox":
                        continue
                    label = (
                        (box.get_attribute("aria-placeholder") or "")
                        + " "
                        + (box.get_attribute("placeholder") or "")
                        + " "
                        + (box.get_attribute("aria-label") or "")
                    ).casefold()
                    score = 0
                    if any(
                        token in label
                        for token in ("write", "start", "what", "say", "напиш", "мисл")
                    ):
                        score += 6
                    if "search" in label or "comment" in label or "message" in label:
                        score -= 10
                    if box.get_attribute("data-lexical-editor") == "true":
                        score += 4
                    if role == "textbox":
                        score += 2
                    geom = box.bounding_box() or {}
                    height = float(geom.get("height") or 0)
                    width = float(geom.get("width") or 0)
                    if height >= 36:
                        score += 2
                    if width >= 200:
                        score += 1
                    if height < 18 or width < 80:
                        score -= 5
                    if score > best_score:
                        best_score = score
                        best = box
                except Exception:
                    continue
    return best


def _dump_caption_debug(page: Any) -> None:
    try:
        frames = []
        for idx, target in enumerate(_iter_page_and_frames(page)):
            try:
                diag = target.evaluate(
                    """() => Array.from(
                      document.querySelectorAll(
                        '[contenteditable], [role="textbox"], textarea, [data-lexical-editor], [aria-placeholder]'
                      )
                    ).slice(0, 20).map((el) => ({
                      tag: el.tagName,
                      role: el.getAttribute('role'),
                      lexical: el.getAttribute('data-lexical-editor'),
                      ce: el.getAttribute('contenteditable'),
                      ph: (el.getAttribute('aria-placeholder') || el.getAttribute('placeholder') || '').slice(0, 80),
                      text: ((el.innerText || el.textContent || '').trim()).slice(0, 60),
                      w: Math.round(el.getBoundingClientRect().width),
                      h: Math.round(el.getBoundingClientRect().height),
                    }))"""
                )
                frames.append({"frame": idx, "nodes": diag})
            except Exception as exc:
                frames.append({"frame": idx, "error": str(exc)})
        print(f"  caption debug: {frames!r}", flush=True)
    except Exception as exc:
        print(f"  caption debug failed: {exc}", flush=True)


def _fill_composer_caption(page: Any, caption: str) -> None:
    """Fill the Business Suite composer caption (Lexical contenteditable).

    Lexical ignores plain ``textContent`` assignment. Prefer Playwright ``fill``,
    then clipboard paste, then keyboard typing — and verify the text stuck.
    """
    text = caption.strip()
    if not text:
        raise FacebookWebError("Facebook photo post caption is required")

    page.wait_for_timeout(800)
    last_error: Exception | None = None

    for attempt in range(4):
        _activate_caption_surface(page)
        box = _pick_caption_box(page)
        if box is None:
            last_error = FacebookWebError("No caption textbox found in the composer")
            _refocus_composer_caption(page)
            page.wait_for_timeout(600)
            continue
        try:
            box.click(timeout=5_000)
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.wait_for_timeout(150)

            try:
                box.fill(text, timeout=5_000)
                if _caption_text_present(page, text):
                    return
            except Exception as exc:
                last_error = exc

            try:
                page.context.grant_permissions(
                    ["clipboard-read", "clipboard-write"],
                    origin="https://business.facebook.com",
                )
            except Exception:
                pass
            try:
                box.click(timeout=3_000)
                page.evaluate(
                    """async (value) => {
                      try {
                        await navigator.clipboard.writeText(value);
                        return 'clipboard';
                      } catch (e) {
                        const ta = document.createElement('textarea');
                        ta.value = value;
                        document.body.appendChild(ta);
                        ta.select();
                        document.execCommand('copy');
                        ta.remove();
                        return 'textarea';
                      }
                    }""",
                    text,
                )
                page.keyboard.press("Control+V")
                page.wait_for_timeout(400)
                if _caption_text_present(page, text):
                    return
            except Exception as exc:
                last_error = exc

            try:
                box.click(timeout=3_000)
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                try:
                    box.press_sequentially(text, delay=8)
                except Exception:
                    page.keyboard.type(text, delay=8)
                page.wait_for_timeout(400)
                if _caption_text_present(page, text):
                    return
            except Exception as exc:
                last_error = exc

            try:
                inserted = bool(
                    page.evaluate(
                        """(text) => {
                          const el = document.activeElement;
                          if (!el || el.getAttribute('contenteditable') !== 'true') {
                            return false;
                          }
                          el.focus();
                          try { document.execCommand('selectAll', false); } catch (e) {}
                          try {
                            return document.execCommand('insertText', false, text);
                          } catch (e) {
                            return false;
                          }
                        }""",
                        text,
                    )
                )
                page.wait_for_timeout(300)
                if inserted and _caption_text_present(page, text):
                    return
            except Exception as exc:
                last_error = exc
        except Exception as exc:
            last_error = exc
        page.wait_for_timeout(500 + attempt * 300)

    _dump_caption_debug(page)
    raise FacebookWebError(
        "Could not fill the Facebook post caption textbox. "
        f"Last error: {last_error}"
    ) from last_error

def _click_add_photo_control(page: Any) -> bool:
    # Prefer bounded JS click — role/name scans hang on Business Suite.
    try:
        clicked = bool(
            _eval_bounded(
                page,
                """() => {
                  const nodes = document.querySelectorAll('button, [role="button"]');
                  const n = Math.min(nodes.length, 200);
                  for (let i = 0; i < n; i++) {
                    const el = nodes[i];
                    const text = (
                      (el.getAttribute('aria-label') || '') + ' ' + (el.innerText || '')
                    ).replace(/\\s+/g, ' ').trim();
                    if (
                      /^Add photo\\/video$/i.test(text) ||
                      /^Add photos\\/videos$/i.test(text) ||
                      /^Photo\\/video$/i.test(text)
                    ) {
                      el.click();
                      return true;
                    }
                  }
                  return false;
                }""",
                timeout_ms=8_000,
            )
        )
        if clicked:
            return True
    except Exception:
        pass
    return False


def _attach_photo(page: Any, image_path: Path) -> None:
    """Attach an image via Business Suite 'Add photo/video' (file chooser or input)."""
    path = image_path.resolve()
    if not path.is_file():
        raise FacebookWebError(f"Image file not found: {path}")

    # Prefer evaluate + set_input_files — wait_for_selector often hangs on this SPA
    # even when evaluate/wait_for_function still work.
    print("  attach: probing file input…", flush=True)
    try:
        count = int(
            page.evaluate("() => document.querySelectorAll('input[type=\"file\"]').length")
        )
    except Exception:
        count = 0
    print(f"  attach: file inputs={count}", flush=True)

    if count < 1:
        print("  attach: clicking Add photo/video…", flush=True)
        _click_add_photo_control(page)
        page.wait_for_timeout(800)
        try:
            count = int(
                page.evaluate(
                    "() => document.querySelectorAll('input[type=\"file\"]').length"
                )
            )
        except Exception:
            count = 0
        print(f"  attach: file inputs after click={count}", flush=True)

    if count > 0:
        try:
            page.set_input_files('input[type="file"]', str(path))
            page.wait_for_timeout(SPA_SETTLE_MS)
            print("  attached via file input", flush=True)
            return
        except Exception as exc:
            print(f"  attach: set_input_files failed: {exc}", flush=True)

    print("  attach: trying native file chooser…", flush=True)
    try:
        with page.expect_file_chooser(timeout=8_000) as chooser_info:
            if not _click_add_photo_control(page):
                raise TimeoutError("Add photo/video control not found")
        chooser_info.value.set_files(str(path))
        page.wait_for_timeout(SPA_SETTLE_MS)
        print("  attached via file chooser", flush=True)
        return
    except FacebookWebError:
        raise
    except Exception as exc:
        raise FacebookWebError(
            "Could not attach photo: no usable file input and Add photo/video "
            f"did not open a file chooser ({exc})."
        ) from exc


def _select_business_suite_page(page: Any, *, page_username: str) -> None:
    """Select the target Page in Business Suite asset pickers when shown.

    Skips when the composer already has the Page selected (typical with asset_id URL).
    """
    if _composer_editor_visible(page):
        return
    username = page_username.strip().lstrip("@")
    if not username:
        return
    patterns = (
        re.compile(re.escape(username), re.I),
        re.compile(r"SadhguruBulgarian|Садгуру България", re.I),
    )
    for pattern in patterns:
        try:
            locator = page.get_by_role("button", name=pattern)
            if locator.count() < 1:
                locator = page.get_by_text(pattern)
            if locator.count() < 1:
                continue
            locator.first.click(timeout=3_000)
            page.wait_for_timeout(1_000)
            return
        except Exception:
            continue


def _composer_editor_visible(page: Any) -> bool:
    """Treat loaded Business Suite composer URL as ready.

    Full DOM text scans hang on this SPA; URL path + short settle is enough.
    Match the path only — login redirects put ``composer`` in ``?next=``.
    """
    parsed = urlparse(page.url or "")
    host = (parsed.netloc or "").casefold()
    path = (parsed.path or "").casefold()
    if _looks_like_login_url(page.url or ""):
        return False
    return "business.facebook.com" in host and "/latest/composer" in path


def _wait_for_composer_interactive(page: Any, *, timeout_ms: int = 45_000) -> None:
    """Wait until the composer DOM responds (file input or Add photo control)."""
    print("  waiting for composer controls…", flush=True)
    ready = _click_via_wait_for_function(
        page,
        """() => {
          if (document.querySelector('input[type="file"]')) return true;
          const nodes = document.querySelectorAll('button, [role="button"]');
          const n = Math.min(nodes.length, 200);
          for (let i = 0; i < n; i++) {
            const t = (
              (nodes[i].getAttribute('aria-label') || '') + ' ' + (nodes[i].innerText || '')
            ).replace(/\\s+/g, ' ').trim();
            if (/add photo\\/video/i.test(t) || /add photos\\/videos/i.test(t)) return true;
          }
          return false;
        }""",
        timeout_ms=timeout_ms,
    )
    if not ready:
        raise FacebookWebError(
            "Business Suite composer loaded but controls never became interactive. "
            "Close other Chrome windows and retry."
        )
    print("  composer controls ready", flush=True)


def _control_is_checked(locator: Any) -> bool:
    try:
        aria = locator.get_attribute("aria-checked")
        if aria is not None:
            return aria == "true"
    except Exception:
        pass
    try:
        pressed = locator.get_attribute("aria-pressed")
        if pressed is not None:
            return pressed == "true"
    except Exception:
        pass
    try:
        return bool(locator.is_checked())
    except Exception:
        return False


def _eval_bounded(page: Any, expression: str, arg: Any = None, *, timeout_ms: int = 8_000) -> Any:
    """Run page.evaluate; prefer wait_for_function when a timeout is needed.

    Playwright 1.61 has no evaluate(timeout=...), and greenlet forbids worker threads.
    """
    del timeout_ms  # kept for call-site compatibility; see wait_for_function helpers
    return page.evaluate(expression, arg)


def _click_via_wait_for_function(
    page: Any,
    expression: str,
    arg: Any = None,
    *,
    timeout_ms: int = 12_000,
) -> bool:
    """Poll in-page until expression returns truthy (has a real timeout)."""
    try:
        page.wait_for_function(expression, arg=arg, timeout=timeout_ms)
        return True
    except Exception:
        return False


def _disable_instagram_destination(page: Any) -> None:
    """Unselect Instagram in Business Suite 'Post to' so only Facebook is used.

    Post to is a role=combobox whose listbox options use aria-selected (not
    role=checkbox). Raises if Instagram cannot be deselected.
    """
    print("  excluding Instagram destination…", flush=True)

    if os.getenv("FACEBOOK_BROWSER_CONFIRM_DESTINATIONS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        print(
            "  Uncheck Instagram in the Post to dropdown, then press Enter here…",
            flush=True,
        )
        input()
        return

    ig_username = (
        os.getenv("META_INSTAGRAM_USERNAME", "sadhguru.bulgarian").strip().lstrip("@")
    )
    ig_key = ig_username.casefold()

    # 1) Open Post to combobox until the IG option is visible.
    try:
        combo = page.locator('[role="combobox"]').filter(
            has_text=re.compile(re.escape(ig_username), re.I)
        )
        if combo.count() < 1:
            combo = page.locator('[role="combobox"]')
        if combo.count() < 1:
            raise FacebookWebError("Post to combobox not found")
        combo.first.click(timeout=8_000, no_wait_after=True)
    except FacebookWebError:
        raise
    except Exception as exc:
        raise FacebookWebError(f"Could not click Post to combobox: {exc}") from exc

    opened = _click_via_wait_for_function(
        page,
        """(ig) => {
          const opts = document.querySelectorAll('[role="option"]');
          for (const el of opts) {
            const t = (
              (el.getAttribute('aria-label') || '') + ' ' + (el.innerText || '')
            ).toLowerCase();
            if (t.includes(ig) || t.includes('instagram')) return true;
          }
          return false;
        }""",
        ig_key,
        timeout_ms=10_000,
    )
    print(f"  post-to open: {opened}", flush=True)
    if not opened:
        raise FacebookWebError(
            "Could not open Business Suite Post to listbox to exclude Instagram."
        )
    page.wait_for_timeout(300)

    try:
        Path("downloads").mkdir(parents=True, exist_ok=True)
        page.screenshot(path="downloads/fb-postto-after-open.png", timeout=5_000)
    except Exception:
        pass

    # 2) Deselect the IG option (aria-selected=true -> false).
    unchecked = _click_via_wait_for_function(
        page,
        """(ig) => {
          const opts = document.querySelectorAll('[role="option"]');
          for (const el of opts) {
            const t = (
              (el.getAttribute('aria-label') || '') + ' ' + (el.innerText || '')
            ).trim().toLowerCase();
            if (!(t.includes(ig) || t === 'instagram' || t.includes('instagram'))) {
              continue;
            }
            const selected = el.getAttribute('aria-selected');
            if (selected === 'false') return true;
            el.click();
            return false;
          }
          return false;
        }""",
        ig_key,
        timeout_ms=12_000,
    )
    print(f"  ig uncheck: {unchecked}", flush=True)

    try:
        state = page.evaluate(
            """() => Array.from(document.querySelectorAll('[role="option"]'))
              .slice(0, 10)
              .map(el => ({
                text: ((el.getAttribute('aria-label') || '') + ' ' + (el.innerText || ''))
                  .trim().replace(/\\s+/g, ' ').slice(0, 100),
                selected: el.getAttribute('aria-selected'),
              }))"""
        )
        print(f"  option state: {json.dumps(state, ensure_ascii=True)}", flush=True)
        Path("downloads").mkdir(parents=True, exist_ok=True)
        Path("downloads/fb-postto-options.json").write_text(
            json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"  option dump failed: {exc}", flush=True)

    if not unchecked:
        raise FacebookWebError(
            "Could not deselect Instagram option in Business Suite Post to listbox."
        )

    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
    except Exception:
        pass

    # 3) Confirm combobox no longer lists the IG handle.
    try:
        chip_text = (
            page.evaluate(
                """() => {
                  const el = document.querySelector('[role="combobox"]');
                  if (!el) return '';
                  return ((el.getAttribute('aria-label') || '') + ' ' + (el.innerText || ''))
                    .toLowerCase();
                }"""
            )
            or ""
        )
    except Exception:
        chip_text = ""
    print(
        "  combobox text after: "
        + repr(chip_text.encode("ascii", "replace").decode("ascii")),
        flush=True,
    )

    try:
        page.screenshot(path="downloads/fb-postto-after-uncheck.png", timeout=5_000)
    except Exception:
        pass

    if ig_key in chip_text:
        raise FacebookWebError(
            "Deselected Instagram option but Post to combobox still lists the IG account."
        )

    print("  Instagram destination unchecked", flush=True)
    # Closing Post-to can remount Lexical; click the composer body so the
    # caption editor comes back before we fill/schedule.
    _refocus_composer_caption(page)


def _refocus_composer_caption(page: Any) -> None:
    """Click away from Post-to so the Lexical caption editor remounts."""
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    page.wait_for_timeout(400)
    clicked = False
    try:
        clicked = bool(
            page.evaluate(
                """() => {
                  const nodes = Array.from(
                    document.querySelectorAll(
                      'div[role="textbox"][contenteditable="true"], [data-lexical-editor="true"], div[contenteditable="true"]'
                    )
                  );
                  for (const el of nodes) {
                    const ph = (
                      (el.getAttribute('aria-placeholder') || '') +
                      ' ' +
                      (el.getAttribute('aria-label') || '')
                    ).toLowerCase();
                    if (/combobox|search|comment/.test(ph)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 100 || r.height < 24) continue;
                    el.click();
                    return true;
                  }
                  // Fallback: click a large main/content region under the composer.
                  const main = document.querySelector('[role="main"]') || document.body;
                  if (main) {
                    const r = main.getBoundingClientRect();
                    const x = Math.min(r.left + r.width / 2, r.right - 20);
                    const y = Math.min(r.top + 220, r.bottom - 20);
                    const target = document.elementFromPoint(x, y);
                    if (target && typeof target.click === 'function') {
                      target.click();
                      return true;
                    }
                  }
                  return false;
                }"""
            )
        )
    except Exception:
        clicked = False
    print(f"  refocus caption editor: {clicked}", flush=True)
    page.wait_for_timeout(700)


def _open_business_suite_composer(page: Any, *, page_username: str) -> None:
    """Open Meta Business Suite create-post composer in a single navigation."""
    composer_url = business_suite_composer_url()
    print(f"  goto {composer_url}", flush=True)
    page.goto(composer_url, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
    print(f"  loaded {page.url}", flush=True)
    page.wait_for_timeout(SPA_SETTLE_MS)
    print("  checking login wall…", flush=True)
    if _page_shows_login_wall(page):
        raise FacebookWebError(
            "Meta Business Suite shows a login wall with the current browser session "
            "(expired cookies, or Meta blocked the login — common from GitHub Actions IPs). "
            "In Facebook: open Security / Login alerts, allow the attempt if shown, then "
            "re-login locally (`python -m media_publisher --facebook-browser-login` or "
            "Cookie-Editor export), update FACEBOOK_BROWSER_STATE_JSON, and avoid rapid "
            "CI retries until the session is trusted again."
        )

    print("  checking composer editor…", flush=True)
    if _composer_editor_visible(page):
        _wait_for_composer_interactive(page)
        print("  composer ready", flush=True)
        return

    _select_business_suite_page(page, page_username=page_username)
    if _composer_editor_visible(page):
        _wait_for_composer_interactive(page)
        print("  composer ready after page select", flush=True)
        return

    opened = _click_named_control(
        page,
        (r"Create post", r"Create a post", r"^Post$"),
        timeout_ms=5_000,
    )
    if opened:
        page.wait_for_timeout(SPA_SETTLE_MS)
    if _composer_editor_visible(page):
        _wait_for_composer_interactive(page)
        print("  composer ready after Create post click", flush=True)
        return

    raise FacebookWebError(
        "Opened Meta Business Suite but the create-post editor was not visible. "
        f"Confirm this URL works in Chrome: {composer_url}"
    )


def _click_post(page: Any) -> None:
    if _click_named_control(page, POST_BUTTON_NAMES, timeout_ms=15_000):
        return
    # Dialog-scoped fallback (composer often in a dialog/modal).
    dialog = page.locator('[role="dialog"]')
    if dialog.count() > 0:
        for name in POST_BUTTON_NAMES:
            pattern = re.compile(name, re.IGNORECASE)
            try:
                btn = dialog.last.get_by_role("button", name=pattern)
                if btn.count() > 0:
                    btn.first.click(timeout=15_000)
                    return
            except Exception:
                continue
    raise FacebookWebError("Could not find the Facebook Post/Publish button")


def _composer_root(page: Any) -> Any:
    dialog = page.locator('[role="dialog"]')
    if dialog.count() > 0:
        return dialog.last
    return page


def _enable_business_suite_schedule_toggle(page: Any) -> bool:
    """Turn on Business Suite 'Set date and time' if present."""
    for name in SCHEDULE_TOGGLE_NAMES:
        pattern = re.compile(name, re.IGNORECASE)
        try:
            toggle = page.get_by_role("switch", name=pattern)
            if toggle.count() > 0:
                switch = toggle.first
                checked = switch.get_attribute("aria-checked")
                if checked != "true":
                    switch.click(timeout=8_000)
                    page.wait_for_timeout(SPA_SETTLE_MS)
                return True
        except Exception:
            continue
        try:
            label = page.get_by_text(pattern)
            if label.count() < 1:
                continue
            # Click the nearby switch/checkbox.
            row = label.first.locator(
                "xpath=ancestor::*[.//*[@role='switch' or @role='checkbox']][1]"
            )
            switch = row.locator('[role="switch"], [role="checkbox"]').first
            checked = switch.get_attribute("aria-checked")
            if checked != "true":
                switch.click(timeout=8_000)
                page.wait_for_timeout(SPA_SETTLE_MS)
            return True
        except Exception:
            continue
    return False


def _open_schedule_panel(page: Any) -> None:
    """Open schedule controls (Business Suite toggle first, then classic menus)."""
    if _enable_business_suite_schedule_toggle(page):
        return

    root = _composer_root(page)
    if _click_named_control(page, SCHEDULE_MENU_NAMES, timeout_ms=5_000):
        page.wait_for_timeout(SPA_SETTLE_MS)
        return

    for name in SCHEDULE_MENU_NAMES:
        pattern = re.compile(name, re.IGNORECASE)
        try:
            btn = root.get_by_role("button", name=pattern)
            if btn.count() > 0:
                btn.first.click(timeout=5_000)
                page.wait_for_timeout(SPA_SETTLE_MS)
                return
        except Exception:
            continue

    raise FacebookWebError(
        "Could not open Facebook schedule controls "
        "(missing 'Set date and time' toggle / Schedule menu)."
    )


def _fill_first_matching_input(root: Any, selectors: tuple[str, ...], value: str) -> bool:
    for selector in selectors:
        try:
            locator = root.locator(selector)
            if locator.count() < 1:
                continue
            field = locator.first
            field.click(timeout=5_000)
            field.fill(value)
            return True
        except Exception:
            continue
    return False


def _set_schedule_datetime(
    page: Any,
    publish_at: datetime,
    *,
    display_timezone: str,
) -> None:
    from media_publisher.timezones import get_timezone

    local = _as_utc(publish_at).astimezone(get_timezone(display_timezone))
    iso_date = local.strftime("%Y-%m-%d")
    us_date = local.strftime("%m/%d/%Y")
    time_24 = local.strftime("%H:%M")
    time_12 = local.strftime("%I:%M %p").lstrip("0")

    root = _composer_root(page)

    date_ok = _fill_first_matching_input(root, ('input[type="date"]',), iso_date)
    if not date_ok:
        date_ok = _fill_first_matching_input(
            root,
            (
                'input[placeholder*="mm/dd" i]',
                'input[placeholder*="Date" i]',
                'input[aria-label*="Date" i]',
                'input[name*="date" i]',
            ),
            us_date,
        )

    time_ok = _fill_first_matching_input(root, ('input[type="time"]',), time_24)
    if not time_ok:
        time_ok = _fill_first_matching_input(
            root,
            (
                'input[placeholder*="Time" i]',
                'input[aria-label*="Time" i]',
                'input[name*="time" i]',
            ),
            time_12,
        )

    if not date_ok or not time_ok:
        raise FacebookWebError(
            f"Could not fill Facebook schedule date/time "
            f"(wanted {us_date} {time_12} {display_timezone}). "
            f"date_ok={date_ok} time_ok={time_ok}"
        )


def _confirm_schedule(page: Any) -> None:
    if _click_named_control(page, SCHEDULE_CONFIRM_NAMES, timeout_ms=10_000):
        return
    root = _composer_root(page)
    for name in SCHEDULE_CONFIRM_NAMES:
        pattern = re.compile(name, re.IGNORECASE)
        try:
            btn = root.get_by_role("button", name=pattern)
            if btn.count() > 0:
                # Prefer the bottom confirm Schedule over the entry control.
                btn.last.click(timeout=10_000)
                return
        except Exception:
            continue
    raise FacebookWebError("Could not confirm Facebook scheduled post")


def _set_business_suite_schedule_values(
    page: Any,
    publish_at: datetime,
    *,
    display_timezone: str,
) -> None:
    """Set date/time on Business Suite schedule row (mm/dd input + time spinbuttons)."""
    from media_publisher.timezones import get_timezone

    local = _as_utc(publish_at).astimezone(get_timezone(display_timezone))
    # Meta's date field accepts mm/dd/yyyy and displays as "Mon D, YYYY".
    date_typed = local.strftime("%m/%d/%Y")
    hour_12 = local.strftime("%I").lstrip("0") or "12"
    minute = local.strftime("%M")
    ampm = local.strftime("%p").upper()

    print(
        f"  setting schedule to {date_typed} {hour_12}:{minute} {ampm} "
        f"({display_timezone})…",
        flush=True,
    )

    date_input = page.locator('input[placeholder*="mm/dd" i]')
    if date_input.count() < 1:
        raise FacebookWebError(
            "Business Suite schedule date input (placeholder mm/dd/yyyy) not found."
        )
    field = date_input.first
    field.click(timeout=8_000)
    try:
        field.fill("")
    except Exception:
        pass
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.keyboard.type(date_typed, delay=30)
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)

    try:
        current_date = (field.input_value() or "").strip()
    except Exception:
        current_date = ""
    print(f"  date field now: {current_date!r}", flush=True)
    # Accept typed mm/dd/yyyy (with or without leading zeros) or "Oct 1, 2026".
    normalized = current_date.replace(" ", "").casefold()
    date_ok = (
        normalized in {date_typed.casefold(), date_typed.lstrip("0").replace("/0", "/").casefold()}
        or (
            f"{int(local.month)}/{local.day}/{local.year}" == normalized
            or f"{local.month:02d}/{local.day}/{local.year}" == normalized
            or f"{local.month:02d}/{local.day:02d}/{local.year}" == normalized
        )
        or (
            local.strftime("%b")[:3].lower() in current_date.casefold()
            and str(local.day) in current_date
            and str(local.year) in current_date
        )
    )
    if not date_ok:
        # One more attempt with "Oct 1, 2026" style.
        try:
            pretty = local.strftime("%b %#d, %Y")
        except ValueError:
            pretty = local.strftime("%b %d, %Y").replace(" 0", " ")
        field.click(timeout=5_000)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.keyboard.type(pretty, delay=30)
        page.keyboard.press("Enter")
        page.wait_for_timeout(500)
        try:
            current_date = (field.input_value() or "").strip()
        except Exception:
            current_date = ""
        print(f"  date field after pretty: {current_date!r}", flush=True)
        date_ok = (
            str(local.year) in current_date
            and str(local.day) in current_date
            and local.strftime("%b")[:3].lower() in current_date.casefold()
        )
    if not date_ok:
        raise FacebookWebError(
            f"Could not set Business Suite schedule date to {date_typed} "
            f"(field shows {current_date!r})."
        )

    # Time is a group of spinbuttons: hours / minutes / (optional dayPeriod).
    hours = page.get_by_role("spinbutton", name=re.compile(r"^hours?$", re.I))
    minutes = page.get_by_role("spinbutton", name=re.compile(r"^minutes?$", re.I))
    if hours.count() < 1 or minutes.count() < 1:
        raise FacebookWebError(
            "Business Suite schedule time spinbuttons (hours/minutes) not found."
        )

    def _set_spin(locator: Any, value: str) -> None:
        box = locator.first
        box.click(timeout=5_000)
        page.keyboard.press("Control+A")
        page.keyboard.type(value, delay=40)
        page.keyboard.press("Tab")
        page.wait_for_timeout(200)

    _set_spin(hours, hour_12)
    _set_spin(minutes, minute)

    # AM/PM is a spinbutton named "meridiem" (aria-valuetext AM/PM, valuemin/max 0/1).
    meridiem = page.get_by_role("spinbutton", name=re.compile(r"^meridiem$", re.I))
    if meridiem.count() > 0:
        box = meridiem.first
        try:
            shown = (box.get_attribute("aria-valuetext") or "").strip().upper()
        except Exception:
            shown = ""
        if shown != ampm:
            box.click(timeout=5_000)
            page.keyboard.press("Control+A")
            page.keyboard.type(ampm[0], delay=40)  # A or P
            page.keyboard.press("Tab")
            page.wait_for_timeout(300)
            try:
                shown = (box.get_attribute("aria-valuetext") or "").strip().upper()
            except Exception:
                shown = ""
            if shown != ampm:
                # Toggle via arrow keys (0=AM, 1=PM on this control).
                wanted = 0 if ampm == "AM" else 1
                try:
                    now = int(box.get_attribute("aria-valuenow") or "-1")
                except Exception:
                    now = -1
                if now != wanted and now >= 0:
                    box.click(timeout=3_000)
                    page.keyboard.press("ArrowUp" if wanted > now else "ArrowDown")
                    page.wait_for_timeout(200)
    else:
        print("  warning: meridiem spinbutton not found; leaving AM/PM as-is", flush=True)

    page.wait_for_timeout(400)
    try:
        Path("downloads").mkdir(parents=True, exist_ok=True)
        page.screenshot(path="downloads/fb-schedule-set.png", timeout=5_000)
    except Exception:
        pass

    # Verify time spinbutton values when readable.
    try:
        hours_val = (hours.first.input_value() or hours.first.get_attribute("aria-valuenow") or "").strip()
        mins_val = (minutes.first.input_value() or minutes.first.get_attribute("aria-valuenow") or "").strip()
    except Exception:
        hours_val, mins_val = "", ""
    print(f"  time spinbuttons now: hours={hours_val!r} minutes={mins_val!r}", flush=True)
    if hours_val and hours_val.lstrip("0") != hour_12.lstrip("0"):
        # Soft check — some widgets keep aria-valuetext only.
        print("  warning: hours spinbutton value mismatch; continuing", flush=True)


def _schedule_composer_post(
    page: Any,
    publish_at: datetime,
    *,
    display_timezone: str,
) -> None:
    _open_schedule_panel(page)
    # Business Suite uses mm/dd/yyyy + time spinbuttons (not input[type=date/time]).
    try:
        _set_business_suite_schedule_values(
            page,
            publish_at,
            display_timezone=display_timezone,
        )
    except FacebookWebError as exc:
        print(f"  Business Suite schedule fields failed ({exc}); trying classic inputs…", flush=True)
        _set_schedule_datetime(page, publish_at, display_timezone=display_timezone)
    _confirm_schedule(page)


def _wait_for_schedule_success(page: Any, *, page_username: str) -> str:
    """Wait for schedule confirmation; return a useful reference URL."""
    # Avoid body.inner_text — it hangs on Business Suite. URL / short settle is enough.
    page.wait_for_timeout(4_000)
    return (
        f"https://www.facebook.com/{page_username.strip().lstrip('@')}"
        "/publishing_tools/?section=SCHEDULED_POSTS"
    )


def extract_facebook_post_permalink(haystack: str, *, page_username: str) -> str | None:
    """Best-effort permalink extraction from page HTML/URL text."""
    matches = POST_URL_PATTERN.findall(haystack)
    username = page_username.strip().lstrip("@").casefold()
    preferred: list[str] = []
    others: list[str] = []
    for match in matches:
        cleaned = match.rstrip(").,;'\"")
        if "story.php" in cleaned and "story_fbid=" not in cleaned:
            continue
        host_path = urlparse(cleaned).path.casefold()
        if username and username in host_path:
            preferred.append(cleaned)
        else:
            others.append(cleaned)
    for candidate in preferred + others:
        if "/posts/" in candidate or "story_fbid=" in candidate or "/permalink/" in candidate:
            return candidate
        if "/photos/" in candidate or "fbid=" in candidate:
            return candidate
    return preferred[0] if preferred else (others[0] if others else None)


def _wait_for_permalink(page: Any, *, page_username: str, before_html: str) -> str:
    deadline_ms = POST_TIMEOUT_MS
    steps = max(1, deadline_ms // 2_000)
    for _ in range(steps):
        page.wait_for_timeout(2_000)
        try:
            html = page.content()
        except Exception:
            html = ""
        combined = f"{page.url}\n{html}"
        permalink = extract_facebook_post_permalink(combined, page_username=page_username)
        if permalink and permalink not in before_html:
            return permalink
        # Soft success: composer dialog closed after posting.
        dialogs = page.locator('[role="dialog"]')
        if dialogs.count() == 0 and permalink:
            return permalink
    # Fall back to any post-looking URL, else page feed.
    try:
        html = page.content()
    except Exception:
        html = ""
    permalink = extract_facebook_post_permalink(
        f"{page.url}\n{html}",
        page_username=page_username,
    )
    if permalink:
        return permalink
    return page_feed_url(page_username)


def _launch_facebook_context(
    playwright: Any,
    *,
    storage_state_path: Path,
    browser_profile_dir: Path | None,
    browser_channel: str | None,
    headless: bool,
    timezone_id: str | None = None,
    proxy: dict[str, str] | None = None,
) -> tuple[Any, Any | None]:
    has_auth_state = storage_state_has_auth_cookies(storage_state_path)
    has_profile = browser_profile_dir is not None and browser_profile_dir.exists()
    if not has_auth_state and not has_profile and not storage_state_path.is_file():
        raise FacebookWebError(
            f"Facebook browser session not found at {storage_state_path}. "
            "Import Cookie-Editor JSON with --facebook-browser-import-session, "
            "or run: python -m media_publisher --facebook-browser-login"
        )

    last_error: Exception | None = None
    for channel in _browser_channel_candidates(browser_channel):
        try:
            # Prefer imported storage-state when it has c_user/xs. An empty
            # persistent profile from a failed interactive login otherwise wins
            # and leaves the browser logged out.
            if has_auth_state:
                browser, context = _launch_storage_context(
                    playwright,
                    storage_state_path,
                    channel=channel,
                    headless=headless,
                    timezone_id=timezone_id,
                    proxy=proxy,
                )
                return context, browser
            if has_profile:
                context = _launch_persistent_context(
                    playwright,
                    browser_profile_dir,
                    channel=channel,
                    headless=headless,
                    timezone_id=timezone_id,
                    proxy=proxy,
                )
                return context, None
            if storage_state_path.is_file():
                browser, context = _launch_storage_context(
                    playwright,
                    storage_state_path,
                    channel=channel,
                    headless=headless,
                    timezone_id=timezone_id,
                    proxy=proxy,
                )
                return context, browser
        except Exception as exc:
            last_error = exc
            continue
    raise FacebookWebError(
        "Could not launch Chromium/Chrome/Edge for Facebook publishing. "
        f"Last error: {last_error}"
    ) from last_error


@contextmanager
def _facebook_page(
    *,
    storage_state_path: Path,
    browser_profile_dir: Path | None,
    browser_channel: str | None,
    headless: bool,
    timezone_id: str | None = None,
    proxy: dict[str, str] | None = None,
) -> Iterator[Any]:
    sync_playwright = _require_playwright()
    with sync_playwright() as playwright:
        context, browser = _launch_facebook_context(
            playwright,
            storage_state_path=storage_state_path,
            browser_profile_dir=browser_profile_dir,
            browser_channel=browser_channel,
            headless=headless,
            timezone_id=timezone_id,
            proxy=proxy,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(20_000)
        page.set_default_navigation_timeout(PAGE_GOTO_TIMEOUT_MS)
        try:
            yield page
        finally:
            # Never overwrite a good imported session with a logged-out browser.
            try:
                if not _page_shows_login_wall(page):
                    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                    context.storage_state(path=str(storage_state_path))
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass


def publish_facebook_photo_via_browser(
    *,
    page_username: str,
    image_path: Path,
    caption: str,
    publish_at: datetime | None = None,
    display_timezone: str = "Europe/Sofia",
    project_root: Path | None = None,
    storage_state_path: Path | None = None,
    browser_profile_dir: Path | None = None,
    browser_channel: str | None = None,
    headless: bool | None = None,
    failure_screenshot: Path | None = None,
) -> tuple[str, str]:
    """Publish a Page photo post via the Facebook web composer.

    Returns ``(post_id_or_url, permalink)``.

    When ``publish_at`` is in the future, uses the composer Schedule controls so
    the post stays off the public feed until that time. Otherwise posts immediately.
    """
    now = datetime.now(timezone.utc)
    schedule_at: datetime | None = None
    if publish_at is not None and _as_utc(publish_at) > now:
        schedule_at = _as_utc(publish_at)

    state_path = storage_state_path or resolve_facebook_browser_state_path(
        project_root=project_root
    )
    profile_dir = browser_profile_dir
    if profile_dir is None:
        candidate = resolve_facebook_browser_profile_dir(project_root=project_root)
        profile_dir = candidate if candidate.exists() else None
    channel = browser_channel if browser_channel is not None else resolve_facebook_browser_channel()
    use_headless = _truthy_headless_default() if headless is None else headless
    resolved_image = Path(image_path).resolve()
    if not resolved_image.is_file():
        raise FacebookWebError(f"Image file not found: {resolved_image}")

    print("Launching browser…", flush=True)
    tz = (display_timezone or "Europe/Sofia").strip() or "Europe/Sofia"
    proxy = resolve_facebook_browser_proxy()
    print(f"Proxy: {_proxy_log_label(proxy)}", flush=True)
    with _facebook_page(
        storage_state_path=state_path,
        browser_profile_dir=profile_dir,
        browser_channel=channel,
        headless=use_headless,
        timezone_id=tz,
        proxy=proxy,
    ) as page:
        try:
            print(f"Opening Business Suite composer…", flush=True)
            before_html = ""
            _open_business_suite_composer(page, page_username=page_username)
            # Do not call page.content() here — it hangs on Business Suite's SPA.

            print("Attaching photo…", flush=True)
            page.wait_for_timeout(SPA_SETTLE_MS)
            _attach_photo(page, resolved_image)
            page.wait_for_timeout(1_500)
            print("Filling caption…", flush=True)
            _fill_composer_caption(page, caption)
            # Post-to remounts Lexical; exclude IG then restore/refill caption.
            _disable_instagram_destination(page)
            print("Re-checking caption after Post-to…", flush=True)
            if not _caption_text_present(page, caption):
                _fill_composer_caption(page, caption)
            if schedule_at is not None:
                print(f"Scheduling for {schedule_at.isoformat()}…", flush=True)
                _schedule_composer_post(
                    page,
                    schedule_at,
                    display_timezone=tz,
                )
                permalink = _wait_for_schedule_success(page, page_username=page_username)
            else:
                print("Publishing…", flush=True)
                _click_post(page)
                permalink = _wait_for_permalink(
                    page,
                    page_username=page_username,
                    before_html=before_html,
                )
            return permalink, permalink
        except FacebookWebError:
            _screenshot_on_failure(page, failure_screenshot)
            raise
        except Exception as exc:
            _screenshot_on_failure(page, failure_screenshot)
            raise FacebookWebError(f"Facebook browser photo publish failed: {exc}") from exc
