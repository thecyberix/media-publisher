from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from catalog_parser.workflow.github_state import (
    WorkflowStateError,
    backup_fetched_at_matches_run,
    eligible_runs_newest_first,
    find_restored_file,
    read_backup_fetched_at,
    restore_workflow_state,
    run_is_within_max_age,
    select_previous_successful_run,
    workflow_run_from_payload,
)


def _run(
    database_id: int,
    created_at: str,
    *,
    number: int = 1,
    updated_at: str | None = None,
    started_at: str | None = None,
) -> dict:
    return {
        "databaseId": database_id,
        "createdAt": created_at,
        "updatedAt": updated_at or created_at,
        "startedAt": started_at or created_at,
        "url": f"https://github.com/example/repo/actions/runs/{database_id}",
        "number": number,
        "displayTitle": "Daily catalog workflow",
        "conclusion": "success",
    }


class SelectPreviousSuccessfulRunTests(unittest.TestCase):
    def test_picks_newest_created_at_even_if_list_is_oldest_first(self) -> None:
        payloads = [
            _run(16, "2026-09-16T00:01:09Z", number=106),
            _run(17, "2026-09-17T00:01:10Z", number=107),
            _run(18, "2026-09-18T00:01:12Z", number=108),
        ]
        selected = select_previous_successful_run(payloads, exclude_run_id=19)
        assert selected is not None
        self.assertEqual(selected.database_id, 18)

    def test_skips_current_run_id(self) -> None:
        payloads = [
            _run(19, "2026-09-19T00:01:10Z", number=109),
            _run(18, "2026-09-18T00:01:12Z", number=108),
            _run(16, "2026-09-16T00:01:09Z", number=106),
        ]
        selected = select_previous_successful_run(payloads, exclude_run_id=19)
        assert selected is not None
        self.assertEqual(selected.database_id, 18)

    def test_does_not_pick_older_rerun_when_newer_success_exists(self) -> None:
        payloads = [
            _run(104, "2026-09-14T14:11:59Z", number=104),
            _run(18, "2026-09-18T00:01:12Z", number=108),
            _run(100, "2026-09-14T07:17:24Z", number=100),
        ]
        selected = select_previous_successful_run(payloads)
        assert selected is not None
        self.assertEqual(selected.database_id, 18)
        eligible = eligible_runs_newest_first(payloads)
        self.assertEqual([run.database_id for run in eligible], [18, 104, 100])

    def test_none_when_only_current_run_is_listed(self) -> None:
        payloads = [_run(19, "2026-09-19T00:01:10Z", number=109)]
        self.assertIsNone(
            select_previous_successful_run(payloads, exclude_run_id=19)
        )


class BackupMatchesRunTests(unittest.TestCase):
    def test_fetched_at_during_run_matches(self) -> None:
        run = workflow_run_from_payload(
            _run(
                18,
                "2026-09-18T00:01:12Z",
                started_at="2026-09-18T00:01:12Z",
                updated_at="2026-09-18T00:03:52Z",
            )
        )
        fetched = datetime(2026, 9, 18, 0, 3, 38, tzinfo=timezone.utc)
        self.assertTrue(backup_fetched_at_matches_run(fetched, run))

    def test_fetched_at_from_older_day_does_not_match(self) -> None:
        run = workflow_run_from_payload(
            _run(
                18,
                "2026-09-18T00:01:12Z",
                started_at="2026-09-18T00:01:12Z",
                updated_at="2026-09-18T00:03:52Z",
            )
        )
        fetched = datetime(2026, 9, 16, 0, 3, 31, tzinfo=timezone.utc)
        self.assertFalse(backup_fetched_at_matches_run(fetched, run))

    def test_run_age_window(self) -> None:
        run = workflow_run_from_payload(_run(18, "2026-09-18T00:01:12Z"))
        now = datetime(2026, 9, 19, 0, 3, 43, tzinfo=timezone.utc)
        self.assertTrue(
            run_is_within_max_age(run, now=now, max_age=timedelta(hours=48))
        )
        self.assertFalse(
            run_is_within_max_age(run, now=now, max_age=timedelta(hours=12))
        )


