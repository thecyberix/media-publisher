"""Schedule YouTube community image posts through the youtube.com composer.

The Data API has no community-post method. A saved Playwright session drives
Create → Create post, attaches the image, and uses Schedule post. The Post
button is never clicked, so a missed schedule control cannot publish early.
"""
from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

from media_publisher.publishers.youtube import parse_channel_handle

DEFAULT_STORAGE_STATE = "credentials/youtube-browser-state.json"
DEFAULT_BROWSER_PROFILE = "credentials/youtube-browser-profile"
DEFAULT_BROWSER_CHANNELS = ("chrome", "msedge")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
PAGE_GOTO_TIMEOUT_MS = 45_000
GOOGLE_AUTH_COOKIE_NAMES = frozenset(
    {
        "SID",
        "SAPISID",
        "__Secure-1PSID",
        "__Secure-3PSID",
        "__Secure-1PAPISID",
        "__Secure-3PAPISID",
    }
)
LOGIN_URL_RE = re.compile(
    r"accounts\.google\.com|youtube\.com/signin|ServiceLogin|InteractiveLogin",
    re.IGNORECASE,
)
POST_BUTTON_RE = re.compile(r"^(Post|Публикуване)$", re.IGNORECASE)
SCHEDULE_ITEM_RE = re.compile(
    r"Schedule(\s+post)?|Насрочване(\s+на\s+публикация)?|Планиране(\s+на\s+публикация)?",
    re.IGNORECASE,
)
CREATE_BUTTON_RE = re.compile(r"^(Create|Създаване)$", re.IGNORECASE)
CREATE_POST_RE = re.compile(
    r"Create post|Създаване на публикация",
    re.IGNORECASE,
)
IMAGE_BUTTONS = (
    r"^Image$",
    r"^Изображение$",
    r"Add image",
    r"Upload image",
    r"Добавяне на изображение",
)
POPUP_BUTTONS = (
    r"^Accept all$",
    r"^Reject all$",
    r"^I agree$",
    r"^Agree$",
    r"^No thanks$",
    r"^Not now$",
    r"^Got it$",
    r"^Skip$",
    r"^Приемане на всички$",
    r"^Отхвърляне на всички$",
    r"^Не, благодаря$",
)
SCHEDULE_CONFIRM = (
    r"^Schedule$",
    r"^Насрочване$",
    r"^Планиране$",
)
INLINE_COMPOSER = (
    r"Share your thoughts",
    r"Say something",
    r"What's on your mind",
    r"Write something",
    r"Create post",
    r"Създаване на публикация",
)


class YouTubeWebError(RuntimeError):
    pass


class YouTubeCommunityLimitError(YouTubeWebError):
    """YouTube refused another community post for this channel today."""


@dataclass(frozen=True)
class CommunityImagePost:
    stem: str
    image_path: Path
    caption: str
    publish_at: datetime
    failure_screenshot: Path | None = None


def youtube_posts_url(handle: str) -> str:
    """Community tab for ``handle``, forced to English so the composer labels match."""
    clean = parse_channel_handle(handle)
    return f"https://www.youtube.com/@{clean}/posts?hl=en&gl=US"


def community_post_permalink(post_id: str, *, channel_handle: str) -> str:
    cleaned = post_id.strip()
    if cleaned:
        return f"https://www.youtube.com/post/{cleaned}"
    return youtube_posts_url(channel_handle)


