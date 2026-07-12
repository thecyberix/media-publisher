"""Download preview images from public Canva publish-share links."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from media_publisher.sources.canva import resolve_canva_url

CANVA_MEDIA_RE = re.compile(r"https://media\.canva\.com/v2/image-resize/[^\"'\s<>]+")
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


def normalize_canva_share_url(canva_url: str) -> str:
    parsed = urlparse(canva_url)
    path = parsed.path
    if "/edit" in path:
        path = path.replace("/edit", "/view", 1)
    elif "/view" not in path and "/design/" in path:
        parts = path.rstrip("/").split("/")
        if len(parts) >= 4:
            path = "/".join(parts[:4] + ["view"])
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

    candidates = CANVA_MEDIA_RE.findall(html)
    if not candidates:
        raise RuntimeError("No preview image URLs found in Canva page")

    def score(url: str) -> tuple[int, int]:
        width = height = 0
        for part in url.split("/"):
            if part.startswith("width:"):
                width = int(part.split(":", 1)[1].split("?", 1)[0])
            if part.startswith("height:"):
                height = int(part.split(":", 1)[1].split("?", 1)[0])
        return width * height, width

    return max(set(candidates), key=score)


def _download_url(url: str, destination: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; media-publisher/1.0)"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        destination.write_bytes(response.read())


def _screen_url(canva_url: str) -> str:
    parsed = urlparse(normalize_canva_share_url(canva_url))
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 3 and parts[0] == "design":
        path = f"/design/{parts[1]}/{parts[2]}/screen"
        return urlunparse(parsed._replace(path=path, query="", fragment=""))
    raise RuntimeError(f"Could not derive Canva screen URL from {canva_url!r}")


def download_canva_share_preview(canva_url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    resolved = resolve_canva_url(canva_url)
    normalized = normalize_canva_share_url(resolved)
    html = _fetch_dom(normalized)
    preview_url = _pick_best_media_url(html)
    for candidate in (preview_url, _screen_url(resolved)):
        try:
            _download_url(candidate, destination)
            return destination
        except urllib.error.HTTPError:
            continue
    raise RuntimeError(f"Could not download Canva preview for {canva_url!r}")


def resolve_canva_share_preview_url(canva_url: str) -> str:
    """Return the best public preview image URL from a Canva share/design link."""
    resolved = resolve_canva_url(canva_url)
    normalized = normalize_canva_share_url(resolved)
    html = _fetch_dom(normalized)
    preview_url = _pick_best_media_url(html)
    for candidate in (preview_url, _screen_url(resolved)):
        request = urllib.request.Request(
            candidate,
            method="HEAD",
            headers={"User-Agent": "Mozilla/5.0 (compatible; media-publisher/1.0)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if 200 <= response.status < 400:
                    return candidate
        except urllib.error.HTTPError:
            continue
    return preview_url
