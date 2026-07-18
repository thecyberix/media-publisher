"""Calibrate Smartcat cookie-based target segment writes."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from catalog_parser.__main__ import load_env_file
from catalog_parser.smartcat_export import (
    SmartcatDocumentContext,
    build_cookie_client_from_env,
)
from catalog_parser.smartcat_write import (
    list_document_segments,
    update_segment_target_text,
)

MARKER = "ТЕСТ АВТОМАТИЧЕН ПРЕВОД XYZ"


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError, AttributeError):
                pass


def main() -> int:
    configure_stdio()
    load_env_file(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--document-id",
        default="9b28e30623fdf3de63891eb4",
        help="Smartcat document id (default: Be The Boss Of Your Life)",
    )
    parser.add_argument("--language-id", type=int, default=1026)
    args = parser.parse_args()

    client = build_cookie_client_from_env(project_root=PROJECT_ROOT)
    context = SmartcatDocumentContext(
        project_id="",
        document_id=args.document_id,
        document_name=args.document_id,
        search=None,
        source_language_id="9",
        target_language_id=str(args.language_id),
    )
    segments = list_document_segments(
        client, context.document_id, args.language_id
    )
    if not segments:
        print("ERROR: no segments", file=sys.stderr)
        return 1

    first = segments[0]
    segment_id = int(first["id"])
    original = ""
    for target in first.get("targets") or []:
        if isinstance(target, dict) and int(target.get("languageId") or 0) == args.language_id:
            original = str(target.get("text") or "")
            break
    print(f"segment={segment_id} original={original[:80]!r}")

    update_segment_target_text(
        client,
        document_id=context.document_id,
        segment_id=segment_id,
        language_id=args.language_id,
        text=MARKER,
    )
    refreshed = list_document_segments(client, context.document_id, args.language_id)[0]
    now = ""
    for target in refreshed.get("targets") or []:
        if isinstance(target, dict) and int(target.get("languageId") or 0) == args.language_id:
            now = str(target.get("text") or "")
            break
    print(f"after write={now!r}")
    ok = now == MARKER

    update_segment_target_text(
        client,
        document_id=context.document_id,
        segment_id=segment_id,
        language_id=args.language_id,
        text=original,
    )
    print("restored original")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
