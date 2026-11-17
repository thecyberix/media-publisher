"""Send the Editing done thumbnail email for one Airtable title."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from media_publisher.runtime_env import materialize_credentials
from media_publisher.sources.airtable import apply_airtable_url_env


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_env_file(ROOT / ".env")
apply_airtable_url_env()
materialize_credentials(ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Email the Editing done thumbnail notice for one title.",
    )
    parser.add_argument("--title", required=True)
    args = parser.parse_args()

    from catalog_parser.airtable import AirtableClient
    from catalog_parser.auth import get_docs_service, get_drive_service_noninteractive
    from catalog_parser.workflow.editing_done_thumbnails import (
        collect_editing_done_missing_prepared_thumbnails,
        send_editing_done_missing_prepared_thumbnails_email,
    )

    client = AirtableClient(
        token=os.environ["AIRTABLE_TOKEN"],
        base_id=os.environ["AIRTABLE_BASE_ID"],
        table_name=os.environ["AIRTABLE_TABLE_NAME"],
    )
    record_id = client.find_record_id_by_exact_field(
        field_name="Title",
        value=args.title,
    )
    if not record_id:
        print(f"No Airtable record with title {args.title!r}", file=sys.stderr)
        return 1

    drive = get_drive_service_noninteractive()
    try:
        docs = get_docs_service(
            Path("credentials.json"),
            Path("token.json"),
            use_console=True,
        )
    except Exception as exc:
        print(f"Docs client unavailable ({exc}); continuing with Drive only")
        docs = None

    candidates = collect_editing_done_missing_prepared_thumbnails(
        records=[client.get_record(record_id)],
        drive_service=drive,
        project_root=ROOT,
        docs_service=docs,
    )
    if not candidates:
        print("Record has no Original Video Thumbnail", file=sys.stderr)
        return 1

    chosen = candidates[0].canva_design
    print(f"Canva design: {chosen or '(not set)'}")
    if not chosen:
        print("Refusing to send without a Canva design link", file=sys.stderr)
        return 1

    sent = send_editing_done_missing_prepared_thumbnails_email(candidates)
    if not sent:
        print(
            "Email was not sent (check GMAIL_SMTP_USER, "
            "GMAIL_SMTP_APP_PASSWORD, NOTIFY_EMAIL)",
            file=sys.stderr,
        )
        return 1
    print(f"Editing done email sent for {args.title!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
