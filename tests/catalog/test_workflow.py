from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from catalog_parser.airtable import (
    FIELD_EDITOR,
    FIELD_STATUS,
    FIELD_TIMING_EDITOR,
    FIELD_TITLE,
    FIELD_TRANSLATOR,
    FIELD_TYPE,
    STATUS_EDITING_DONE,
    STATUS_TODO,
    STATUS_TRANSLATION_DONE,
)
from catalog_parser.workflow.actions import execute_action
from catalog_parser.workflow.table_cache import TableCache
from catalog_parser.workflow.comments import (
    comment_indicates_editor_ready,
    comment_indicates_translator_ready,
    extract_original_content_from_comments,
    extract_translated_content_from_comments,
    extract_value_after_prefix,
)
from catalog_parser.workflow.rules import (
    WorkflowAction,
    WorkflowActionType,
    plan_ingest_actions,
    plan_record_actions,
    resolve_assign_editor_actions,
    resolve_assign_timing_editor_actions,
)


class WorkflowCommentTests(unittest.TestCase):
    def test_translator_ready_patterns(self) -> None:
        self.assertTrue(comment_indicates_translator_ready("преведено"))
        self.assertTrue(comment_indicates_translator_ready("Translation ready"))
        self.assertFalse(comment_indicates_translator_ready("Описание:\nfoo"))

    def test_editor_ready_patterns(self) -> None:
        self.assertTrue(comment_indicates_editor_ready("редактирано"))
        self.assertTrue(comment_indicates_editor_ready("editing done"))

    def test_extract_value_after_prefix_from_next_line(self) -> None:
        value = extract_value_after_prefix(
            "Редактирано заглавие:\nЗащо връзките се провалят",
            "Редактирано заглавие:",
        )
        self.assertEqual(value, "Защо връзките се провалят")

    def test_extract_value_after_prefix_from_same_line(self) -> None:
        value = extract_value_after_prefix(
            "Преведено заглавие: Най-простото решение при запек",
            "Преведено заглавие:",
        )
        self.assertEqual(value, "Най-простото решение при запек")

    def test_extract_original_content_from_comments(self) -> None:
        comments = [
            {
                "text": "Заглавие:\nOriginal YT Title",
                "createdTime": "2026-01-01T00:00:00.000Z",
            },
            {
                "text": "Заглавие:\nUpdated YT Title",
                "createdTime": "2026-01-02T00:00:00.000Z",
            },
            {
                "text": "Описание:\nOriginal description",
                "createdTime": "2026-01-01T00:00:00.000Z",
            },
        ]
        original = extract_original_content_from_comments(comments)
        self.assertEqual(original.original_video_name, "Updated YT Title")
        self.assertEqual(original.original_video_description, "Original description")

    def test_extract_original_content_strips_sadhguru_suffix(self) -> None:
        comments = [
            {
                "text": "Заглавие:\nWhy Relationships Fail | Sadhguru",
                "createdTime": "2026-01-01T00:00:00.000Z",
            },
        ]
        original = extract_original_content_from_comments(comments)
        self.assertEqual(original.original_video_name, "Why Relationships Fail")

    def test_extract_original_content_falls_back_to_title(self) -> None:
        comments = [{"text": "Преведено заглавие:\nTranslated title"}]
        original = extract_original_content_from_comments(
            comments,
            title_fallback="Fallback Title | Sadhguru",
        )
        self.assertEqual(original.original_video_name, "Fallback Title")

    def test_extract_translated_content_prefers_edited_over_translated(self) -> None:
        comments = [
            {
                "text": "Преведено заглавие:\nПреведено заглавие",
                "createdTime": "2026-01-01T00:00:00.000Z",
            },
            {
                "text": "Редактирано заглавие:\nРедактирано заглавие",
                "createdTime": "2026-01-02T00:00:00.000Z",
            },
            {
                "text": "Преведено описание:\nПреведено описание",
                "createdTime": "2026-01-01T00:00:00.000Z",
            },
            {
                "text": "Редактирано описание:\nРедактирано описание",
                "createdTime": "2026-01-02T00:00:00.000Z",
            },
        ]
        translated = extract_translated_content_from_comments(comments)
        self.assertEqual(translated.video_name_translated, "Редактирано заглавие")
        self.assertEqual(translated.video_description_translated, "Редактирано описание")

    def test_extract_translated_content_uses_translated_fallback(self) -> None:
        comments = [
            {"text": "Преведено заглавие:\nПреведено заглавие"},
            {"text": "Преведено описание:\nПреведено описание"},
        ]
        translated = extract_translated_content_from_comments(comments)
        self.assertEqual(translated.video_name_translated, "Преведено заглавие")
        self.assertEqual(translated.video_description_translated, "Преведено описание")

    def test_translator_ready_ignores_template_title_comment(self) -> None:
        self.assertFalse(
            comment_indicates_translator_ready("Преведено заглавие:\nПреведено заглавие")
        )


