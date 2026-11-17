"""Download preview images from public Canva publish-share links."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from media_publisher.sources.canva import resolve_canva_url
from media_publisher.sources.source_thumbnail import ASPECT_TOLERANCE

CANVA_MEDIA_RE = re.compile(r"https://media\.canva\.com/v2/image-resize/[^\"'\s<>]+")
DOCUMENT_IMAGE_RE = re.compile(
    r"https://media\.canva\.com/v2/document-image/[^\"'\s<>]+"
)
PAGE_DOCUMENT_IMAGE_RE = re.compile(
    r'"page":(\d+).{0,240}?"url":"(https://media\.canva\.com/v2/document-image/[^"]+)"'
)
BOOTSTRAP_FILE_RE = re.compile(
    r'"url":"(https://media\.canva\.com/v2/image-resize/[^"]+)"'
    r'[^}]*?"width":(\d+)'
    r'[^}]*?"height":(\d+)'
    r'[^}]*?"quality":"([^"]+)"',
)
QUALITY_RANK = {
    "SCREEN_3X": 6,
    "SCREEN_2X": 5,
    "SCREEN": 4,
    "THUMBNAIL": 3,
    "MICRO_THUMBNAIL": 2,
    "NANO_THUMBNAIL": 1,
    "PICO_THUMBNAIL": 0,
}
MIN_PREVIEW_BYTES = 1000
MIN_PREVIEW_EDGE = 32
_SCREEN_DOWNLOAD_TIMEOUT_SECONDS = 45
_SHARE_PATH_STOP = {"view", "edit", "screen"}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def normalize_canva_share_url(canva_url: str) -> str:
    parsed = urlparse(canva_url)
    path = parsed.path
    if "/edit" in path:
        path = path.replace("/edit", "/view", 1)
    elif "/view" not in path and "/design/" in path:
        parts = path.rstrip("/").split("/")
        if len(parts) >= 4:
            path = "/".join(parts[:4] + ["view"])
        elif len(parts) == 3:
            path = "/".join(parts + ["view"])
    return urlunparse(parsed._replace(path=path))


def _find_chrome() -> str:
    for candidate in (
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    chrome = shutil.which("chrome") or shutil.which("msedge") or shutil.which("google-chrome")
    if chrome:
        return chrome
    raise RuntimeError("Chrome or Edge is required for Canva share-link previews")


def _fetch_dom(canva_url: str) -> str:
    browser = _find_chrome()
    completed = subprocess.run(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--virtual-time-budget=25000",
            "--dump-dom",
            canva_url,
        ],
        capture_output=True,
        timeout=180,
        check=False,
    )
    html = completed.stdout.decode("utf-8", errors="replace")
    if len(html) < 1000:
        stderr = completed.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Canva page DOM was empty for {canva_url!r}: {stderr[-400:]}"
        )
    return html


def _html_has_preview_hints(html: str) -> bool:
    lowered = html.casefold()
    return (
        "media.canva.com" in lowered
        or "bootstrap" in lowered
        or "og:image" in lowered
    )


def _fetch_page_html(canva_url: str) -> str:
    """Return Canva share-page HTML, preferring a plain HTTP fetch.

    Publish-share ``/view`` pages often embed ``media.canva.com`` preview URLs
    in the initial HTML, so CI runners without Chrome can still resolve them.
    Falls back to headless Chrome when the static response is unusable.
    """
    request = urllib.request.Request(
        canva_url,
        headers={"User-Agent": DEFAULT_USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            html = response.read().decode("utf-8", errors="replace")
        if len(html) >= 1000 and _html_has_preview_hints(html):
            return html
    except urllib.error.URLError:
        pass
    return _fetch_dom(canva_url)


def _size_from_media_url(url: str) -> tuple[int, int] | None:
    width = height = 0
    for part in url.split("/"):
        if part.startswith("width:"):
            width = int(part.split(":", 1)[1].split("?", 1)[0])
        elif part.startswith("height:"):
            height = int(part.split(":", 1)[1].split("?", 1)[0])
    if width <= 0 or height <= 0:
        return None
    return width, height


def _dimension_preference(
    width: int,
    height: int,
    *,
    quality_rank: int,
    target_size: tuple[int, int] | None,
) -> tuple[int, float, int, int]:
    """Rank a candidate size. Aspect match comes before resolution."""
    pixels = width * height
    if target_size is None or target_size[1] <= 0 or height <= 0 or width <= 0:
        return (0, 0.0, quality_rank, pixels)
    distance = abs((target_size[0] / target_size[1]) - (width / height))
    matched = 1 if distance <= ASPECT_TOLERANCE else 0
    return (matched, -distance, quality_rank, pixels)


def _bootstrap_element_sizes(html: str) -> list[tuple[int, int, int]]:
    sizes: list[tuple[int, int, int]] = []
    for _url, width, height, quality in BOOTSTRAP_FILE_RE.findall(html):
        sizes.append((int(width), int(height), QUALITY_RANK.get(quality, 0)))
    if sizes:
        return sizes

    marker = "window['bootstrap'] = JSON.parse('"
    start = html.find(marker)
    if start < 0:
        return []
    start += len(marker)
    end = start
    escaped = False
    while end < len(html):
        char = html[end]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "'":
            break
        end += 1
    try:
        payload = json.loads(html[start:end])
    except json.JSONDecodeError:
        return []

    for asset in payload.get("base", {}).get("assets", []):
        if not isinstance(asset, dict):
            continue
        for file_info in asset.get("files", []):
            if not isinstance(file_info, dict):
                continue
            url = file_info.get("url")
            if not isinstance(url, str) or "media.canva.com" not in url:
                continue
            width = int(file_info.get("width") or 0)
            height = int(file_info.get("height") or 0)
            if width <= 0 or height <= 0:
                continue
            quality = str(file_info.get("quality", ""))
            sizes.append((width, height, QUALITY_RANK.get(quality, 0)))
    return sizes


def _extract_bootstrap_dimensions(
    html: str,
    *,
    target_size: tuple[int, int] | None = None,
) -> tuple[int, int] | None:
    """Return the design size, preferring a page whose aspect matches.

    Composed ``document-image`` pages are the design. Element photos embedded
    in the same HTML are only a fallback, because a large photo can be a
    different shape from the page.
    """
    page_sizes = [
        (*size, 0)
        for url in document_image_urls_from_html(html)
        if (size := _size_from_media_url(url)) is not None
    ]
    candidates = page_sizes or _bootstrap_element_sizes(html)
    if not candidates:
        return None
    width, height, _quality = max(
        candidates,
        key=lambda item: _dimension_preference(
            item[0],
            item[1],
            quality_rank=item[2],
            target_size=target_size,
        ),
    )
    return width, height


def probe_canva_design_dimensions(
    canva_url: str,
    *,
    target_size: tuple[int, int] | None = None,
) -> tuple[int, int] | None:
    """Return the preview image dimensions for a public Canva design link."""
    resolved = resolve_canva_url(canva_url)
    normalized = normalize_canva_share_url(resolved)
    html = _fetch_page_html(normalized)
    return _extract_bootstrap_dimensions(html, target_size=target_size)


def _pick_best_bootstrap_url(html: str) -> str | None:
    best: tuple[int, int, str] | None = None
    for match in BOOTSTRAP_FILE_RE.finditer(html):
        url, width, height, quality = match.groups()
        rank = QUALITY_RANK.get(quality, 0)
        score = (rank, int(width) * int(height), url)
        if best is None or score > best:
            best = score
    if best:
        return best[2]

    marker = "window['bootstrap'] = JSON.parse('"
    start = html.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = start
    escaped = False
    while end < len(html):
        char = html[end]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "'":
            break
        end += 1
    try:
        payload = json.loads(html[start:end])
    except json.JSONDecodeError:
        return None
    best = None
    for asset in payload.get("base", {}).get("assets", []):
        for file_info in asset.get("files", []):
            url = file_info.get("url")
            if not isinstance(url, str) or "media.canva.com" not in url:
                continue
            quality = str(file_info.get("quality", ""))
            rank = QUALITY_RANK.get(quality, 0)
            width = int(file_info.get("width") or 0)
            height = int(file_info.get("height") or 0)
            score = (rank, width * height, url)
            if best is None or score > best:
                best = score
    return best[2] if best else None


def _media_url_score(url: str) -> tuple[int, int]:
    size = _size_from_media_url(url)
    if size is None:
        return 0, 0
    width, height = size
    return width * height, width


def pick_best_canva_media_url(urls: list[str]) -> str | None:
    candidates = [
        url
        for url in urls
        if isinstance(url, str) and "media.canva.com" in url
    ]
    if not candidates:
        return None
    return max(set(candidates), key=_media_url_score)


def preview_image_url_from_html(html: str) -> str:
    """Return the best preview image URL embedded in a Canva page."""
    return _pick_best_media_url(html)


def _pick_best_media_url(html: str) -> str:
    bootstrap_url = _pick_best_bootstrap_url(html)
    if bootstrap_url:
        return bootstrap_url

    og_match = re.search(
        r'<meta property="og:image" content="([^"]+)"',
        html,
        flags=re.IGNORECASE,
    )
    if og_match:
        return og_match.group(1)

    best = pick_best_canva_media_url(CANVA_MEDIA_RE.findall(html))
    if best:
        return best
    raise RuntimeError("No preview image URLs found in Canva page")


def _unescape_canva_url(url: str) -> str:
    return (
        url.replace("\\/", "/")
        .replace("\\u0026", "&")
        .replace("\\u003d", "=")
        .replace("\\u003f", "?")
        .replace("&amp;", "&")
    )


def document_image_urls_from_html(html: str) -> list[str]:
    """Return composed preview URLs embedded in a Canva share page.

    These ``document-image`` files are the titled design. Element
    ``image-resize`` URLs in the same page are the untitled cover plate, so
    they are ignored.
    """
    normalized = _unescape_canva_url(html)
    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for page, raw_url in PAGE_DOCUMENT_IMAGE_RE.findall(normalized):
        url = _unescape_canva_url(raw_url)
        if url in seen:
            continue
        seen.add(url)
        area, _width = _media_url_score(url)
        ranked.append((int(page), area, url))
    for raw_url in DOCUMENT_IMAGE_RE.findall(normalized):
        url = _unescape_canva_url(raw_url)
        if url in seen:
            continue
        seen.add(url)
        area, _width = _media_url_score(url)
        ranked.append((99, area, url))
    ranked.sort(key=lambda item: (item[0] != 1, -item[1]))
    return [url for _page, _area, url in ranked]


def _download_url(url: str, destination: Path, *, timeout: int = 120) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": DEFAULT_USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = str(response.headers.get("Content-Type", "")).casefold()
        payload = response.read()
        if content_type and not content_type.startswith("image/"):
            raise urllib.error.URLError(
                f"Expected image Content-Type for {url!r}, got {content_type!r}"
            )
        destination.write_bytes(payload)


def _screen_url(canva_url: str) -> str:
    parsed = urlparse(normalize_canva_share_url(canva_url))
    parts = [part for part in parsed.path.split("/") if part]
    if (
        len(parts) >= 3
        and parts[0].casefold() == "design"
        and parts[2].casefold() not in _SHARE_PATH_STOP
    ):
        path = f"/design/{parts[1]}/{parts[2]}/screen"
        return urlunparse(parsed._replace(path=path, query="", fragment=""))
    raise RuntimeError(
        f"Could not derive Canva screen URL from {canva_url!r} "
        "(need a publish-share link with a share token)"
    )


def _preview_looks_valid(destination: Path) -> bool:
    """True when ``destination`` is a real, non-blank preview image.

    Canva often returns an HTML login/bot wall (still >1KB) for ``/screen``
    from datacenter IPs. Size-only checks accepted those and skipped the DOM
    fallback, so callers reported \"share preview was empty\".
    """
    if not destination.is_file() or destination.stat().st_size < MIN_PREVIEW_BYTES:
        return False
    try:
        from PIL import Image

        with Image.open(destination) as image:
            image.load()
            width, height = image.size
            if width < MIN_PREVIEW_EDGE or height < MIN_PREVIEW_EDGE:
                return False
    except Exception:
        return False
    from catalog_parser.drive_thumbnail import image_looks_empty

    return not image_looks_empty(destination)


def _browser_screen_enabled() -> bool:
    return os.getenv("CANVA_SHARE_PREVIEW_BROWSER", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _response_is_image(response: Any) -> bool:
    content_type = str(response.headers.get("content-type", "")).casefold()
    return response.status < 400 and content_type.startswith("image/")


def _composed_image_responses(responses: list[Any]) -> list[Any]:
    chosen = []
    for response in responses:
        if not _response_is_image(response):
            continue
        if "document-image" in response.url or response.url.rstrip("/").endswith(
            "/screen"
        ):
            chosen.append(response)
    return chosen


def download_screen_with_chromium(
    screen_url: str,
    destination: Path,
    *,
    headed: bool = False,
) -> str:
    """Open ``/screen`` in Chromium and save the composed image.

    Returns the response content type. The navigation itself is saved when it
    is an image. When Canva returns an HTML page, a composed ``document-image``
    loaded by that page is saved instead.
    """
    from playwright.sync_api import sync_playwright

    destination.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        try:
            context = browser.new_context(user_agent=DEFAULT_USER_AGENT)
            page = context.new_page()
            seen: list[Any] = []
            page.on("response", seen.append)
            response = page.goto(screen_url, wait_until="load", timeout=60_000)
            if response is not None and _response_is_image(response):
                destination.write_bytes(response.body())
                return str(response.headers.get("content-type", "")).casefold()
            composed = _composed_image_responses(seen)
            if composed:
                best = max(composed, key=lambda item: _media_url_score(item.url))
                destination.write_bytes(best.body())
                return str(best.headers.get("content-type", "")).casefold()
            if response is None:
                raise RuntimeError(f"Chromium received no response for {screen_url}")
            content_type = str(response.headers.get("content-type", "")).casefold()
            raise RuntimeError(
                f"Chromium /screen returned HTTP {response.status} "
                f"content-type {content_type or '(none)'!r} at {page.url}"
            )
        finally:
            browser.close()


def _download_screen_with_browser(url: str, destination: Path) -> bool:
    """Load ``/screen`` in Chromium when ``CANVA_SHARE_PREVIEW_BROWSER`` is set."""
    if not _browser_screen_enabled():
        return False
    try:
        download_screen_with_chromium(url, destination)
    except Exception as exc:
        print(f"  -> Chromium /screen download failed: {exc}")
        return False
    return _preview_looks_valid(destination)


def _try_screen_download(screen_url: str, destination: Path) -> bool:
    try:
        _download_url(
            screen_url,
            destination,
            timeout=_SCREEN_DOWNLOAD_TIMEOUT_SECONDS,
        )
    except urllib.error.URLError:
        return False
    return _preview_looks_valid(destination)


def _download_embedded_document_image(canva_url: str, destination: Path) -> bool:
    """Save the titled preview linked from the share page HTML.

    Datacenter addresses are often refused the ``/screen`` response itself.
    The share page still contains a signed ``document-image`` URL, and that
    file downloads normally.
    """
    try:
        html = _fetch_page_html(normalize_canva_share_url(canva_url))
    except Exception:
        return False
    for image_url in document_image_urls_from_html(html):
        try:
            _download_url(image_url, destination)
        except urllib.error.URLError:
            continue
        if _preview_looks_valid(destination):
            return True
    return False


def download_canva_share_preview(canva_url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    resolved = resolve_canva_url(canva_url)
    try:
        screen = _screen_url(resolved)
    except RuntimeError:
        screen = None
    if screen and _try_screen_download(screen, destination):
        return destination
    if _download_embedded_document_image(resolved, destination):
        return destination
    if screen and _download_screen_with_browser(screen, destination):
        return destination
    if screen:
        raise RuntimeError(f"Canva /screen image was not available for {canva_url!r}")
    raise RuntimeError(
        f"Could not derive a Canva /screen URL from {canva_url!r}"
    )


def resolve_canva_share_preview_url(canva_url: str) -> str:
    """Return the best public preview image URL from a Canva share/design link."""
    resolved = resolve_canva_url(canva_url)
    normalized = normalize_canva_share_url(resolved)
    html = _fetch_page_html(normalized)
    preview_url = _pick_best_media_url(html)
    for candidate in (preview_url, _screen_url(resolved)):
        request = urllib.request.Request(
            candidate,
            method="HEAD",
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if 200 <= response.status < 400:
                    return candidate
        except urllib.error.HTTPError:
            continue
    return preview_url
