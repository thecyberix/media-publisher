from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog_parser.workflow.actions import ActionResult
from catalog_parser.workflow.assignment_email import (
    assignment_notices,
    format_assignment_email,
    group_notices_by_email,
    send_assignment_emails,
)
from catalog_parser.workflow.config import PersonProfile, WorkflowConfig
from catalog_parser.workflow.rules import WorkflowAction, WorkflowActionType

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "catalog"


def _person(name: str, email: str | None) -> PersonProfile:
    return PersonProfile(name=name, weekly_capacity_reels=1, email=email)


def _config() -> WorkflowConfig:
    return WorkflowConfig(
        drive_url="https://drive.google.com/drive/folders/abc",
        catalog_id="sheet",
        translators=[_person("Ada", "ada@example.com")],
        editors=[_person("Bea", "bea@example.com"), _person("NoMail", None)],
        timing_editors=[_person("Cara", "ada@example.com")],
        work_dir=Path("."),
        target_reel_to_video_ratio=6,
        max_video_seconds=900,
    )


def _result(
    action_type: WorkflowActionType,
    *,
    name: str,
    titles: tuple[str, ...],
    success: bool = True,
) -> ActionResult:
    return ActionResult(
        action=WorkflowAction(action_type=action_type, reason="test"),
        success=success,
        message="ok",
        assigned_titles=titles,
        assignee_name=name,
    )


class AssignmentEmailTests(unittest.TestCase):
    def test_one_email_lists_every_assignment_for_that_address(self) -> None:
        results = [
            _result(
                WorkflowActionType.ASSIGN_EDITOR,
                name="Bea",
                titles=("Stay Flexible",),
            ),
            _result(
                WorkflowActionType.INGEST_FOR_EDITOR,
                name="Bea",
                titles=("Investing Time", "Mental Hardship"),
            ),
            _result(
                WorkflowActionType.ASSIGN_EDITOR,
                name="NoMail",
                titles=("Skipped person",),
            ),
            _result(
                WorkflowActionType.INGEST_FOR_TRANSLATOR,
                name="Ada",
                titles=("Wisdom",),
                success=False,
            ),
            _result(
                WorkflowActionType.COMBINE_MEDIA,
                name="Bea",
                titles=("Combined",),
            ),
        ]
        grouped = group_notices_by_email(
            assignment_notices(results),
            {"Ada": "ada@example.com", "Bea": "bea@example.com"},
        )
        self.assertEqual(list(grouped), ["bea@example.com"])
        subject, body = format_assignment_email(grouped["bea@example.com"])
        self.assertEqual(subject, "Sadhguru translation assignments (3)")
        self.assertIn("Bea — editor", body)
        self.assertIn("- Stay Flexible", body)
        self.assertIn("- Investing Time", body)
        self.assertIn("- Mental Hardship", body)
        self.assertNotIn("Skipped person", body)
        self.assertNotIn("Combined", body)

    def test_shared_address_is_one_message(self) -> None:
        notices = assignment_notices(
            [
                _result(
                    WorkflowActionType.INGEST_FOR_TRANSLATOR,
                    name="Ada",
                    titles=("Reel A",),
                ),
                _result(
                    WorkflowActionType.ASSIGN_TIMING_EDITOR,
                    name="Cara",
                    titles=("Video B",),
                ),
            ]
        )
        grouped = group_notices_by_email(
            notices,
            {"Ada": "ada@example.com", "Cara": "ada@example.com"},
        )
        self.assertEqual(list(grouped), ["ada@example.com"])
        self.assertEqual(len(grouped["ada@example.com"]), 2)

    def test_send_once_per_address(self) -> None:
        if str(_SCRIPTS) not in sys.path:
            sys.path.insert(0, str(_SCRIPTS))
        results = [
            _result(
                WorkflowActionType.ASSIGN_EDITOR,
                name="Bea",
                titles=("One", "Two"),
            ),
            _result(
                WorkflowActionType.INGEST_FOR_TRANSLATOR,
                name="Ada",
                titles=("Three",),
            ),
        ]
        with patch.dict(
            os.environ,
            {
                "GMAIL_SMTP_USER": "bot@example.com",
                "GMAIL_SMTP_APP_PASSWORD": "secret",
            },
        ):
            with patch("send_notification_email.send_email") as send:
                sent = send_assignment_emails(_config(), results, dry_run=False, log=lambda *_a: None)
        self.assertEqual(sent, 2)
        self.assertEqual(send.call_count, 2)
        addresses = {call.kwargs["to_address"] for call in send.call_args_list}
        self.assertEqual(addresses, {"ada@example.com", "bea@example.com"})

    def test_dry_run_does_not_send(self) -> None:
        if str(_SCRIPTS) not in sys.path:
            sys.path.insert(0, str(_SCRIPTS))
        results = [
            _result(
                WorkflowActionType.ASSIGN_EDITOR,
                name="Bea",
                titles=("One",),
            )
        ]
        with patch("send_notification_email.send_email") as send:
            sent = send_assignment_emails(_config(), results, dry_run=True, log=lambda *_a: None)
        self.assertEqual(sent, 0)
        send.assert_not_called()