class WorkflowRuleTests(unittest.TestCase):
    def test_combine_when_editing_done_without_media(self) -> None:
        record = {
            "id": "rec1",
            "fields": {
                FIELD_TITLE: "Test Video",
                "Type": "Reel",
                "Status": STATUS_EDITING_DONE,
                "Combined Media File": None,
                FIELD_TIMING_EDITOR: "Already Assigned",
            },
        }
        actions = plan_record_actions(record)
        action_types = [action.action_type.value for action in actions]
        self.assertEqual(action_types, ["combine_media"])

    def test_combine_when_editing_done_without_aligned_subtitles(self) -> None:
        record = {
            "id": "rec1d",
            "fields": {
                FIELD_TITLE: "Test Video",
                "Type": "Reel",
                "Status": STATUS_EDITING_DONE,
                "Combined Media File": "https://drive.google.com/file/d/abc/view",
                FIELD_TIMING_EDITOR: "Already Assigned",
                "Translation resources": "https://ea.smartcat.com/projects/x",
            },
        }
        actions = plan_record_actions(record)
        action_types = [action.action_type.value for action in actions]
        self.assertEqual(action_types, ["combine_media"])
        self.assertEqual(actions[0].reason, "Editing done; aligned subtitles missing")

    def test_assign_timing_editor_when_editing_done_without_timing_editor(self) -> None:
        record = {
            "id": "rec1b",
            "fields": {
                FIELD_TITLE: "Test Video",
                "Type": "Video",
                "Status": STATUS_EDITING_DONE,
                "Combined Media File": "https://drive.google.com/file/d/abc/view",
            },
        }
        actions = plan_record_actions(record)
        action_types = [action.action_type.value for action in actions]
        self.assertEqual(action_types, ["assign_timing_editor"])

    def test_assign_timing_editor_and_combine_when_editing_done(self) -> None:
        record = {
            "id": "rec1c",
            "fields": {
                FIELD_TITLE: "Test Video",
                "Type": "Reel",
                "Status": STATUS_EDITING_DONE,
                "Combined Media File": None,
            },
        }
        actions = plan_record_actions(record)
        action_types = [action.action_type.value for action in actions]
        self.assertEqual(action_types, ["assign_timing_editor", "combine_media"])

    def test_assign_editor_when_translation_done_without_editor(self) -> None:
        record = {
            "id": "rec2",
            "fields": {
                FIELD_TITLE: "Test Video",
                "Type": "Video",
                "Status": STATUS_TRANSLATION_DONE,
            },
        }
        actions = plan_record_actions(record)
        action_types = [action.action_type.value for action in actions]
        self.assertEqual(action_types, ["assign_editor"])

    def test_no_actions_when_todo(self) -> None:
        record = {
            "id": "rec3",
            "fields": {
                FIELD_TITLE: "Test Video",
                "Type": "Video",
                "Status": STATUS_TODO,
            },
        }
        self.assertEqual(plan_record_actions(record), [])


