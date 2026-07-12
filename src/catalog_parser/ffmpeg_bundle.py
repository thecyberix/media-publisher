from __future__ import annotations

import os
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path


class FfmpegBundleError(RuntimeError):
    pass


@dataclass(frozen=True)
class FfmpegBundle:
    ffmpeg_path: Path


def _project_root() -> Path:
    # src/catalog_parser/ffmpeg_bundle.py -> project root is 2 parents up from src/
    return Path(__file__).resolve().parents[2]


def _bundle_dir() -> Path:
    return _project_root() / "tools" / "ffmpeg"


def _platform_key() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _ffmpeg_exe_name() -> str:
    return "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"


def default_bundled_ffmpeg_path() -> Path:
    return _bundle_dir() / _platform_key() / _ffmpeg_exe_name()


def ensure_ffmpeg_bundled(*, force: bool = False) -> FfmpegBundle:
    """
    Ensure an ffmpeg binary exists under tools/ffmpeg/<platform>/.

    The binary itself is not committed here; this function downloads and extracts
    a static build at runtime (useful for GitHub Actions and other headless runs).

    Override with env var FFMPEG_PATH to skip bundling.
    """
    override = os.getenv("FFMPEG_PATH", "").strip()
    if override:
        candidate = Path(override)
        if candidate.exists():
            return FfmpegBundle(ffmpeg_path=candidate)
        found = shutil.which(override)
        if found:
            return FfmpegBundle(ffmpeg_path=Path(found))
        raise FfmpegBundleError(f"FFMPEG_PATH is set but ffmpeg was not found: {override!r}")

    target = default_bundled_ffmpeg_path()
    if target.exists() and not force:
        return FfmpegBundle(ffmpeg_path=target)

    target.parent.mkdir(parents=True, exist_ok=True)

    system = _platform_key()
    machine = platform.machine().lower()
    if machine not in {"amd64", "x86_64", "x64", "arm64", "aarch64"}:
        raise FfmpegBundleError(f"Unsupported CPU architecture for ffmpeg bundle: {machine!r}")

    if system == "windows":
        # BtbN provides reliable Windows static builds (zip).
        # Note: URL points to the "latest" release artifact; pin if you need strict reproducibility.
        url = "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"
        extractor = _extract_ffmpeg_from_zip
    elif system == "linux":
        # John Van Sickle provides widely used Linux static builds (tar.xz).
        url = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
        extractor = _extract_ffmpeg_from_tar
    else:
        raise FfmpegBundleError(
            "macOS ffmpeg bundling is not configured. "
            "Set FFMPEG_PATH or install ffmpeg on PATH."
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        archive = tmp_dir / "ffmpeg_bundle"
        _download(url, archive)
        extracted = extractor(archive, tmp_dir)
        extracted.replace(target)

    # Ensure executable bit on non-Windows.
    if not sys.platform.startswith("win"):
        target.chmod(target.stat().st_mode | 0o111)

    return FfmpegBundle(ffmpeg_path=target)


def _download(url: str, destination: Path) -> None:
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
    except Exception as exc:
        raise FfmpegBundleError(f"Failed to download ffmpeg bundle from {url!r}: {exc}") from exc
    destination.write_bytes(data)


def _extract_ffmpeg_from_zip(archive_path: Path, tmp_dir: Path) -> Path:
    with zipfile.ZipFile(archive_path, "r") as zf:
        zf.extractall(tmp_dir / "unzipped")
    root = tmp_dir / "unzipped"
    candidates = list(root.rglob("ffmpeg.exe"))
    if not candidates:
        raise FfmpegBundleError("ffmpeg.exe not found in downloaded zip")
    # Prefer the first stable one.
    return candidates[0]


def _extract_ffmpeg_from_tar(archive_path: Path, tmp_dir: Path) -> Path:
    extract_dir = tmp_dir / "untarred"
    extract_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive_path, "r:*") as tf:
            tf.extractall(extract_dir)
    except Exception as exc:
        raise FfmpegBundleError(f"Failed to extract tar archive: {exc}") from exc
    candidates = list(extract_dir.rglob("ffmpeg"))
    if not candidates:
        raise FfmpegBundleError("ffmpeg not found in downloaded tar")
    return candidates[0]

