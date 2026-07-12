from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(SCRIPTS_DIR))

from catalog_parser.__main__ import load_env_file
from catalog_parser.reports.weekly_work import (
    build_weekly_work_report_for_project,
    format_weekly_work_report_email,
    previous_calendar_week_range,
)

from send_notification_email import send_email


def main() -> int:
    load_env_file(PROJECT_ROOT / ".env")

    parser = argparse.ArgumentParser(
        description="Build and optionally email the weekly translation/editing report."
    )
    parser.add_argument(
        "--history-path",
        type=Path,
        default=None,
        help="Path to status_history.json (default: output/workflow/status_history.json).",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Send the report via Gmail SMTP (requires GMAIL_SMTP_* env vars).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the email subject and body without sending.",
    )
    args = parser.parse_args()

    week = previous_calendar_week_range()
    report = build_weekly_work_report_for_project(
        PROJECT_ROOT,
        week=week,
        history_path=args.history_path,
    )
    subject, body = format_weekly_work_report_email(report)

    print(subject)
    print()
    print(body)

    if args.dry_run or not args.send:
        return 0

    smtp_user = os.getenv("GMAIL_SMTP_USER", "").strip()
    smtp_password = os.getenv("GMAIL_SMTP_APP_PASSWORD", "").strip()
    notify_email = os.getenv("NOTIFY_EMAIL", "georgi.uzunov-ext@sadhguru.org").strip()
    if not smtp_user or not smtp_password:
        print("Missing GMAIL_SMTP_USER or GMAIL_SMTP_APP_PASSWORD", file=sys.stderr)
        return 1

    send_email(
        smtp_user=smtp_user,
        smtp_password=smtp_password,
        to_address=notify_email,
        subject=subject,
        body=body,
    )
    print(f"\nSent weekly report to {notify_email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
