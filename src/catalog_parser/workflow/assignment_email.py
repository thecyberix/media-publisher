from __future__ import annotations

import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from catalog_parser.workflow.actions import ActionResult
from catalog_parser.workflow.config import WorkflowConfig
from catalog_parser.workflow.rules import WorkflowActionType

_ROLE_BY_ACTION = {
    WorkflowActionType.INGEST_FOR_TRANSLATOR: "translator",
    WorkflowActionType.INGEST_FOR_EDITOR: "editor",
    WorkflowActionType.ASSIGN_EDITOR: "editor",
    WorkflowActionType.ASSIGN_TIMING_EDITOR: "timing editor",
}


@dataclass(frozen=True)
class AssignmentNotice:
    name: str
    role: str
    title: str


def profile_emails(config: WorkflowConfig) -> dict[str, str]:
    """Map a profile name to its email. The first non-empty address wins."""
    found: dict[str, str] = {}
    for profile in (*config.translators, *config.editors, *config.timing_editors):
        if profile.email and profile.name not in found:
            found[profile.name] = profile.email
    return found


def assignment_notices(results: list[ActionResult]) -> list[AssignmentNotice]:
    notices: list[AssignmentNotice] = []
    for result in results:
        if not result.success or not result.assigned_titles or not result.assignee_name:
            continue
        role = _ROLE_BY_ACTION.get(result.action.action_type)
        if role is None:
            continue
        for title in result.assigned_titles:
            if title.strip():
                notices.append(
                    AssignmentNotice(
                        name=result.assignee_name,
                        role=role,
                        title=title.strip(),
                    )
                )
    return notices


def group_notices_by_email(
    notices: list[AssignmentNotice],
    emails_by_name: dict[str, str],
) -> dict[str, list[AssignmentNotice]]:
    grouped: dict[str, list[AssignmentNotice]] = defaultdict(list)
    for notice in notices:
        address = emails_by_name.get(notice.name)
        if not address:
            continue
        grouped[address].append(notice)
    return dict(grouped)


def format_assignment_email(notices: list[AssignmentNotice]) -> tuple[str, str]:
    subject = f"Sadhguru translation assignments ({len(notices)})"
    lines = ["The daily workflow assigned you the following.", ""]
    by_person: dict[tuple[str, str], list[str]] = defaultdict(list)
    order: list[tuple[str, str]] = []
    for notice in notices:
        key = (notice.name, notice.role)
        if key not in by_person:
            order.append(key)
        by_person[key].append(notice.title)
    for name, role in order:
        lines.append(f"{name} — {role}")
        for title in by_person[(name, role)]:
            lines.append(f"- {title}")
        lines.append("")
    return subject, "\n".join(lines).strip() + "\n"


def send_assignment_emails(
    config: WorkflowConfig,
    results: list[ActionResult],
    *,
    dry_run: bool,
    log=print,
) -> int:
    """Send one email per address that received assignments in this run.

    Profiles without an email are skipped. Dry-run logs the messages and
    does not contact SMTP. Returns how many messages were sent.
    """
    grouped = group_notices_by_email(
        assignment_notices(results),
        profile_emails(config),
    )
    if not grouped:
        return 0

    if dry_run:
        for address, notices in grouped.items():
            log(f"Would email {len(notices)} assignment(s) to {address}")
        return 0

    smtp_user = os.getenv("GMAIL_SMTP_USER", "").strip()
    smtp_password = os.getenv("GMAIL_SMTP_APP_PASSWORD", "").strip()
    if not smtp_user or not smtp_password:
        log(
            "WARN: assignments succeeded but notification email was not sent "
            "(check GMAIL_SMTP_USER / GMAIL_SMTP_APP_PASSWORD)."
        )
        return 0

    scripts_dir = Path(__file__).resolve().parents[3] / "scripts" / "catalog"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from send_notification_email import send_email

    sent = 0
    for address, notices in grouped.items():
        subject, body = format_assignment_email(notices)
        try:
            send_email(
                smtp_user=smtp_user,
                smtp_password=smtp_password,
                to_address=address,
                subject=subject,
                body=body,
            )
        except Exception as exc:  # noqa: BLE001 — assignment already succeeded
            log(f"WARN: assignment email to {address} failed: {exc}")
            continue
        log(f"Assignment email sent to {address} ({len(notices)} item(s)).")
        sent += 1
    return sent
