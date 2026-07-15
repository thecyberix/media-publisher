"""Alert when a watched HappyScribe library folder still has transcriptions."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from media_publisher.sources.happyscribe import (  # noqa: E402
    HappyScribeClient,
    HappyScribeError,
    HappyScribeTranscription,
    parse_library_url,
)

DEFAULT_WATCH_LIBRARY_URL = (
    "https://www.happyscribe.com/v2/8104266/library/53816432"
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NONEMPTY = 2


def build_alert_body(
    *,
    library_url: str,
    transcriptions: list[HappyScribeTranscription],
) -> str:
    lines = [
        "The watched HappyScribe library folder is not empty.",
        "",
        f"Library: {library_url}",
        f"Items: {len(transcriptions)}",
        "",
        "Transcriptions:",
    ]
    for item in transcriptions:
        name = item.name.strip() or item.id
        lines.append(f"  - {name} ({item.id})")
    lines.extend(
        [
            "",
            "Clear or move these items in HappyScribe when ready.",
        ]
    )
    return "\n".join(lines) + "\n"


def check_library(
    *,
    api_key: str,
    library_url: str,
) -> list[HappyScribeTranscription]:
    location = parse_library_url(library_url)
    client = HappyScribeClient(api_key=api_key)
    return client.list_library_transcriptions(location)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Exit non-zero when a HappyScribe library folder still has transcriptions."
        )
    )
    parser.add_argument(
        "--library-url",
        default=(
            os.getenv("HAPPYSCRIBE_WATCH_LIBRARY_URL", "").strip()
            or DEFAULT_WATCH_LIBRARY_URL
        ),
        help="HappyScribe library URL to watch.",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("HAPPYSCRIBE_API_KEY", "").strip(),
        help="HappyScribe API key (default: HAPPYSCRIBE_API_KEY).",
    )
    parser.add_argument(
        "--skip-if-missing",
        action="store_true",
        help="Exit 0 when HAPPYSCRIBE_API_KEY is not configured.",
    )
    parser.add_argument(
        "--body-file",
        type=Path,
        help="Write a notification email body when the folder is non-empty.",
    )
    args = parser.parse_args()

    api_key = (args.api_key or "").strip()
    if not api_key:
        if args.skip_if_missing:
            print("SKIP: HAPPYSCRIBE_API_KEY not configured")
            return EXIT_OK
        print("Missing HAPPYSCRIBE_API_KEY", file=sys.stderr)
        return EXIT_ERROR

    library_url = args.library_url.strip()
    try:
        transcriptions = check_library(api_key=api_key, library_url=library_url)
    except HappyScribeError as exc:
        print(f"HappyScribe library check failed: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if not transcriptions:
        print(f"OK: HappyScribe library is empty ({library_url})")
        return EXIT_OK

    print(
        f"ALERT: HappyScribe library has {len(transcriptions)} item(s): {library_url}"
    )
    for item in transcriptions:
        print(f"  - {item.name.strip() or item.id}")

    body = build_alert_body(library_url=library_url, transcriptions=transcriptions)
    if args.body_file is not None:
        body_path = args.body_file
        if not body_path.is_absolute():
            body_path = REPO_ROOT / body_path
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_text(body, encoding="utf-8")
        print(f"Wrote alert body to {body_path}")

    return EXIT_NONEMPTY


if __name__ == "__main__":
    raise SystemExit(main())