class RestoredFileAndBackupTests(unittest.TestCase):
    def test_finds_upload_artifact_v4_layout_and_legacy_output_prefix(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            modern = root / "workflow" / "status_history.json"
            modern.parent.mkdir(parents=True)
            modern.write_text("[]", encoding="utf-8")
            self.assertEqual(
                find_restored_file(root, "workflow/status_history.json"),
                modern,
            )
            legacy_root = root / "legacy"
            legacy = legacy_root / "output" / "backups" / "airtable-latest.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("{}", encoding="utf-8")
            self.assertEqual(
                find_restored_file(legacy_root, "backups/airtable-latest.json"),
                legacy,
            )

    def test_read_backup_fetched_at(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "airtable-latest.json"
            path.write_text(
                json.dumps({"fetched_at": "2026-09-18T00:03:38.826507+00:00"}),
                encoding="utf-8",
            )
            fetched = read_backup_fetched_at(path)
            self.assertEqual(fetched.tzinfo, timezone.utc)
            self.assertEqual(fetched.day, 18)

    def test_read_backup_invalid_json(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "airtable-latest.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(WorkflowStateError):
                read_backup_fetched_at(path)


class RestoreWorkflowStateScriptTests(unittest.TestCase):
    def test_restore_copies_files_and_rejects_mismatched_backup(self) -> None:

        payloads = [
            _run(
                16,
                "2026-09-16T00:01:09Z",
                number=106,
                started_at="2026-09-16T00:01:09Z",
                updated_at="2026-09-16T00:03:42Z",
            ),
            _run(
                18,
                "2026-09-18T00:01:12Z",
                number=108,
                started_at="2026-09-18T00:01:12Z",
                updated_at="2026-09-18T00:03:52Z",
            ),
        ]

        def fake_list(workflow: str, *, limit: int) -> list[dict]:
            self.assertEqual(workflow, "catalog-daily-workflow.yml")
            self.assertGreaterEqual(limit, 2)
            return payloads

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            extract_dir = tmp / "extract"
            output_root = tmp / "output"

            def fake_download(*, run_id: int, artifact: str, extract_dir: Path) -> None:
                self.assertEqual(run_id, 18)
                self.assertEqual(artifact, "workflow-state")
                extract_dir.mkdir(parents=True, exist_ok=True)
                backup = extract_dir / "backups" / "airtable-latest.json"
                backup.parent.mkdir(parents=True)
                backup.write_text(
                    json.dumps(
                        {
                            "fetched_at": "2026-09-16T00:03:31.464684+00:00",
                            "records": [],
                        }
                    ),
                    encoding="utf-8",
                )
                history = extract_dir / "workflow" / "status_history.json"
                history.parent.mkdir(parents=True)
                history.write_text("[]", encoding="utf-8")

            with self.assertRaises(WorkflowStateError) as ctx:
                restore_workflow_state(
                    workflow="catalog-daily-workflow.yml",
                    exclude_run_id=19,
                    artifact="workflow-state",
                    extract_dir=extract_dir,
                    output_root=output_root,
                    required=[
                        "backups/airtable-latest.json",
                        "workflow/status_history.json",
                    ],
                    optional=[],
                    list_limit=20,
                    max_run_age=timedelta(hours=72),
                    warn_run_age=timedelta(hours=36),
                    backup_slack=timedelta(hours=2),
                    now=datetime(2026, 9, 19, 0, 3, 43, tzinfo=timezone.utc),
                    list_runs=fake_list,
                    download=fake_download,
                )
            self.assertIn("does not match selected run 18", str(ctx.exception))

    def test_restore_succeeds_when_backup_matches_selected_run(self) -> None:

        payloads = [
            _run(
                18,
                "2026-09-18T00:01:12Z",
                number=108,
                started_at="2026-09-18T00:01:12Z",
                updated_at="2026-09-18T00:03:52Z",
            )
        ]

        def fake_list(workflow: str, *, limit: int) -> list[dict]:
            return payloads

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            extract_dir = tmp / "extract"
            output_root = tmp / "output"

            def fake_download(*, run_id: int, artifact: str, extract_dir: Path) -> None:
                extract_dir.mkdir(parents=True, exist_ok=True)
                backup = extract_dir / "backups" / "airtable-latest.json"
                backup.parent.mkdir(parents=True)
                backup.write_text(
                    json.dumps(
                        {
                            "fetched_at": "2026-09-18T00:03:38.826507+00:00",
                            "records": [],
                        }
                    ),
                    encoding="utf-8",
                )
                history = extract_dir / "workflow" / "status_history.json"
                history.parent.mkdir(parents=True)
                history.write_text("[]", encoding="utf-8")

            selected = restore_workflow_state(
                workflow="catalog-daily-workflow.yml",
                exclude_run_id=19,
                artifact="workflow-state",
                extract_dir=extract_dir,
                output_root=output_root,
                required=[
                    "backups/airtable-latest.json",
                    "workflow/status_history.json",
                ],
                optional=["workflow/editor_last_assigned.json"],
                list_limit=20,
                max_run_age=timedelta(hours=72),
                warn_run_age=timedelta(hours=36),
                backup_slack=timedelta(hours=2),
                now=datetime(2026, 9, 19, 0, 3, 43, tzinfo=timezone.utc),
                list_runs=fake_list,
                download=fake_download,
            )
            self.assertEqual(selected.database_id, 18)
            self.assertTrue(
                (output_root / "backups" / "airtable-latest.json").is_file()
            )
            self.assertTrue(
                (output_root / "workflow" / "status_history.json").is_file()
            )

    def test_restore_fails_when_selected_run_is_too_old(self) -> None:

        payloads = [_run(16, "2026-09-16T00:01:09Z", number=106)]

        with self.assertRaises(WorkflowStateError) as ctx:
            restore_workflow_state(
                workflow="catalog-daily-workflow.yml",
                exclude_run_id=19,
                artifact="workflow-state",
                extract_dir=Path("unused"),
                output_root=Path("unused"),
                required=["backups/airtable-latest.json"],
                optional=[],
                list_limit=20,
                max_run_age=timedelta(hours=48),
                warn_run_age=timedelta(hours=36),
                backup_slack=timedelta(hours=2),
                now=datetime(2026, 9, 19, 0, 3, 43, tzinfo=timezone.utc),
                list_runs=lambda workflow, *, limit: payloads,
                download=lambda **kwargs: None,
            )
        self.assertIn("older than", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
