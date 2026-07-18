from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from catalog_parser.smartcat import (
    bulgarian_target_is_fully_done,
    bulgarian_target_needs_translation,
)
from catalog_parser.smartcat_export import SmartcatDocumentContext
from catalog_parser.smartcat_write import write_target_texts_from_cues
from catalog_parser.translation.prefill import (
    normalize_srt_casing,
    prefill_document_from_english,
    sentence_case_cue_text,
)
from catalog_parser.translation.srt import Cue


class GateTests(unittest.TestCase):
    def test_needs_translation_vs_fully_done(self) -> None:
        empty = {
            "workflowStages": [
                {"type": 1, "progress": 0, "wordsTranslated": 0, "translatedCharsWithoutSpaces": 0}
            ]
        }
        partial = {
            "workflowStages": [
                {
                    "type": 1,
                    "progress": 40,
                    "wordsTranslated": 10,
                    "translatedCharsWithoutSpaces": 50,
                }
            ]
        }
        done = {
            "workflowStages": [
                {
                    "type": 1,
                    "progress": 100,
                    "wordsTranslated": 100,
                    "translatedCharsWithoutSpaces": 500,
                }
            ]
        }
        self.assertTrue(bulgarian_target_needs_translation(empty))
        self.assertFalse(bulgarian_target_is_fully_done(empty))
        self.assertFalse(bulgarian_target_needs_translation(partial))
        self.assertFalse(bulgarian_target_is_fully_done(partial))
        self.assertFalse(bulgarian_target_needs_translation(done))
        self.assertTrue(bulgarian_target_is_fully_done(done))


class PrefillHelperTests(unittest.TestCase):
    def test_sentence_case(self) -> None:
        self.assertEqual(sentence_case_cue_text("HELLO WORLD"), "Hello world")
        self.assertEqual(sentence_case_cue_text("Already Mixed"), "Already Mixed")

    def test_normalize_srt_casing(self) -> None:
        srt = "1\n00:00:01,000 --> 00:00:02,000\nHELLO THERE\n"
        out = normalize_srt_casing(srt)
        self.assertIn("Hello there", out)


class WriteTargetsTests(unittest.TestCase):
    def test_write_target_texts_calls_put(self) -> None:
        client = MagicMock()
        list_payload = {
            "total": 2,
            "items": [
                {"id": 11, "targets": [{"languageId": 1026, "text": ""}]},
                {"id": 12, "targets": [{"languageId": 1026, "text": ""}]},
            ],
        }
        client.web_request.side_effect = [
            (200, json.dumps(list_payload).encode("utf-8")),
            (200, b'{"ok":true}'),
            (200, b'{"ok":true}'),
        ]
        context = SmartcatDocumentContext(
            project_id="",
            document_id="doc1",
            document_name="doc1",
            search=None,
            source_language_id="9",
            target_language_id="1026",
        )
        written = write_target_texts_from_cues(
            client,
            context,
            [
                Cue(1, "00:00:01,000", "00:00:02,000", "Първо"),
                Cue(2, "00:00:03,000", "00:00:04,000", "Второ"),
            ],
        )
        self.assertEqual(written, 2)
        put_calls = [
            call
            for call in client.web_request.call_args_list
            if call.args and call.args[0] == "PUT"
        ]
        self.assertEqual(len(put_calls), 2)
        self.assertIn("mode", put_calls[0].kwargs["params"])
        self.assertEqual(put_calls[0].kwargs["params"]["mode"], "manager")


class PrefillOrchestrationTests(unittest.TestCase):
    def test_prefill_skips_when_already_translated(self) -> None:
        client = MagicMock()
        document = {
            "id": "doc1",
            "targets": [
                {
                    "languageId": 1026,
                    "workflowStages": [
                        {
                            "type": 1,
                            "progress": 0,
                            "wordsTranslated": 0,
                            "translatedCharsWithoutSpaces": 0,
                        }
                    ],
                }
            ],
        }
        list_payload = {
            "total": 1,
            "items": [
                {
                    "id": 42,
                    "targets": [{"languageId": 1026, "text": "Вече преведено"}],
                }
            ],
        }

        def web_request(method, path, *, params=None, json_body=None):
            if method == "GET" and path.startswith("/api/Documents/"):
                return 200, json.dumps(document).encode("utf-8")
            if method == "GET" and path == "/api/Segments":
                return 200, json.dumps(list_payload).encode("utf-8")
            return 500, b"unexpected"

        client.web_request.side_effect = web_request
        context = SmartcatDocumentContext(
            project_id="",
            document_id="doc1",
            document_name="doc1",
            search=None,
            source_language_id="9",
            target_language_id="1026",
        )
        result = prefill_document_from_english(client, context)
        self.assertTrue(result.ok)
        self.assertTrue(result.skipped)

    def test_prefill_writes_when_empty(self) -> None:
        client = MagicMock()
        document = {
            "id": "doc1",
            "targets": [
                {
                    "languageId": 1026,
                    "workflowStages": [
                        {
                            "type": 1,
                            "progress": 0,
                            "wordsTranslated": 0,
                            "translatedCharsWithoutSpaces": 0,
                        }
                    ],
                }
            ],
        }
        list_payload = {
            "total": 1,
            "items": [{"id": 42, "targets": [{"languageId": 1026, "text": ""}]}],
        }
        source_srt = "1\n00:00:01,000 --> 00:00:02,000\nHello\n"

        call_count = {"segments": 0}

        def web_request(method, path, *, params=None, json_body=None):
            if method == "GET" and path.startswith("/api/Documents/"):
                return 200, json.dumps(document).encode("utf-8")
            if method == "GET" and path == "/api/Segments":
                call_count["segments"] += 1
                return 200, json.dumps(list_payload).encode("utf-8")
            if method == "PUT":
                return 200, b"{}"
            return 500, b"unexpected"

        client.web_request.side_effect = web_request
        context = SmartcatDocumentContext(
            project_id="",
            document_id="doc1",
            document_name="doc1",
            search=None,
            source_language_id="9",
            target_language_id="1026",
        )

        fake_index = MagicMock()
        with (
            patch(
                "catalog_parser.translation.prefill.export_document_srt_via_web_api",
                return_value=source_srt,
            ),
            patch(
                "catalog_parser.translation.prefill.load_or_build_index",
                return_value=fake_index,
            ),
            patch(
                "catalog_parser.translation.prefill.chat_config_from_env",
                return_value=MagicMock(),
            ),
            patch(
                "catalog_parser.translation.prefill.translate_srt_text",
                return_value="1\n00:00:01,000 --> 00:00:02,000\nЗдравей\n",
            ),
        ):
            result = prefill_document_from_english(client, context)

        self.assertTrue(result.ok)
        self.assertFalse(result.skipped)
        self.assertEqual(result.written_segments, 1)
        self.assertEqual(result.source_cues, 1)
        self.assertGreaterEqual(call_count["segments"], 2)


if __name__ == "__main__":
    unittest.main()
