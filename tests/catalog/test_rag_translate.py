from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from catalog_parser.translation.index import (
    Bm25Index,
    CorpusDoc,
    build_index,
    build_metadata_index,
    load_corpus_pairs,
    load_metadata_corpus_pairs,
    save_index,
    tokenize,
)
from catalog_parser.translation.rag_translate import (
    ChatConfig,
    OpenAIChatConfig,
    apply_translation_casing,
    build_batch_messages,
    build_metadata_messages,
    build_single_cue_messages,
    chat_completion,
    chat_config_from_env,
    parse_batch_translations,
    requires_all_caps,
    token_jaccard,
    translate_cue_texts,
    translate_metadata_field,
)


class Bm25IndexTests(unittest.TestCase):
    def test_tokenize_lowercases_and_splits(self) -> None:
        self.assertEqual(tokenize("Hello, World!"), ["hello", "world"])

    def test_retrieve_prefers_lexical_match(self) -> None:
        docs = [
            CorpusDoc(en="Be the boss of your life", bg="Бъдете господар", video_title="A"),
            CorpusDoc(en="How many hours he sleeps", bg="Колко часа спи", video_title="B"),
            CorpusDoc(en="Celebrate guru purnima", bg="Празнувайте", video_title="C"),
        ]
        index = Bm25Index(docs)
        hits = index.retrieve("boss of your life", k=2)
        self.assertEqual(len(hits), 1)
        self.assertIn("boss", hits[0].en.lower())
        self.assertEqual(hits[0].bg, "Бъдете господар")

        sleep_hits = index.retrieve("hours he sleeps", k=1)
        self.assertEqual(sleep_hits[0].bg, "Колко часа спи")

    def test_build_excludes_holdout_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pairs = root / "pairs.jsonl"
            holdout = root / "holdout.json"
            index_path = root / "bm25_index.json"
            pairs.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "video_title": "Keep Me",
                                "en": "inner engineering works",
                                "bg": "вътрешното инженерство работи",
                            }
                        ),
                        json.dumps(
                            {
                                "video_title": "Hold Me Out",
                                "en": "holdout only phrase",
                                "bg": "само холдаут",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            holdout.write_text(
                json.dumps(
                    {
                        "videos": [{"title": "Hold Me Out"}],
                    }
                ),
                encoding="utf-8",
            )
            docs = load_corpus_pairs(pairs, holdout_path=holdout, exclude_holdout=True)
            self.assertEqual(len(docs), 1)
            self.assertEqual(docs[0].video_title, "Keep Me")

            index = build_index(pairs, holdout_path=holdout)
            save_index(index, index_path)
            loaded = Bm25Index.from_dict(
                json.loads(index_path.read_text(encoding="utf-8"))
            )
            hits = loaded.retrieve("inner engineering", k=1)
            self.assertEqual(hits[0].bg, "вътрешното инженерство работи")
            miss = loaded.retrieve("holdout only phrase", k=1)
            self.assertEqual(miss, [])

    def test_metadata_pairs_filter_by_kind_and_holdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pairs = root / "metadata_pairs.jsonl"
            holdout = root / "holdout.json"
            pairs.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "kind": "title",
                                "video_title": "Keep Title",
                                "en": "Be the boss of your life",
                                "bg": "Бъдете господар на живота си",
                            }
                        ),
                        json.dumps(
                            {
                                "kind": "description",
                                "video_title": "Keep Title",
                                "en": "A short description about joy.",
                                "bg": "Кратко описание за радостта.",
                            }
                        ),
                        json.dumps(
                            {
                                "kind": "title",
                                "video_title": "Hold Me Out",
                                "en": "holdout title phrase",
                                "bg": "холдаут заглавие",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            holdout.write_text(
                json.dumps({"videos": [{"title": "Hold Me Out"}]}),
                encoding="utf-8",
            )
            titles = load_metadata_corpus_pairs(
                pairs, kind="title", holdout_path=holdout, exclude_holdout=True
            )
            descs = load_metadata_corpus_pairs(
                pairs, kind="description", holdout_path=holdout, exclude_holdout=True
            )
            self.assertEqual(len(titles), 1)
            self.assertEqual(titles[0].en, "Be the boss of your life")
            self.assertEqual(len(descs), 1)
            self.assertIn("joy", descs[0].en)

            index = build_metadata_index(pairs, kind="title", holdout_path=holdout)
            hits = index.retrieve("boss of your life", k=1)
            self.assertEqual(hits[0].bg, "Бъдете господар на живота си")


class RagTranslateTests(unittest.TestCase):
    def test_requires_all_caps_for_reel_and_short(self) -> None:
        self.assertTrue(requires_all_caps("Reel"))
        self.assertTrue(requires_all_caps("short"))
        self.assertFalse(requires_all_caps("Video"))
        self.assertFalse(requires_all_caps(None))
        self.assertFalse(requires_all_caps(""))

    def test_apply_translation_casing(self) -> None:
        text = "Вижте, само ако шефът е компетентен."
        self.assertEqual(
            apply_translation_casing(text, "Reel"),
            "ВИЖТЕ, САМО АКО ШЕФЪТ Е КОМПЕТЕНТЕН.",
        )
        self.assertEqual(
            apply_translation_casing(text, "Short"),
            "ВИЖТЕ, САМО АКО ШЕФЪТ Е КОМПЕТЕНТЕН.",
        )
        self.assertEqual(apply_translation_casing(text, "Video"), text)
        self.assertEqual(apply_translation_casing(text, None), text)

    def test_prompt_packing_includes_examples(self) -> None:
        from catalog_parser.translation.index import CorpusHit

        examples = [
            CorpusHit(en="Be joyful", bg="Бъдете радостни", video_title="X", score=1.0)
        ]
        messages = build_single_cue_messages("Be silent", examples)
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("Be joyful", messages[1]["content"])
        self.assertIn("Бъдете радостни", messages[1]["content"])
        self.assertIn("Be silent", messages[1]["content"])
        self.assertNotIn("ALL CAPS", messages[1]["content"])

        reel_messages = build_single_cue_messages(
            "Be silent",
            examples,
            record_type="Reel",
        )
        self.assertIn("ALL CAPS", reel_messages[1]["content"])

        video_messages = build_single_cue_messages(
            "Be silent",
            examples,
            record_type="Video",
        )
        self.assertIn("normal sentence capitalization", video_messages[1]["content"])

        batch = build_batch_messages(["One", "Two"], [examples, examples], record_type="Short")
        self.assertIn("Cue 1", batch[1]["content"])
        self.assertIn("Cue 2", batch[1]["content"])
        self.assertIn("JSON array", batch[1]["content"])
        self.assertIn("ALL CAPS", batch[1]["content"])
        self.assertIn("English to translate", batch[1]["content"])
        self.assertIn("line break", batch[1]["content"])
        self.assertIn("Previous subtitle", batch[1]["content"])

    def test_parse_batch_translations(self) -> None:
        self.assertEqual(
            parse_batch_translations('["А", "Б"]', 2),
            ["А", "Б"],
        )
        self.assertEqual(
            parse_batch_translations("```json\n[\"А\", \"Б\"]\n```", 2),
            ["А", "Б"],
        )

    def test_match_source_newlines_and_quote_repair(self) -> None:
        from catalog_parser.translation.rag_translate import (
            match_source_newlines,
            polish_subtitle_translations,
            repair_bulgarian_quotes,
        )

        self.assertEqual(
            match_source_newlines("I SAID, MA'AM", "КАЗАХ:\n\nГОСПОЖО"),
            "КАЗАХ: ГОСПОЖО",
        )
        self.assertEqual(
            match_source_newlines("LINE ONE\nLINE TWO", "РЕД ЕДИН\n\nРЕД ДВА"),
            "РЕД ЕДИН\nРЕД ДВА",
        )
        # Already-close ratios: leave the model breaks alone.
        self.assertEqual(
            match_source_newlines(
                "HOW TO LIVE\nA JOYFUL LIFE",
                "КАК ДА ЖИВЕЕМ\nРадостен Живот",
            ),
            "КАК ДА ЖИВЕЕМ\nРадостен Живот",
        )
        # Line-count mismatch: re-split by English word shares.
        self.assertEqual(
            match_source_newlines(
                "Hello\nNamaskar\nWhat do you choose?",
                "Здравей Намаскар Какво избирате?",
            ),
            "Здравей\nНамаскар\nКакво избирате?",
        )
        # Too few BG words for the English line count: do not invent orphans.
        self.assertEqual(
            match_source_newlines(
                "Soak in\nEcstasy of\nENLIGHTENMENT\nwith Sadhguru",
                "Потопете се в\nекстаза на\nПРОСВЕТЛЕНИЕТО",
            ),
            "Потопете се в\nекстаза на\nПРОСВЕТЛЕНИЕТО",
        )

        from catalog_parser.translation.rag_translate import (
            match_source_line_casing,
            parse_caption_lines_json,
        )

        self.assertEqual(
            match_source_line_casing(
                "BE JOYFUL\nALWAYS",
                "бъдете радостни\nвинаги",
            ),
            "БЪДЕТЕ РАДОСТНИ\nВИНАГИ",
        )
        self.assertEqual(
            match_source_line_casing(
                "Be Joyful Always",
                "бъдете радостни винаги",
            ),
            "Бъдете Радостни Винаги",
        )
        self.assertEqual(
            match_source_line_casing(
                "Life on the Edge",
                "Живот На Ръба",
            ),
            "Живот на Ръба",
        )
        self.assertEqual(
            match_source_line_casing(
                "Sadhguru in 2024\nLife on the Edge",
                "Садгуру През 2024\nЖивот На Ръба",
            ),
            "Садгуру през 2024\nЖивот на Ръба",
        )
        self.assertEqual(
            parse_caption_lines_json('["Line one", "Line two"]'),
            ["Line one", "Line two"],
        )
        self.assertEqual(parse_caption_lines_json("[]"), [])

        sources = [
            'I said, "ma\'am, i am well!',
            'How are you?"',
        ]
        translations = [
            "КАЗАХ: „ГОСПОЖО, АЗ\nДОБРЕ СЪМ!",
            "КАК СИ?",
        ]
        polished = polish_subtitle_translations(sources, translations)
        self.assertEqual(polished[0], "КАЗАХ: „ГОСПОЖО, АЗ ДОБРЕ СЪМ!")
        self.assertEqual(polished[1], "КАК СИ?“")

        paired = repair_bulgarian_quotes(
            ['She asked, "How are you?"'],
            ["Тя попита: КАК СИ?"],
        )
        self.assertIn("„", paired[0])
        self.assertIn("“", paired[0])

        from catalog_parser.translation.rag_translate import (
            normalize_bg_quote_punctuation,
        )

        self.assertEqual(
            normalize_bg_quote_punctuation("ПОПИТА: „КАК СТЕ“?"),
            "ПОПИТА: „КАК СТЕ?“",
        )

    def test_parse_batch_translations_repairs_bulgarian_dialogue_quotes(self) -> None:
        samples = [
            (
                '["МОЖЕ БИ ТЯ Е НАД 75.", "ТЯ ДОЙДЕ ПРИ МЕН С УСМИВКА И", '
                '"МИЛО И ПРОСТО МЕ ПОПИТА:", "„КАК СИ?"", "КАЗАХ: „ГОСПОЖО, АЗ"]',
                [
                    "МОЖЕ БИ ТЯ Е НАД 75.",
                    "ТЯ ДОЙДЕ ПРИ МЕН С УСМИВКА И",
                    "МИЛО И ПРОСТО МЕ ПОПИТА:",
                    "„КАК СИ?",
                    "КАЗАХ: „ГОСПОЖО, АЗ",
                ],
            ),
            (
                '[\n  "НЕ ВИ КАЗВАМ,",\n  "„ЯЖТЕ МНОГО СЛАДКО."",\n  '
                '"ВСИЧКО, КОЕТО ВИ КАЗВАМ, Е",\n  "НЕ СЕ БОРЕТЕ С НЕГО.",\n  '
                '"АКО СЕ СЪСРЕДОТОЧИТЕ ВЪРХУ ТОВА КАК"\n]',
                [
                    "НЕ ВИ КАЗВАМ,",
                    "„ЯЖТЕ МНОГО СЛАДКО.",
                    "ВСИЧКО, КОЕТО ВИ КАЗВАМ, Е",
                    "НЕ СЕ БОРЕТЕ С НЕГО.",
                    "АКО СЕ СЪСРЕДОТОЧИТЕ ВЪРХУ ТОВА КАК",
                ],
            ),
            (
                '["ГРЕШНО В ЖИВОТА ВИ,", "ПЪРВОТО НЕЩО Е ДА ВИДИТЕ,", '
                '"„МОЖЕ БИ АЗ СЪМ ПРИЧИНАТА ЗА ТОВА."", "ПОГЛЕДНЕТЕ ВНИМАТЕЛНО.", '
                '"АКО НЕ СТЕ ВИЕ,"]',
                [
                    "ГРЕШНО В ЖИВОТА ВИ,",
                    "ПЪРВОТО НЕЩО Е ДА ВИДИТЕ,",
                    "„МОЖЕ БИ АЗ СЪМ ПРИЧИНАТА ЗА ТОВА.",
                    "ПОГЛЕДНЕТЕ ВНИМАТЕЛНО.",
                    "АКО НЕ СТЕ ВИЕ,",
                ],
            ),
            (
                '["„ТОВА Е ЛУД ЧОВЕК."", "ЗАЩОТО", "ЗА ПОВЕЧЕТО ХОРА,", '
                '"УМЪТ ИМ НЕ МОЖЕ ДА ОСТАНЕ", "НА КАКВОТО И ДА Е ЕДНО НЕЩО"]',
                [
                    "„ТОВА Е ЛУД ЧОВЕК.",
                    "ЗАЩОТО",
                    "ЗА ПОВЕЧЕТО ХОРА,",
                    "УМЪТ ИМ НЕ МОЖЕ ДА ОСТАНЕ",
                    "НА КАКВОТО И ДА Е ЕДНО НЕЩО",
                ],
            ),
        ]
        for raw, expected in samples:
            with self.subTest(raw=raw[:40]):
                self.assertEqual(parse_batch_translations(raw, 5), expected)

    def test_translate_cue_texts_retries_singles_when_batch_json_broken(self) -> None:
        from catalog_parser.translation.index import CorpusHit

        class FakeIndex:
            def retrieve(self, query_en: str, k: int = 8) -> list[CorpusHit]:
                return [
                    CorpusHit(
                        en="similar " + query_en,
                        bg="подобно",
                        video_title="T",
                        score=1.0,
                    )
                ]

        config = OpenAIChatConfig(api_key="test-key", model="gpt-4o-mini")
        broken = MagicMock()
        broken.status_code = 200
        broken.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '["not", "valid", json]',
                    }
                }
            ]
        }
        single_a = MagicMock()
        single_a.status_code = 200
        single_a.json.return_value = {
            "choices": [{"message": {"content": "Първо"}}]
        }
        single_b = MagicMock()
        single_b.status_code = 200
        single_b.json.return_value = {
            "choices": [{"message": {"content": "Второ"}}]
        }
        fake_session = MagicMock()
        fake_session.post.side_effect = [broken, single_a, single_b]

        out = translate_cue_texts(
            ["First", "Second"],
            FakeIndex(),
            config,
            top_k=2,
            batch_size=5,
            session=fake_session,
        )
        self.assertEqual(out, ["Първо", "Второ"])
        self.assertEqual(fake_session.post.call_count, 3)

    def test_token_jaccard(self) -> None:
        self.assertEqual(token_jaccard("а б в", "а б в"), 1.0)
        self.assertGreater(token_jaccard("а б в", "а б г"), 0.4)
        self.assertEqual(token_jaccard("а", "б"), 0.0)

    def test_translate_cue_texts_mocks_http(self) -> None:
        from catalog_parser.translation.index import CorpusHit

        class FakeIndex:
            def retrieve(self, query_en: str, k: int = 8) -> list[CorpusHit]:
                return [
                    CorpusHit(
                        en="similar " + query_en,
                        bg="подобно",
                        video_title="T",
                        score=1.0,
                    )
                ]

        config = OpenAIChatConfig(api_key="test-key", model="gpt-4o-mini")
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "choices": [{"message": {"content": '["Първо", "Второ"]'}}]
        }
        fake_session = MagicMock()
        fake_session.post.return_value = fake_response

        out = translate_cue_texts(
            ["First", "Second"],
            FakeIndex(),
            config,
            top_k=2,
            batch_size=5,
            session=fake_session,
        )
        self.assertEqual(out, ["Първо", "Второ"])
        fake_session.post.assert_called_once()
        kwargs = fake_session.post.call_args.kwargs
        self.assertIn("Authorization", kwargs["headers"])
        self.assertEqual(kwargs["json"]["model"], "gpt-4o-mini")

    def test_translate_cue_texts_uppercases_reel(self) -> None:
        from catalog_parser.translation.index import CorpusHit

        class FakeIndex:
            def retrieve(self, query_en: str, k: int = 8) -> list[CorpusHit]:
                return [
                    CorpusHit(
                        en="similar",
                        bg="подобно",
                        video_title="T",
                        score=1.0,
                    )
                ]

        config = OpenAIChatConfig(api_key="test-key", model="gpt-4o-mini")
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "choices": [{"message": {"content": '["Първо", "Второ"]'}}]
        }
        fake_session = MagicMock()
        fake_session.post.return_value = fake_response

        out = translate_cue_texts(
            ["First", "Second"],
            FakeIndex(),
            config,
            top_k=2,
            batch_size=5,
            record_type="Reel",
            session=fake_session,
        )
        self.assertEqual(out, ["ПЪРВО", "ВТОРО"])
        prompt = fake_session.post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertIn("ALL CAPS", prompt)

    def test_anthropic_chat_completion_mocks_http(self) -> None:
        config = ChatConfig(
            api_key="ant-key",
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "content": [{"type": "text", "text": "Здравей"}]
        }
        fake_session = MagicMock()
        fake_session.post.return_value = fake_response

        text = chat_completion(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
            ],
            config,
            session=fake_session,
        )
        self.assertEqual(text, "Здравей")
        kwargs = fake_session.post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["x-api-key"], "ant-key")
        self.assertIn("anthropic-version", kwargs["headers"])
        self.assertEqual(kwargs["json"]["model"], "claude-sonnet-4-6")
        self.assertEqual(kwargs["json"]["system"], "sys")
        self.assertEqual(kwargs["json"]["messages"][0]["role"], "user")
        self.assertTrue(str(fake_session.post.call_args.args[0]).endswith("/v1/messages"))

    def test_chat_config_prefers_anthropic_when_key_set(self) -> None:
        env = {
            "ANTHROPIC_API_KEY": "ant-test",
            "OPENAI_API_KEY": "openai-test",
            "ANTHROPIC_MODEL": "claude-sonnet-4-6",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("TRANSLATION_PROVIDER", None)
            config = chat_config_from_env()
        self.assertEqual(config.provider, "anthropic")
        self.assertEqual(config.api_key, "ant-test")
        self.assertEqual(config.model, "claude-sonnet-4-6")

    def test_translate_metadata_field_mocks_http(self) -> None:
        from catalog_parser.translation.index import CorpusHit

        class FakeIndex:
            def retrieve(self, query_en: str, k: int = 8) -> list[CorpusHit]:
                return [
                    CorpusHit(
                        en="Be joyful",
                        bg="Бъдете радостни",
                        video_title="T",
                        score=1.0,
                    )
                ]

        config = OpenAIChatConfig(api_key="test-key", model="gpt-4o-mini")
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "choices": [{"message": {"content": "Бъдете спокойни"}}]
        }
        fake_session = MagicMock()
        fake_session.post.return_value = fake_response

        out = translate_metadata_field(
            "Be calm",
            kind="title",
            index=FakeIndex(),
            config=config,
            top_k=2,
            session=fake_session,
        )
        self.assertEqual(out, "Бъдете спокойни")
        messages = fake_session.post.call_args.kwargs["json"]["messages"]
        self.assertIn("YouTube title", messages[1]["content"])
        self.assertIn("Be joyful", messages[1]["content"])
        self.assertIn("Be calm", messages[1]["content"])

        title_messages = build_metadata_messages(
            "Hello",
            [
                CorpusHit(
                    en="Hi", bg="Здравей", video_title="X", score=1.0
                )
            ],
            kind="title",
        )
        self.assertIn("title", title_messages[1]["content"].lower())
        desc_messages = build_metadata_messages(
            "A longer description.",
            [],
            kind="description",
        )
        self.assertIn("paragraph", desc_messages[1]["content"].lower())
        caption_messages = build_metadata_messages(
            "BE JOYFUL\nALWAYS",
            [],
            kind="caption",
        )
        self.assertIn("caption", caption_messages[1]["content"].lower())
        self.assertIn("line", caption_messages[1]["content"].lower())


if __name__ == "__main__":
    unittest.main()
