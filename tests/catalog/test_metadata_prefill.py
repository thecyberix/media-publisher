from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog_parser.translation.metadata_prefill import (
    clear_metadata_index_cache,
    translate_record_metadata_if_needed,
)
from catalog_parser.translation.rag_translate import ChatConfig


class MetadataPrefillTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_metadata_index_cache()

    def test_skips_when_disabled(self) -> None:
        record = {"ytTitle": "Hello", "ytDescription": "World"}
        result = translate_record_metadata_if_needed(record, enabled=False)
        self.assertTrue(result.skipped)
        self.assertNotIn("bgTitle", record)

    def test_skips_when_translation_provider_is_none(self) -> None:
        record = {"ytTitle": "Hello", "ytDescription": "World"}
        with patch.dict(os.environ, {"TRANSLATION_PROVIDER": "none"}, clear=False):
            result = translate_record_metadata_if_needed(record)
        self.assertTrue(result.skipped)
        self.assertNotIn("bgTitle", record)

    def test_skips_when_no_english(self) -> None:
        record: dict = {"ctTitle": ""}
        result = translate_record_metadata_if_needed(record, enabled=True)
        self.assertTrue(result.skipped)
        self.assertTrue(any("no English" in err for err in result.errors))

    def test_sets_bg_title_with_stub_translator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pairs = root / "metadata_pairs.jsonl"
            holdout = root / "holdout.json"
            title_index = root / "title_index.json"
            desc_index = root / "desc_index.json"
            pairs.write_text(
                json.dumps(
                    {
                        "kind": "title",
                        "video_title": "Sample",
                        "en": "Be joyful always",
                        "bg": "Бъдете винаги радостни",
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "kind": "description",
                        "video_title": "Sample",
                        "en": "A short note about joy.",
                        "bg": "Кратка бележка за радостта.",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            holdout.write_text(json.dumps({"videos": []}), encoding="utf-8")

            record = {
                "ytTitle": "Be joyful always today",
                "ytDescription": "A short note about joy in life.",
            }
            config = ChatConfig(api_key="test", provider="openai")

            def fake_translate(en_text, *, kind, index, config, top_k=8, session=None):
                if kind == "title":
                    return "Бъдете радостни днес"
                return "Кратка бележка за радостта в живота."

            with patch(
                "catalog_parser.translation.metadata_prefill.translate_metadata_field",
                side_effect=fake_translate,
            ):
                result = translate_record_metadata_if_needed(
                    record,
                    enabled=True,
                    config=config,
                    pairs_path=pairs,
                    holdout_path=holdout,
                    title_index_path=title_index,
                    description_index_path=desc_index,
                )

            self.assertTrue(result.title_translated)
            self.assertTrue(result.description_translated)
            self.assertEqual(record["bgTitle"], "Бъдете радостни днес")
            self.assertEqual(
                record["bgDescription"], "Кратка бележка за радостта в живота."
            )
            self.assertEqual(result.errors, [])


if __name__ == "__main__":
    unittest.main()