def extract_post_id(payload: Any) -> str | None:
    """Return the first nested ``postId`` string in a create_post response."""
    if isinstance(payload, dict):
        for key in ("postId", "post_id"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in payload.values():
            found = extract_post_id(value)
            if found:
                return found
        return None
    if isinstance(payload, list):
        for item in payload:
            found = extract_post_id(item)
            if found:
                return found
    return None


def interpret_create_post_response(status: int, payload: Any) -> str:
    """Return the new post id, or ``""`` when YouTube accepted the post without one."""
    text = (
        json.dumps(payload, ensure_ascii=False)
        if payload is not None
        else ""
    )
    if status >= 400 or (isinstance(payload, dict) and payload.get("error")):
        if re.search(r"limit|too many|rate", text, re.IGNORECASE):
            raise YouTubeCommunityLimitError(
                f"YouTube refused another community post (HTTP {status}). "
                "The channel likely hit the daily post limit. Re-run tomorrow; "
                "posts already recorded in history are skipped."
            )
        detail = text[:500] or f"HTTP {status}"
        raise YouTubeWebError(f"YouTube create_post failed with HTTP {status}: {detail}")
    return extract_post_id(payload) or ""


def timezone_match_needles(when: datetime, timezone_name: str) -> tuple[str, ...]:
    """Labels that identify ``when``'s UTC offset in a YouTube time-zone list."""
    offset = when.utcoffset() or timedelta(0)
    total = int(offset.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    hours, minutes = divmod(abs(total), 60)
    hh = f"{hours:02d}"
    mm = f"{minutes:02d}"
    city = timezone_name.strip().split("/")[-1].replace("_", " ").lower()
    needles = [
        city,
        f"gmt{sign}{hh}:{mm}",
        f"utc{sign}{hh}:{mm}",
        f"gmt{sign}{hours}:{mm}",
        f"utc{sign}{hours}:{mm}",
    ]
    if minutes == 0:
        needles.extend(
            [
                f"gmt{sign}{hours}",
                f"gmt{sign}{hh}",
                f"utc{sign}{hours}",
                f"utc{sign}{hh}",
            ]
        )
    if city == "sofia":
        needles.extend(["bulgaria", "eastern european"])
    unique: list[str] = []
    for needle in needles:
        if needle and needle not in unique:
            unique.append(needle)
    return tuple(unique)


def timezone_text_matches(text: str, needles: tuple[str, ...] | list[str]) -> bool:
    lowered = text.lower()
    compact = re.sub(r"\s+", "", lowered)
    for needle in needles:
        if needle.startswith("gmt") or needle.startswith("utc"):
            if re.search(re.escape(needle) + r"(?![\d:])", compact):
                return True
        elif needle and re.search(
            rf"(?<![a-z]){re.escape(needle)}(?![a-z])",
            lowered,
        ):
            return True
    return False


def storage_state_has_google_auth(storage_state_path: Path) -> bool:
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
        cookie.get("name")
        for cookie in cookies
        if isinstance(cookie, dict)
    }
    return bool(names & GOOGLE_AUTH_COOKIE_NAMES)


def resolve_youtube_browser_state_path(
    *,
    project_root: Path | None = None,
    override: str | Path | None = None,
) -> Path:
    raw = override or os.getenv("YOUTUBE_BROWSER_STATE", DEFAULT_STORAGE_STATE).strip()
    return _resolve_path(raw or DEFAULT_STORAGE_STATE, project_root=project_root)


def resolve_youtube_browser_profile_dir(
    *,
    project_root: Path | None = None,
    override: str | Path | None = None,
) -> Path:
    raw = (
        override
        or os.getenv("YOUTUBE_BROWSER_PROFILE", DEFAULT_BROWSER_PROFILE).strip()
    )
    return _resolve_path(raw or DEFAULT_BROWSER_PROFILE, project_root=project_root)


def resolve_youtube_browser_channel() -> str | None:
    """Preferred browser channel.

    Empty / ``chromium`` uses Playwright's Chromium. Unset defaults to Chrome
    locally and Chromium under GitHub Actions.
    """
    if "YOUTUBE_BROWSER_CHANNEL" in os.environ:
        raw = os.environ["YOUTUBE_BROWSER_CHANNEL"].strip().lower()
        if raw in {"", "chromium", "bundled", "none"}:
            return None
        if raw in {"edge", "msedge"}:
            return "msedge"
        return raw
    if os.getenv("GITHUB_ACTIONS", "").strip().lower() in {"1", "true"}:
        return None
    return "chrome"


def import_browser_session(source_path: Path, destination_path: Path) -> None:
    if not source_path.exists():
        raise YouTubeWebError(f"Session file not found: {source_path}")
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    cookies = _cookies_from_import_payload(payload)
    names = {cookie["name"] for cookie in cookies}
    if not names & GOOGLE_AUTH_COOKIE_NAMES:
        raise YouTubeWebError(
            "Import file has no Google auth cookies "
            "(SAPISID, SID, or __Secure-1PSID). Export them from a signed-in youtube.com tab."
        )
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.write_text(
        json.dumps({"cookies": cookies, "origins": []}, indent=2) + "\n",
        encoding="utf-8",
    )


def save_browser_session_interactive(
    browser_state_path: Path,
    *,
    browser_profile_dir: Path,
    channel_handle: str,
    browser_channel: str | None = "chrome",
) -> None:
    """Open the channel posts tab; the operator signs in and saves the session."""
    sync_playwright = _require_playwright()
    browser_state_path.parent.mkdir(parents=True, exist_ok=True)
    start_url = youtube_posts_url(channel_handle)
    handle = parse_channel_handle(channel_handle)
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
                    timezone_id="Europe/Sofia",
                )
                page = context.pages[0] if context.pages else context.new_page()
                print(f"Opened YouTube using {label}.")
                print(f"1. Sign in as the Google account that manages @{handle}.")
                print("2. If YouTube asks you to switch channel, choose that one.")
                print(f"3. Confirm {start_url} shows the posts tab.")
                print("4. Press Enter here to save the session.")
                page.goto(start_url, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
                page.wait_for_timeout(1_000)
                input()
                if LOGIN_URL_RE.search(page.url) or not _context_has_google_auth(context):
                    raise YouTubeWebError(
                        "Still signed out of YouTube. Finish login, then run this again."
                    )
                context.storage_state(path=str(browser_state_path))
                context.close()
            print(f"Saved YouTube browser session to {browser_state_path}.")
            return
        except YouTubeWebError:
            raise
        except Exception as exc:
            last_error = exc
            continue
    raise YouTubeWebError(
        "Could not launch Chrome or Edge for YouTube login. "
        "Install Google Chrome or Microsoft Edge, then retry. "
        f"Last error: {last_error}"
    ) from last_error


def schedule_community_image_posts(
    posts: list[CommunityImagePost] | tuple[CommunityImagePost, ...],
    *,
    channel_handle: str,
    display_timezone: str,
    storage_state_path: Path,
    browser_profile_dir: Path | None,
    browser_channel: str | None,
    headless: bool | None,
    on_success: Callable[[CommunityImagePost, str], None],
    print_line: Callable[[str], None] | None = None,
) -> tuple[int, int, list[str]]:
    """Schedule each image post. Calls ``on_success`` after YouTube accepts it.

    Returns ``(scheduled, failed, errors)``. A daily post-limit error stops the
    rest of the batch; other failures skip to the next day.
    """
    log = print_line or print
    if not posts:
        return 0, 0, []
    use_headless = _headless_default() if headless is None else headless
    ok = 0
    failed = 0
    errors: list[str] = []
    captured: list[dict[str, Any]] = []
    with _youtube_page(
        storage_state_path=storage_state_path,
        browser_profile_dir=browser_profile_dir,
        browser_channel=browser_channel,
        headless=use_headless,
        timezone_id=display_timezone,
    ) as page:
        def _on_response(response: Any) -> None:
            if "backstage/create_post" not in response.url:
                return
            entry: dict[str, Any] = {"status": response.status}
            try:
                entry["json"] = response.json()
            except Exception:
                try:
                    entry["text"] = response.text()[:1500]
                except Exception:
                    entry["text"] = ""
            captured.append(entry)

        page.on("response", _on_response)
        _goto_posts(page, channel_handle)
        for index, post in enumerate(posts):
            if index:
                try:
                    _goto_posts(page, channel_handle)
                except YouTubeWebError as exc:
                    failed += 1
                    message = f"{post.stem}: {exc}"
                    errors.append(message)
                    log(f"  failed — {exc}")
                    break
            log(
                f"{post.stem}: schedule {post.publish_at.isoformat()} "
                f"({post.image_path.name})"
            )
            captured.clear()
            try:
                post_id = _schedule_one(
                    page,
                    post,
                    channel_handle=channel_handle,
                    display_timezone=display_timezone,
                    captured=captured,
                )
            except YouTubeCommunityLimitError as exc:
                _screenshot(page, post.failure_screenshot)
                failed += 1
                errors.append(f"{post.stem}: {exc}")
                log(f"  failed — {exc}")
                break
            except YouTubeWebError as exc:
                _screenshot(page, post.failure_screenshot)
                failed += 1
                errors.append(f"{post.stem}: {exc}")
                log(f"  failed — {exc}")
                continue
            except Exception as exc:
                _screenshot(page, post.failure_screenshot)
                failed += 1
                errors.append(f"{post.stem}: {exc}")
                log(f"  failed — {exc}")
                continue
            permalink = community_post_permalink(
                post_id, channel_handle=channel_handle
            )
            on_success(post, permalink)
            ok += 1
            page.wait_for_timeout(1_200)
    return ok, failed, errors


def _schedule_one(
    page: Any,
    post: CommunityImagePost,
    *,
    channel_handle: str,
    display_timezone: str,
    captured: list[dict[str, Any]],
) -> str:
    image_path = Path(post.image_path).resolve()
    if not image_path.is_file():
        raise YouTubeWebError(f"Image file not found: {image_path}")
    print("  opening composer…", flush=True)
    dialog = _open_post_composer(page, channel_handle)
    print("  attaching image…", flush=True)
    _attach_image(page, dialog, image_path)
    print("  filling caption…", flush=True)
    _fill_caption(dialog, post.caption)
    print(f"  scheduling for {post.publish_at.isoformat()}…", flush=True)
    root = _open_schedule_panel(page, dialog)
    _set_schedule_when(page, root, post.publish_at, display_timezone)
    captured.clear()
    _confirm_schedule(page, root)
    return _wait_for_create_post(page, captured)


def _open_post_composer(page: Any, handle: str) -> Any:
    dialog = _visible_composer(page)
    if dialog is not None:
        return dialog
    for pattern in INLINE_COMPOSER:
        compiled = re.compile(pattern, re.IGNORECASE)
        if _click_visible(page.get_by_role("textbox", name=compiled)):
            break
        if _click_visible(page.get_by_role("button", name=compiled)):
            break
    dialog = _wait_composer(page, seconds=4)
    if dialog is not None:
        return dialog
    create = page.get_by_role("button", name=CREATE_BUTTON_RE)
    if not _click_visible(create):
        create = page.locator(
            "button[aria-label='Create'], button[aria-label='Създаване']"
        )
        if not _click_visible(create):
            raise YouTubeWebError(
                "Could not find Create. Sign in as "
                f"@{parse_channel_handle(handle)} and confirm the posts tab loaded. "
                + _control_summary(page)
            )
    page.wait_for_timeout(500)
    item = page.get_by_role("menuitem", name=CREATE_POST_RE)
    if not _click_visible(item):
        item = page.get_by_text(CREATE_POST_RE)
        if not _click_visible(item):
            raise YouTubeWebError(
                "Create menu has no Create post item. " + _control_summary(page)
            )
    _choose_channel_if_asked(page, handle)
    dialog = _wait_composer(page, seconds=15)
    if dialog is None:
        raise YouTubeWebError(
            "Create post dialog did not open. " + _control_summary(page)
        )
    return dialog


def _choose_channel_if_asked(page: Any, handle: str) -> None:
    dialog = _top_visible_dialog(page)
    if dialog is None:
        return
    try:
        text = dialog.inner_text(timeout=2_000)
    except Exception:
        return
    lowered = text.lower()
    if "channel" not in lowered and "канал" not in lowered:
        return
    if _looks_like_composer(dialog):
        return
    option = dialog.get_by_text(re.compile(re.escape(parse_channel_handle(handle)), re.I))
    if option.count() == 0:
        raise YouTubeWebError(
            f"YouTube asked which channel to use, and @{parse_channel_handle(handle)} "
            "was not listed. Switch to that channel during "
            "--youtube-browser-login, then retry."
        )
    option.first.click(timeout=5_000)


def _attach_image(page: Any, dialog: Any, image_path: Path) -> None:
    before = dialog.locator("img").count()
    file_input = _image_file_input(dialog) or _image_file_input(page)
    if file_input is not None:
        file_input.set_input_files(str(image_path))
    else:
        try:
            with page.expect_file_chooser(timeout=8_000) as chooser:
                if not _click_named(dialog, IMAGE_BUTTONS, roles=("button",)):
                    raise YouTubeWebError(
                        "Could not find the image button in the post composer. "
                        + _control_summary(page)
                    )
            chooser.value.set_files(str(image_path))
        except YouTubeWebError:
            raise
        except Exception as exc:
            file_input = _image_file_input(dialog) or _image_file_input(page)
            if file_input is None:
                raise YouTubeWebError(
                    f"Could not attach the image ({exc}). " + _control_summary(page)
                ) from exc
            file_input.set_input_files(str(image_path))
    _wait_for_image_preview(dialog, before_count=before)
    _wait_until_post_enabled(dialog)


def _fill_caption(dialog: Any, caption: str) -> None:
    box = dialog.get_by_role("textbox").first
    box.click(timeout=5_000)
    box.press("Control+A")
    box.press("Backspace")
    dialog.page.keyboard.insert_text(caption)
    if not _caption_visible(box, caption):
        box.evaluate(
            """(el, value) => {
                el.focus();
                document.execCommand('selectAll', false, null);
                document.execCommand('insertText', false, value);
            }""",
            caption,
        )
    if not _caption_visible(box, caption):
        raise YouTubeWebError("The composer did not keep the caption.")


def _caption_visible(box: Any, caption: str) -> bool:
    probe = caption.strip()[:20]
    if not probe:
        return True
    try:
        visible = box.inner_text(timeout=3_000)
    except Exception:
        return False
    return probe in visible


def _open_schedule_panel(page: Any, dialog: Any) -> Any:
    if not _click_schedule_arrow(dialog):
        raise YouTubeWebError(
            "Could not find the arrow beside Post. Nothing was published. "
            + _control_summary(page)
        )
    page.wait_for_timeout(400)
    _click_schedule_menu_item(page)
    return _wait_for_schedule_fields(page)


def _click_schedule_arrow(dialog: Any) -> bool:
    if _click_named(
        dialog,
        (r"^More$", r"^More options$", r"^Other options$", r"^Още$"),
        roles=("button",),
        timeout_ms=1_500,
    ):
        return True
    post = dialog.get_by_role("button", name=POST_BUTTON_RE)
    if post.count() == 0:
        return False
    post_box = post.first.bounding_box()
    if not post_box:
        return False
    buttons = dialog.get_by_role("button")
    best = None
    best_dist = 10_000.0
    for index in range(buttons.count()):
        button = buttons.nth(index)
        try:
            name = (button.inner_text(timeout=400) or "").strip()
        except Exception:
            name = ""
        if POST_BUTTON_RE.match(name):
            continue
        box = button.bounding_box()
        if not box:
            continue
        dist = box["x"] - (post_box["x"] + post_box["width"])
        vertical = abs(
            (box["y"] + box["height"] / 2) - (post_box["y"] + post_box["height"] / 2)
        )
        if -8 <= dist <= 96 and vertical <= 28 and dist < best_dist:
            best = button
            best_dist = dist
    if best is None:
        return False
    best.click(timeout=4_000)
    return True


def _click_schedule_menu_item(page: Any) -> None:
    for role in ("menuitem", "option", "button"):
        locator = page.get_by_role(role, name=SCHEDULE_ITEM_RE)
        if _click_visible(locator):
            return
    if _click_visible(page.get_by_text(SCHEDULE_ITEM_RE)):
        return
    raise YouTubeWebError(
        "Schedule post was not in the menu beside Post. Nothing was published. "
        + _control_summary(page)
    )


def _wait_for_schedule_fields(page: Any) -> Any:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for root in _candidate_roots(page):
            if root.locator("input[type='date'], input[type='time']").count() > 0:
                return root
        page.wait_for_timeout(300)
    raise YouTubeWebError(
        "Schedule panel did not show date and time fields. Nothing was published. "
        + _control_summary(page)
    )


def _set_schedule_when(
    page: Any,
    root: Any,
    publish_at: datetime,
    display_timezone: str,
) -> None:
    try:
        zone = ZoneInfo(display_timezone)
    except Exception as exc:
        raise YouTubeWebError(
            f"Unknown timezone {display_timezone!r}: {exc}"
        ) from exc
    local = publish_at.astimezone(zone)
    date_value = local.strftime("%Y-%m-%d")
    time_value = local.strftime("%H:%M")
    if root.locator("input[type='date']").count():
        if not _fill_first(root, "input[type='date']", date_value):
            raise YouTubeWebError(
                f"Could not set the schedule date to {date_value}. "
                "Nothing was published. " + _control_summary(page)
            )
    elif not _fill_labeled(root, (r"^Date$", r"^Дата$"), date_value):
        raise YouTubeWebError(
            "Schedule panel has no date field. Nothing was published. "
            + _control_summary(page)
        )
    if root.locator("input[type='time']").count():
        if not _fill_first(root, "input[type='time']", time_value):
            raise YouTubeWebError(
                f"Could not set the schedule time to {time_value}. "
                "Nothing was published. " + _control_summary(page)
            )
    elif not _fill_labeled(root, (r"^Time$", r"^Час$"), time_value):
        raise YouTubeWebError(
            "Schedule panel has no time field. Nothing was published. "
            + _control_summary(page)
        )
    _ensure_timezone(page, root, local, display_timezone)


def _ensure_timezone(page: Any, root: Any, when: datetime, timezone_name: str) -> None:
    needles = timezone_match_needles(when, timezone_name)
    # Read the selected control only. The dialog also contains the channel name
    # ("Bulgarian") and every time-zone option, which must not count as a match.
    if timezone_text_matches(_selected_timezone_text(root), needles):
        return
    if _select_timezone_option(page, root, needles):
        return
    raise YouTubeWebError(
        f"Could not set the schedule time zone to {timezone_name}. "
        "Nothing was published. " + _control_summary(page)
    )


def _selected_timezone_text(root: Any) -> str:
    parts: list[str] = []
    selects = root.locator("select")
    for index in range(selects.count()):
        select = selects.nth(index)
        try:
            selected = select.locator("option:checked")
            if selected.count():
                parts.append(selected.first.inner_text(timeout=1_000) or "")
            parts.append(select.input_value() or "")
        except Exception:
            continue
    for pattern in (r"Time zone", r"Timezone", r"Часова зона"):
        compiled = re.compile(pattern, re.IGNORECASE)
        for role in ("combobox", "button"):
            locator = root.get_by_role(role, name=compiled)
            if locator.count() == 0:
                continue
            try:
                parts.append(locator.first.inner_text(timeout=1_000) or "")
                label = locator.first.get_attribute("aria-label") or ""
                if label:
                    parts.append(label)
            except Exception:
                continue
    return "\n".join(parts)


def _select_timezone_option(page: Any, root: Any, needles: tuple[str, ...]) -> bool:
    selects = root.locator("select")
    for index in range(selects.count()):
        select = selects.nth(index)
        options = select.locator("option")
        for option_index in range(options.count()):
            label = (options.nth(option_index).inner_text(timeout=1_000) or "").strip()
            if not timezone_text_matches(label, needles):
                continue
            value = options.nth(option_index).get_attribute("value")
            if value:
                select.select_option(value=value)
            else:
                select.select_option(label=label)
            return True
    for pattern in (r"Time zone", r"Timezone", r"Часова зона"):
        compiled = re.compile(pattern, re.IGNORECASE)
        combo = root.get_by_role("combobox", name=compiled)
        if combo.count() == 0:
            combo = root.get_by_role("button", name=compiled)
        if combo.count() == 0 or not _click_visible(combo):
            continue
        page.wait_for_timeout(300)
        option = page.get_by_role("option")
        for option_index in range(min(option.count(), 500)):
            try:
                label = option.nth(option_index).inner_text(timeout=500) or ""
            except Exception:
                continue
            if timezone_text_matches(label, needles):
                option.nth(option_index).click(timeout=4_000)
                return True
    return False


def _confirm_schedule(page: Any, root: Any) -> None:
    if _click_named(root, SCHEDULE_CONFIRM, roles=("button",)):
        return
    if _click_named(page, SCHEDULE_CONFIRM, roles=("button",)):
        return
    raise YouTubeWebError(
        "Could not find the Schedule button. Nothing was published. "
        + _control_summary(page)
    )


def _wait_for_create_post(page: Any, captured: list[dict[str, Any]]) -> str:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not captured:
        page.wait_for_timeout(400)
    if not captured:
        raise YouTubeWebError(
            "Schedule was clicked but YouTube did not accept the post. "
            + _control_summary(page)
        )
    entry = captured[-1]
    payload = entry.get("json")
    if payload is None and entry.get("text"):
        try:
            payload = json.loads(str(entry["text"]))
        except json.JSONDecodeError:
            payload = {"error": entry["text"]}
    return interpret_create_post_response(int(entry.get("status") or 0), payload)


def _goto_posts(page: Any, handle: str) -> None:
    url = youtube_posts_url(handle)
    print(f"  goto {url}", flush=True)
    page.goto(url, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
    page.wait_for_timeout(1_200)
    _dismiss_popups(page)
    if LOGIN_URL_RE.search(page.url):
        raise YouTubeWebError(
            "YouTube browser session is signed out. "
            "Run: python -m media_publisher --youtube-browser-login"
        )


def _dismiss_popups(page: Any) -> None:
    _click_named(page, POPUP_BUTTONS, roles=("button",), timeout_ms=1_200)


def _wait_composer(page: Any, *, seconds: float) -> Any | None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        dialog = _visible_composer(page)
        if dialog is not None:
            return dialog
        page.wait_for_timeout(250)
    return None


def _visible_composer(page: Any) -> Any | None:
    dialogs = page.get_by_role("dialog")
    for index in range(dialogs.count() - 1, -1, -1):
        dialog = dialogs.nth(index)
        try:
            if dialog.is_visible() and _looks_like_composer(dialog):
                return dialog
        except Exception:
            continue
    return None


def _top_visible_dialog(page: Any) -> Any | None:
    dialogs = page.get_by_role("dialog")
    for index in range(dialogs.count() - 1, -1, -1):
        dialog = dialogs.nth(index)
        try:
            if dialog.is_visible():
                return dialog
        except Exception:
            continue
    return None


def _looks_like_composer(dialog: Any) -> bool:
    if dialog.get_by_role("textbox").count() < 1:
        return False
    if dialog.get_by_role("button", name=POST_BUTTON_RE).count() > 0:
        return True
    if dialog.get_by_role("button", name=re.compile(r"Image|Изображение", re.I)).count() > 0:
        return True
    return False


def _candidate_roots(page: Any) -> list[Any]:
    roots: list[Any] = []
    dialogs = page.get_by_role("dialog")
    for index in range(dialogs.count() - 1, -1, -1):
        dialog = dialogs.nth(index)
        try:
            if dialog.is_visible():
                roots.append(dialog)
        except Exception:
            continue
    roots.append(page)
    return roots


def _image_file_input(root: Any) -> Any | None:
    locator = root.locator("input[type='file']")
    fallback = None
    for index in range(locator.count()):
        field = locator.nth(index)
        accept = (field.get_attribute("accept") or "").lower()
        if any(token in accept for token in ("image", "jpg", "jpeg", "png", "webp")):
            return field
        if accept == "" and fallback is None:
            fallback = field
    return fallback


def _wait_for_image_preview(dialog: Any, *, before_count: int) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        images = dialog.locator("img")
        if images.count() > before_count:
            return
        if dialog.locator("[style*='blob:']").count() > 0:
            return
        for index in range(images.count()):
            src = images.nth(index).get_attribute("src") or ""
            if src.startswith("blob:"):
                return
        dialog.page.wait_for_timeout(400)
    raise YouTubeWebError("Image preview did not appear after upload.")


def _wait_until_post_enabled(dialog: Any) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        button = dialog.get_by_role("button", name=POST_BUTTON_RE)
        if button.count():
            try:
                if button.first.is_enabled():
                    return
            except Exception:
                pass
        dialog.page.wait_for_timeout(400)
    raise YouTubeWebError(
        "Post stayed disabled. The image may still be uploading or was rejected."
    )


def _fill_first(root: Any, css: str, value: str) -> bool:
    locator = root.locator(css)
    if locator.count() == 0:
        return False
    field = locator.first
    field.click(timeout=3_000)
    field.fill(value)
    current = (field.input_value() or "").strip()
    return value in current or current.startswith(value[:5])


def _fill_labeled(root: Any, names: tuple[str, ...], value: str) -> bool:
    for name in names:
        compiled = re.compile(name, re.IGNORECASE)
        locator = root.get_by_role("textbox", name=compiled)
        if locator.count() == 0:
            locator = root.get_by_label(compiled)
        if locator.count() == 0:
            continue
        locator.first.fill(value)
        return True
    return False


def _click_named(
    root: Any,
    patterns: tuple[str, ...],
    *,
    roles: tuple[str, ...] = ("button", "link"),
    timeout_ms: int = 4_000,
) -> bool:
    for pattern in patterns:
        compiled = re.compile(pattern, re.IGNORECASE)
        for role in roles:
            if _click_visible(root.get_by_role(role, name=compiled), timeout_ms=timeout_ms):
                return True
    return False


def _click_visible(locator: Any, timeout_ms: int = 4_000) -> bool:
    try:
        count = locator.count()
    except Exception:
        return False
    for index in range(count):
        candidate = locator.nth(index)
        try:
            if not candidate.is_visible():
                continue
            candidate.click(timeout=timeout_ms)
            return True
        except Exception:
            continue
    return False


def _control_summary(page: Any) -> str:
    names: list[str] = []
    buttons = page.get_by_role("button")
    for index in range(min(buttons.count(), 18)):
        try:
            button = buttons.nth(index)
            label = button.get_attribute("aria-label") or ""
            text = ""
            try:
                text = button.inner_text(timeout=300)
            except Exception:
                pass
            shown = (label or text).strip().replace("\n", " ")
            if shown:
                names.append(shown[:48])
        except Exception:
            continue
    inputs: list[str] = []
    fields = page.locator("input")
    for index in range(min(fields.count(), 10)):
        try:
            field = fields.nth(index)
            kind = field.get_attribute("type") or "text"
            label = field.get_attribute("aria-label") or ""
            inputs.append(f"{kind}:{label}" if label else kind)
        except Exception:
            continue
    return f"url={page.url} buttons={names} inputs={inputs}"


def _screenshot(page: Any, destination: Path | None) -> None:
    if destination is None:
        return
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(destination), full_page=True)
    except Exception:
        pass


def _resolve_path(raw: str | Path, *, project_root: Path | None) -> Path:
    path = Path(raw)
    if not path.is_absolute() and project_root is not None:
        return project_root / path
    return path


def _headless_default() -> bool:
    raw = os.getenv("YOUTUBE_BROWSER_HEADLESS", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return os.getenv("GITHUB_ACTIONS", "").strip().lower() in {"1", "true"}


def _browser_channel_candidates(preferred: str | None) -> tuple[str | None, ...]:
    normalized = (preferred or "").strip().lower()
    if normalized in {"", "chromium", "bundled", "none"}:
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
        raise YouTubeWebError(
            "Playwright is required for YouTube community posts. "
            'Install it with: pip install playwright && playwright install chromium'
        ) from exc
    return sync_playwright


def _context_has_google_auth(context: Any) -> bool:
    try:
        cookies = context.cookies()
    except Exception:
        return False
    names = {cookie.get("name") for cookie in cookies if isinstance(cookie, dict)}
    return bool(names & GOOGLE_AUTH_COOKIE_NAMES)


def _normalize_same_site(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "Lax"
    normalized = value.strip().lower().replace("_", " ")
    if normalized in {"none", "no restriction"}:
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
    if expires is None and isinstance(raw.get("expirationDate"), (int, float)):
        expires = float(raw["expirationDate"])
    expires_value = float(expires) if isinstance(expires, (int, float)) and expires > 0 else -1.0
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


def _cookies_from_import_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("cookies"), list):
        raw_cookies = payload["cookies"]
    elif isinstance(payload, list):
        raw_cookies = payload
    else:
        raise YouTubeWebError(
            "Expected a Cookie-Editor JSON array or a Playwright storage-state "
            "object with a top-level 'cookies' array."
        )
    cookies: list[dict[str, Any]] = []
    for raw in raw_cookies:
        if not isinstance(raw, dict):
            continue
        normalized = _normalize_browser_cookie(raw)
        if normalized is not None:
            cookies.append(normalized)
    if not cookies:
        raise YouTubeWebError("No usable cookies found in the import file.")
    return cookies


def _launch_persistent_context(
    playwright: Any,
    profile_dir: Path,
    *,
    channel: str | None,
    headless: bool,
    timezone_id: str | None,
) -> Any:
    profile_dir.mkdir(parents=True, exist_ok=True)
    launch_kwargs: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": headless,
        "viewport": {"width": 1400, "height": 900},
        "locale": "en-US",
        "user_agent": DEFAULT_USER_AGENT,
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    }
    if timezone_id:
        launch_kwargs["timezone_id"] = timezone_id
    if channel:
        launch_kwargs["channel"] = channel
    context = playwright.chromium.launch_persistent_context(**launch_kwargs)
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return context


def _launch_storage_context(
    playwright: Any,
    storage_state_path: Path,
    *,
    channel: str | None,
    headless: bool,
    timezone_id: str | None,
) -> tuple[Any, Any]:
    launch_kwargs: dict[str, Any] = {
        "headless": headless,
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    }
    if channel:
        launch_kwargs["channel"] = channel
    browser = playwright.chromium.launch(**launch_kwargs)
    context_kwargs: dict[str, Any] = {
        "storage_state": str(storage_state_path),
        "viewport": {"width": 1400, "height": 900},
        "locale": "en-US",
        "user_agent": DEFAULT_USER_AGENT,
    }
    if timezone_id:
        context_kwargs["timezone_id"] = timezone_id
    context = browser.new_context(**context_kwargs)
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return browser, context


def _launch_youtube_context(
    playwright: Any,
    *,
    storage_state_path: Path,
    browser_profile_dir: Path | None,
    browser_channel: str | None,
    headless: bool,
    timezone_id: str | None,
) -> tuple[Any, Any | None]:
    has_auth_state = storage_state_has_google_auth(storage_state_path)
    has_profile = browser_profile_dir is not None and browser_profile_dir.exists()
    if not has_auth_state and not has_profile:
        raise YouTubeWebError(
            f"YouTube browser session not found at {storage_state_path}. "
            "Run: python -m media_publisher --youtube-browser-login"
        )
    last_error: Exception | None = None
    for channel in _browser_channel_candidates(browser_channel):
        try:
            if has_auth_state:
                browser, context = _launch_storage_context(
                    playwright,
                    storage_state_path,
                    channel=channel,
                    headless=headless,
                    timezone_id=timezone_id,
                )
                return context, browser
            assert browser_profile_dir is not None
            context = _launch_persistent_context(
                playwright,
                browser_profile_dir,
                channel=channel,
                headless=headless,
                timezone_id=timezone_id,
            )
            return context, None
        except Exception as exc:
            last_error = exc
            continue
    raise YouTubeWebError(
        "Could not launch Chromium/Chrome/Edge for YouTube community posts. "
        f"Last error: {last_error}"
    ) from last_error


@contextmanager
def _youtube_page(
    *,
    storage_state_path: Path,
    browser_profile_dir: Path | None,
    browser_channel: str | None,
    headless: bool,
    timezone_id: str | None,
) -> Iterator[Any]:
    sync_playwright = _require_playwright()
    with sync_playwright() as playwright:
        context, browser = _launch_youtube_context(
            playwright,
            storage_state_path=storage_state_path,
            browser_profile_dir=browser_profile_dir,
            browser_channel=browser_channel,
            headless=headless,
            timezone_id=timezone_id,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(8_000)
        page.set_default_navigation_timeout(PAGE_GOTO_TIMEOUT_MS)
        try:
            yield page
        finally:
            try:
                if not LOGIN_URL_RE.search(page.url) and _context_has_google_auth(context):
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
