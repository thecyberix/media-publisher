from __future__ import annotations

import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def ensure_tzdata() -> None:
    """Make IANA timezones available on Windows (tzdata is a project dependency)."""
    try:
        ZoneInfo("UTC")
        return
    except ZoneInfoNotFoundError:
        pass

    try:
        import tzdata  # noqa: F401
    except ModuleNotFoundError:
        import subprocess

        print("Installing tzdata (required for timezones on Windows)...", file=sys.stderr)
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "tzdata>=2024.1"],
        )
        import tzdata  # noqa: F401

    ZoneInfo("UTC")


def get_timezone(name: str) -> ZoneInfo:
    """Return a ZoneInfo, loading tzdata on Windows when needed."""
    ensure_tzdata()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"Timezone {name!r} is not available.") from exc