def _translator_reel_records(
    translator: str,
    count: int,
    *,
    status: str = STATUS_TODO,
) -> list[dict]:
    return [
        {
            "id": f"rec_{translator}_{index}",
            "fields": {
                FIELD_TITLE: f"Reel {translator} {index}",
                "Type": "Reel",
                "Status": status,
                "Translator": translator,
                "Duration": 60,
            },
        }
        for index in range(count)
    ]


def _editing_assignment_records(
    editor: str,
    count: int,
    *,
    record_type: str = "Reel",
) -> list[dict]:
    return [
        {
            "id": f"rec_edit_{editor}_{index}",
            "fields": {
                FIELD_TITLE: f"Editing {editor} {index}",
                "Type": record_type,
                "Status": STATUS_TRANSLATION_DONE,
                "Editor": editor,
                "Duration": 60,
            },
        }
        for index in range(count)
    ]


class IngestPlanningTests(unittest.TestCase):
    def test_no_preference_waits_for_video_capacity_instead_of_reel_fallback(self) -> None:
        records = _translator_reel_records("Genka Petrova", 12)
        actions = plan_ingest_actions(
            records,
            translators=[("Genka Petrova", 15, None)],
            target_reel_to_video_ratio=6,
            max_video_seconds=900,
        )
        self.assertEqual(actions, [])

    def test_no_preference_assigns_video_when_capacity_available(self) -> None:
        records = _translator_reel_records("Genka Petrova", 0)
        actions = plan_ingest_actions(
            records,
            translators=[("Genka Petrova", 10, None)],
            target_reel_to_video_ratio=6,
            max_video_seconds=900,
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].ingest_type, "Video")
        self.assertEqual(actions[0].ingest_count, 1)

    def test_no_preference_assigns_video_when_exact_capacity_available(self) -> None:
        records = _translator_reel_records("Genka Petrova", 5)
        actions = plan_ingest_actions(
            records,
            translators=[("Genka Petrova", 15, None)],
            target_reel_to_video_ratio=6,
            max_video_seconds=900,
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].ingest_type, "Video")
        self.assertEqual(actions[0].ingest_count, 1)

    def test_small_capacity_translator_still_ingests_reels_when_ratio_wants_video(self) -> None:
        records: list[dict] = []
        actions = plan_ingest_actions(
            records,
            translators=[("Dilyana Hayes", 4, None)],
            target_reel_to_video_ratio=6,
            max_video_seconds=900,
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].ingest_type, "Reel")
        self.assertEqual(actions[0].ingest_count, 4)

    def test_reel_ingest_uses_full_remaining_capacity(self) -> None:
        actions = plan_ingest_actions(
            [],
            translators=[("Genka Petrova", 15, "Reel")],
            target_reel_to_video_ratio=6,
            max_video_seconds=900,
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].ingest_type, "Reel")
        self.assertEqual(actions[0].ingest_count, 15)

    def test_video_can_go_to_another_translator_while_first_waits(self) -> None:
        records = _translator_reel_records("Genka Petrova", 12)
        actions = plan_ingest_actions(
            records,
            translators=[
                ("Genka Petrova", 15, None),
                ("Zhivko Zhelyazkov", 15, None),
            ],
            target_reel_to_video_ratio=6,
            max_video_seconds=900,
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].translator_name, "Zhivko Zhelyazkov")
        self.assertEqual(actions[0].ingest_type, "Video")

    def test_translation_done_does_not_block_translator_ingest(self) -> None:
        from catalog_parser.workflow.rules import count_active_translation_reel_units

        records = [
            {
                "id": "rec_video",
                "fields": {
                    FIELD_TITLE: "Finished translating",
                    "Type": "Video",
                    "Status": STATUS_TRANSLATION_DONE,
                    "Translator": "Genka Petrova",
                    "Duration": 900,
                },
            }
        ]
        self.assertEqual(count_active_translation_reel_units(records, "Genka Petrova"), 0)
        actions = plan_ingest_actions(
            records,
            translators=[("Genka Petrova", 10, None)],
            target_reel_to_video_ratio=6,
            max_video_seconds=900,
        )
        self.assertTrue(actions)
        self.assertEqual(actions[0].translator_name, "Genka Petrova")

    def test_editing_done_does_not_count_toward_editor_utilization(self) -> None:
        from catalog_parser.workflow.rules import count_active_editing_reel_units

        records = [
            {
                "id": "rec1",
                "fields": {
                    "Type": "Reel",
                    "Status": STATUS_EDITING_DONE,
                    "Editor": "Nina Rueva",
                    "Duration": 60,
                },
            },
            {
                "id": "rec2",
                "fields": {
                    "Type": "Reel",
                    "Status": STATUS_TRANSLATION_DONE,
                    "Editor": "Nina Rueva",
                    "Duration": 60,
                },
            },
        ]
        self.assertEqual(count_active_editing_reel_units(records, "Nina Rueva"), 1)

    def test_dual_role_translator_skips_ingest_while_editing_jobs_exist(self) -> None:
        records = _editing_assignment_records("Dilyana Hayes", 2)
        actions = plan_ingest_actions(
            records,
            translators=[
                ("Dilyana Hayes", 15, "Reel"),
                ("Genka Petrova", 15, None),
            ],
            target_reel_to_video_ratio=6,
            max_video_seconds=900,
            editor_names=frozenset({"Dilyana Hayes", "Nina Rueva"}),
        )
        self.assertTrue(all(action.translator_name != "Dilyana Hayes" for action in actions))
        self.assertTrue(actions)

    def test_dual_role_translator_can_ingest_when_no_editing_jobs(self) -> None:
        actions = plan_ingest_actions(
            [],
            translators=[("Dilyana Hayes", 4, "Reel")],
            target_reel_to_video_ratio=6,
            max_video_seconds=900,
            editor_names=frozenset({"Dilyana Hayes"}),
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].translator_name, "Dilyana Hayes")

    def test_same_run_editor_assignment_blocks_dual_role_translation_ingest(self) -> None:
        """Editor picks stamped before ingest must block dual-role translators."""
        records = [
            {
                "id": "rec_needs_editor",
                "fields": {
                    FIELD_TITLE: "Long video ready for editing",
                    "Type": "Video",
                    "Status": STATUS_TRANSLATION_DONE,
                    "Duration": 600,
                },
            }
        ]
        record_actions = []
        for record in records:
            record_actions.extend(plan_record_actions(record))
        self.assertEqual(
            [action.action_type for action in record_actions],
            [WorkflowActionType.ASSIGN_EDITOR],
        )

        editors = [
            ("Dilyana Hayes", 15, "Video"),
            ("Nina Rueva", 4, "Reel"),
        ]
        resolved = resolve_assign_editor_actions(records, record_actions, editors=editors)
        self.assertEqual(resolved[0].editor_name, "Dilyana Hayes")
        self.assertEqual(records[0]["fields"][FIELD_EDITOR], "Dilyana Hayes")

        ingest_actions = plan_ingest_actions(
            records,
            translators=[
                ("Dilyana Hayes", 15, "Reel"),
                ("Genka Petrova", 15, None),
            ],
            target_reel_to_video_ratio=6,
            max_video_seconds=900,
            editor_names=frozenset({"Dilyana Hayes", "Nina Rueva"}),
        )
        self.assertTrue(
            all(action.translator_name != "Dilyana Hayes" for action in ingest_actions)
        )
        self.assertTrue(ingest_actions)

    def test_preferred_editor_assigned_before_general_picks(self) -> None:
        records = [
            {
                "id": "rec_general",
                "fields": {
                    FIELD_TITLE: "General reel",
                    "Type": "Reel",
                    "Status": STATUS_TRANSLATION_DONE,
                    FIELD_TRANSLATOR: "Genka Petrova",
                    "Duration": 60,
                },
            },
            {
                "id": "rec_preferred",
                "fields": {
                    FIELD_TITLE: "Preferred video",
                    "Type": "Video",
                    "Status": STATUS_TRANSLATION_DONE,
                    FIELD_TRANSLATOR: "Tsvetelina Topuzova",
                    "Duration": 600,
                },
            },
        ]
        record_actions = []
        for record in records:
            record_actions.extend(plan_record_actions(record))

        editors = [
            ("Dilyana Hayes", 10, "Video"),
            ("Nina Rueva", 4, "Reel"),
        ]
        resolved = resolve_assign_editor_actions(
            records,
            record_actions,
            editors=editors,
            preferred_editors_by_translator={
                "Tsvetelina Topuzova": "Dilyana Hayes",
            },
        )
        editor_actions = [
            action
            for action in resolved
            if action.action_type == WorkflowActionType.ASSIGN_EDITOR
        ]
        self.assertEqual(
            [action.record_id for action in editor_actions],
            ["rec_preferred", "rec_general"],
        )
        self.assertEqual(editor_actions[0].editor_name, "Dilyana Hayes")
        self.assertIn("preferred editor", editor_actions[0].reason)
        self.assertEqual(editor_actions[1].editor_name, "Nina Rueva")
        self.assertEqual(records[1]["fields"][FIELD_EDITOR], "Dilyana Hayes")
        self.assertEqual(records[0]["fields"][FIELD_EDITOR], "Nina Rueva")

    def test_same_run_timing_editor_balances_by_utilization_and_type(self) -> None:
        records = [
            {
                "id": "rec_full",
                "fields": {
                    FIELD_TITLE: "Existing video",
                    "Type": "Video",
                    "Status": STATUS_EDITING_DONE,
                    FIELD_TIMING_EDITOR: "Timing A",
                },
            },
            {
                "id": "rec_needs_timing",
                "fields": {
                    FIELD_TITLE: "Ready for timing",
                    "Type": "Video",
                    "Status": STATUS_EDITING_DONE,
                    "Combined Media File": "https://drive.google.com/file/d/abc/view",
                },
            },
            {
                "id": "rec_reel",
                "fields": {
                    FIELD_TITLE: "Reel ready for timing",
                    "Type": "Reel",
                    "Status": STATUS_EDITING_DONE,
                    "Combined Media File": "https://drive.google.com/file/d/def/view",
                },
            },
        ]
        record_actions = []
        for record in records[1:]:
            record_actions.extend(plan_record_actions(record))
        self.assertEqual(
            [action.action_type for action in record_actions],
            [
                WorkflowActionType.ASSIGN_TIMING_EDITOR,
                WorkflowActionType.ASSIGN_TIMING_EDITOR,
            ],
        )

        timing_editors = [
            ("Timing A", 10, "Video"),  # higher utilization; still eligible
            ("Timing B", 20, "Reel"),
            ("Timing C", 20, None),
        ]
        resolved = resolve_assign_timing_editor_actions(
            records,
            record_actions,
            timing_editors=timing_editors,
        )
        self.assertEqual(resolved[0].timing_editor_name, "Timing C")
        self.assertEqual(records[1]["fields"][FIELD_TIMING_EDITOR], "Timing C")
        self.assertEqual(resolved[1].timing_editor_name, "Timing B")
        self.assertEqual(records[2]["fields"][FIELD_TIMING_EDITOR], "Timing B")

    def test_timing_video_resolved_before_reel_when_reel_listed_first(self) -> None:
        """Flexible timing editors must take waiting Videos before Reels."""
        records = [
            {
                "id": "rec_reel",
                "fields": {
                    FIELD_TITLE: "Reel ready for timing",
                    "Type": "Reel",
                    "Status": STATUS_EDITING_DONE,
                    "Combined Media File": "https://drive.google.com/file/d/def/view",
                },
            },
            {
                "id": "rec_video",
                "fields": {
                    FIELD_TITLE: "Video ready for timing",
                    "Type": "Video",
                    "Status": STATUS_EDITING_DONE,
                    "Combined Media File": "https://drive.google.com/file/d/abc/view",
                },
            },
        ]
        record_actions = []
        for record in records:
            record_actions.extend(plan_record_actions(record))
        # Reel is listed before Video in the planned action list.
        self.assertEqual(record_actions[0].record_id, "rec_reel")
        self.assertEqual(record_actions[1].record_id, "rec_video")

        timing_editors = [
            ("Timing Reel", 20, "Reel"),
            ("Timing Flex", 20, None),
        ]
        resolved = resolve_assign_timing_editor_actions(
            records,
            record_actions,
            timing_editors=timing_editors,
        )
        timing_resolved = [
            action
            for action in resolved
            if action.action_type == WorkflowActionType.ASSIGN_TIMING_EDITOR
        ]
        by_id = {action.record_id: action for action in timing_resolved}
        self.assertEqual(by_id["rec_video"].timing_editor_name, "Timing Flex")
        self.assertEqual(records[1]["fields"][FIELD_TIMING_EDITOR], "Timing Flex")
        self.assertEqual(by_id["rec_reel"].timing_editor_name, "Timing Reel")
        self.assertEqual(records[0]["fields"][FIELD_TIMING_EDITOR], "Timing Reel")

    def test_timing_editor_capacity_is_not_a_hard_cap(self) -> None:
        """Over-capacity timing editors still receive work when they match type."""
        records = [
            {
                "id": "rec_existing",
                "fields": {
                    FIELD_TITLE: "Existing video",
                    "Type": "Video",
                    "Status": STATUS_EDITING_DONE,
                    FIELD_TIMING_EDITOR: "Timing Video",
                },
            },
            {
                "id": "rec_video",
                "fields": {
                    FIELD_TITLE: "Another video",
                    "Type": "Video",
                    "Status": STATUS_EDITING_DONE,
                },
            },
        ]
        actions = [
            WorkflowAction(
                action_type=WorkflowActionType.ASSIGN_TIMING_EDITOR,
                record_id="rec_video",
                title="Another video",
                reason="needs timing",
            ),
        ]
        timing_editors = [
            ("Timing Video", 10, "Video"),
        ]
        resolved = resolve_assign_timing_editor_actions(
            records,
            actions,
            timing_editors=timing_editors,
        )
        self.assertEqual(resolved[0].timing_editor_name, "Timing Video")
        self.assertEqual(records[1]["fields"][FIELD_TIMING_EDITOR], "Timing Video")

    def test_timing_editor_balances_reels_by_utilization(self) -> None:
        records = [
            {
                "id": "rec_pad",
                "fields": {
                    FIELD_TITLE: "Pad reel",
                    "Type": "Reel",
                    "Status": STATUS_EDITING_DONE,
                    FIELD_TIMING_EDITOR: "Timing A",
                },
            },
            {
                "id": "rec_reel",
                "fields": {
                    FIELD_TITLE: "New reel",
                    "Type": "Reel",
                    "Status": STATUS_EDITING_DONE,
                },
            },
        ]
        actions = [
            WorkflowAction(
                action_type=WorkflowActionType.ASSIGN_TIMING_EDITOR,
                record_id="rec_reel",
                title="New reel",
                reason="needs timing",
            ),
        ]
        timing_editors = [
            ("Timing A", 10, "Reel"),
            ("Timing B", 10, "Reel"),
        ]
        resolved = resolve_assign_timing_editor_actions(
            records,
            actions,
            timing_editors=timing_editors,
        )
        self.assertEqual(resolved[0].timing_editor_name, "Timing B")
        self.assertEqual(records[1]["fields"][FIELD_TIMING_EDITOR], "Timing B")

