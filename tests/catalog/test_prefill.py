from __future__ import annotations

import json
import os
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
    ai_prefill_enabled,
    normalize_srt_casing,
    prefill_document_from_english,
    sentence_case_cue_text,
)
from catalog_parser.translation.srt import Cue


class GateTests(unittest.TestCase):
    def test_ai_prefill_disabled_when_provider_is_none(self) -> None:
        env = {"SMARTCAT_AI_PREFILL": "true", "TRANSLATION_PROVIDER": "none"}
        with patch.dict(os.environ, env, clear=False):
            self.assertFalse(ai_prefill_enabled())

    def test_ai_prefill_enabled_when_prefill_on_and_provider_set(self) -> None:
        env = {"SMARTCAT_AI_PREFILL": "true", "TRANSLATION_PROVIDER": "anthropic"}
        with patch.dict(os.environ, env, clear=False):
            self.assertTrue(ai_prefill_enabled())

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

    def test_resolve_record_type(self) -> None:
        from catalog_parser.translation.prefill import resolve_record_type

        self.assertEqual(resolve_record_type({"Type": "Reel"}), "Reel")
        self.assertEqual(resolve_record_type({"ctType": "Video"}), "Video")
        self.assertIsNone(resolve_record_type({}))


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

    def test_write_joins_cues_by_subtitle_id(self) -> None:
        from catalog_parser.smartcat_write import (
            map_subtitle_tags_to_translation,
            target_text_and_tags_for_segment,
        )

        segment = {
            "id": 1033862,
            "subtitleId": [1, 2, 3],
            "source": {
                "text": "NOW WHY RUDRAKSHA",
                "tags": [
                    {
                        "tagNumber": 1,
                        "tagType": 2,
                        "position": 3,
                        "isSubtitleTag": True,
                    },
                    {
                        "tagNumber": 2,
                        "tagType": 2,
                        "position": 10,
                        "isSubtitleTag": True,
                    },
                ],
            },
        }
        cues = [
            Cue(1, "00:00:01,000", "00:00:02,000", "Сега"),
            Cue(2, "00:00:02,000", "00:00:03,000", "защо се носи рудракша"),
            Cue(3, "00:00:03,000", "00:00:04,000", "едно нещо е"),
            Cue(4, "00:00:04,000", "00:00:05,000", "игнорирай"),
        ]
        mapped = target_text_and_tags_for_segment(segment, cues, segment_index=0)
        assert mapped is not None
        text, tags = mapped
        self.assertEqual(text, "Сега\nзащо се носи рудракша\nедно нещо е")
        self.assertEqual(len(tags), 2)
        self.assertEqual(tags[0]["position"], len("Сега"))
        self.assertEqual(
            tags[1]["position"],
            len("Сега\nзащо се носи рудракша"),
        )

        en = (
            'SHE CAME SMILING AT ME SO SWEETLY AND JUST ASKED ME, "HOW ARE YOU?"'
        )
        bg = (
            "ТЯ ДОЙДЕ ПРИ МЕН С ТОЛКОВА МИЛА УСМИВКА И ПРОСТО МЕ ПОПИТА: „КАК СТЕ“?"
        )
        # EN break before the opening quote (space at index of ` "`).
        quote_pos = en.index('"')
        source_tags = [
            {
                "tagNumber": 1,
                "tagType": 2,
                "position": 25,
                "isSubtitleTag": True,
            },
            {
                "tagNumber": 2,
                "tagType": 2,
                "position": quote_pos,
                "isSubtitleTag": True,
            },
        ]
        remapped = map_subtitle_tags_to_translation(en, bg, source_tags)
        self.assertEqual(len(remapped), 2)
        self.assertEqual(bg[remapped[1]["position"]], "„")
        self.assertNotEqual(bg[remapped[1]["position"]], ":")
        # Colon stays on the previous side of the break.
        self.assertIn(":", bg[: remapped[1]["position"]])

        # Multiple „ in one segment: prefer the one after ':' for a quote break.
        en_multi = 'I THOUGHT OKAY, AND THEN I SAID, "OKAY, I AM DOING WELL.'
        bg_multi = (
            "ПОМИСЛИХ СИ „ДОБРЕ И ТОГАВА КАЗАХ: „ДОБРЕ, ЧУВСТВАМ СЕ ДОБРЕ."
        )
        quote_en = en_multi.index('"')
        multi_tags = [
            {
                "tagNumber": 1,
                "tagType": 2,
                "position": 15,
                "isSubtitleTag": True,
            },
            {
                "tagNumber": 2,
                "tagType": 2,
                "position": quote_en,
                "isSubtitleTag": True,
            },
        ]
        multi = map_subtitle_tags_to_translation(en_multi, bg_multi, multi_tags)
        self.assertEqual(bg_multi[multi[1]["position"]], "„")
        self.assertTrue(bg_multi[: multi[1]["position"]].endswith(": "))

        client = MagicMock()
        list_payload = {"total": 1, "items": [segment]}
        client.web_request.side_effect = [
            (200, json.dumps(list_payload).encode("utf-8")),
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
        written = write_target_texts_from_cues(client, context, cues)
        self.assertEqual(written, 1)
        put = [
            call
            for call in client.web_request.call_args_list
            if call.args and call.args[0] == "PUT"
        ][0]
        self.assertEqual(
            put.kwargs["json_body"]["text"],
            "Сега\nзащо се носи рудракша\nедно нещо е",
        )
        self.assertEqual(len(put.kwargs["json_body"]["tags"]), 2)


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
                    "targets": [
                        {
                            "languageId": 1026,
                            "text": "Вече преведено",
                            "isConfirmed": True,
                        }
                    ],
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

    def test_prefill_rewrites_unconfirmed_targets(self) -> None:
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
                    "source": {"text": "Hello", "tags": []},
                    "targets": [
                        {
                            "languageId": 1026,
                            "text": "Грешен чернова",
                            "isConfirmed": False,
                        }
                    ],
                }
            ],
        }

        def web_request(method, path, *, params=None, json_body=None):
            if method == "GET" and path.startswith("/api/Documents/"):
                return 200, json.dumps(document).encode("utf-8")
            if method == "GET" and path == "/api/Segments":
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
                "catalog_parser.translation.prefill.load_or_build_index",
                return_value=fake_index,
            ),
            patch(
                "catalog_parser.translation.prefill.chat_config_from_env",
                return_value=MagicMock(),
            ),
            patch(
                "catalog_parser.translation.prefill.translate_cue_texts",
                return_value=["Здравей"],
            ),
        ):
            result = prefill_document_from_english(client, context)
        self.assertTrue(result.ok)
        self.assertFalse(result.skipped)
        self.assertEqual(result.written_segments, 1)

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
            "items": [
                {
                    "id": 42,
                    "source": {"text": "HELLO", "tags": []},
                    "targets": [{"languageId": 1026, "text": ""}],
                }
            ],
        }

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
                "catalog_parser.translation.prefill.load_or_build_index",
                return_value=fake_index,
            ),
            patch(
                "catalog_parser.translation.prefill.chat_config_from_env",
                return_value=MagicMock(),
            ),
            patch(
                "catalog_parser.translation.prefill.translate_cue_texts",
                return_value=["ЗДРАВЕЙ"],
            ) as translate_mock,
        ):
            result = prefill_document_from_english(
                client, context, record_type="Reel"
            )

        self.assertTrue(result.ok)
        self.assertFalse(result.skipped)
        self.assertEqual(result.written_segments, 1)
        self.assertEqual(result.source_cues, 1)
        self.assertGreaterEqual(call_count["segments"], 1)
        self.assertEqual(
            translate_mock.call_args.kwargs.get("record_type"),
            "Reel",
        )
        self.assertEqual(translate_mock.call_args.args[0], ["Hello"])
        put = [
            call
            for call in client.web_request.call_args_list
            if call.args and call.args[0] == "PUT"
        ][0]
        self.assertIn("ЗДРАВЕЙ", put.kwargs["json_body"]["text"])

    def test_prefill_translates_distinct_overlapping_segment_sources(self) -> None:
        """Adjacent segments that share subtitleIds still get distinct EN→BG."""
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
            "total": 2,
            "items": [
                {
                    "id": 1,
                    "subtitleId": [15, 16],
                    "source": {
                        "text": "All talking about a mental health pandemic.",
                        "tags": [],
                    },
                    "targets": [{"languageId": 1026, "text": ""}],
                },
                {
                    "id": 2,
                    "subtitleId": [16, 17],
                    "source": {
                        "text": "Keeping your mental balance is challenging.",
                        "tags": [],
                    },
                    "targets": [{"languageId": 1026, "text": ""}],
                },
            ],
        }

        def web_request(method, path, *, params=None, json_body=None):
            if method == "GET" and path.startswith("/api/Documents/"):
                return 200, json.dumps(document).encode("utf-8")
            if method == "GET" and path == "/api/Segments":
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
        with (
            patch(
                "catalog_parser.translation.prefill.load_or_build_index",
                return_value=MagicMock(),
            ),
            patch(
                "catalog_parser.translation.prefill.chat_config_from_env",
                return_value=MagicMock(),
            ),
            patch(
                "catalog_parser.translation.prefill.translate_cue_texts",
                return_value=[
                    "Говорят за пандемия на психичното здраве.",
                    "Поддържането на психическия ви баланс е предизвикателство.",
                ],
            ) as translate_mock,
        ):
            result = prefill_document_from_english(
                client, context, record_type="Video"
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.written_segments, 2)
        self.assertEqual(
            translate_mock.call_args.args[0],
            [
                "All talking about a mental health pandemic.",
                "Keeping your mental balance is challenging.",
            ],
        )
        puts = [
            call.kwargs["json_body"]["text"]
            for call in client.web_request.call_args_list
            if call.args and call.args[0] == "PUT"
        ]
        self.assertEqual(
            puts,
            [
                "Говорят за пандемия на психичното здраве.",
                "Поддържането на психическия ви баланс е предизвикателство.",
            ],
        )


if __name__ == "__main__":
    unittest.main()