class WorkflowActionTests(unittest.TestCase):
    def test_assign_editor_reuses_table_cache_without_extra_reads(self) -> None:
        table_cache = TableCache(
            [
                {
                    "id": "rec1",
                    "fields": {
                        FIELD_TITLE: "Video A",
                        "Type": "Video",
                        "Status": STATUS_TRANSLATION_DONE,
                    },
                },
                {
                    "id": "rec2",
                    "fields": {
                        FIELD_TITLE: "Video B",
                        "Type": "Video",
                        "Status": STATUS_TRANSLATION_DONE,
                        "Editor": "Nina Rueva",
                    },
                },
            ]
        )
        airtable = MagicMock()
        config = MagicMock()
        config.editors = [
            SimpleNamespace(
                name="Dilyana Hayes",
                weekly_capacity_reels=15,
                preferred_editing_type="Video",
            ),
            SimpleNamespace(
                name="Nina Rueva",
                weekly_capacity_reels=4,
                preferred_editing_type="Reel",
            ),
        ]

        action = WorkflowAction(
            action_type=WorkflowActionType.ASSIGN_EDITOR,
            record_id="rec1",
            title="Video A",
        )
        result = execute_action(
            action,
            airtable=airtable,
            config=config,
            drive_service=None,
            docs_service=None,
            credentials_path=MagicMock(),
            token_path=MagicMock(),
            dry_run=False,
            table_cache=table_cache,
        )

        self.assertTrue(result.success)
        airtable.list_records.assert_not_called()
        airtable.get_record.assert_not_called()
        airtable.update_record_fields.assert_called_once_with(
            "rec1",
            {FIELD_EDITOR: "Dilyana Hayes"},
        )
        self.assertEqual(table_cache.get("rec1")["fields"][FIELD_EDITOR], "Dilyana Hayes")

    def test_assign_timing_editor_reuses_table_cache_without_extra_reads(self) -> None:
        table_cache = TableCache(
            [
                {
                    "id": "rec1",
                    "fields": {
                        FIELD_TITLE: "Video A",
                        "Type": "Video",
                        "Status": STATUS_EDITING_DONE,
                    },
                },
                {
                    "id": "rec2",
                    "fields": {
                        FIELD_TITLE: "Video B",
                        "Type": "Video",
                        "Status": STATUS_EDITING_DONE,
                        FIELD_TIMING_EDITOR: "Timing Full",
                    },
                },
            ]
        )
        airtable = MagicMock()
        config = MagicMock()
        config.timing_editors = [
            SimpleNamespace(
                name="Timing Full",
                weekly_capacity_reels=10,
                preferred_timing_type="Video",
            ),
            SimpleNamespace(
                name="Timing Free",
                weekly_capacity_reels=20,
                preferred_timing_type="Video",
            ),
        ]

        action = WorkflowAction(
            action_type=WorkflowActionType.ASSIGN_TIMING_EDITOR,
            record_id="rec1",
            title="Video A",
        )
        result = execute_action(
            action,
            airtable=airtable,
            config=config,
            drive_service=None,
            docs_service=None,
            credentials_path=MagicMock(),
            token_path=MagicMock(),
            dry_run=False,
            table_cache=table_cache,
        )

        self.assertTrue(result.success)
        airtable.list_records.assert_not_called()
        airtable.get_record.assert_not_called()
        airtable.update_record_fields.assert_called_once_with(
            "rec1",
            {FIELD_TIMING_EDITOR: "Timing Free"},
        )
        self.assertEqual(
            table_cache.get("rec1")["fields"][FIELD_TIMING_EDITOR],
            "Timing Free",
        )


if __name__ == "__main__":
    unittest.main()
