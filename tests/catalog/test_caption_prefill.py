from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog_parser.translation.caption_prefill import (
    looks_like_manual_canva_placeholder,
    resolve_english_caption_lines,
    translate_record_caption_if_needed,
)
from catalog_parser.translation.metadata_prefill import clear_metadata_index_cache
from catalog_parser.translation.rag_translate import ChatConfig


class CaptionPrefillTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_metadata_index_cache()

    def test_looks_like_manual_canva_placeholder(self) -> None:
        self.assertTrue(
            looks_like_manual_canva_placeholder(
                ["Download this design", "manually from Canva", "DAGa81rbUOw"]
            )
        )
        self.assertFalse(
            looks_like_manual_canva_placeholder(["Soak in", "ENLIGHTENMENT"])
        )

    def test_skips_manual_canva_placeholder_caption(self) -> None:
        record: dict = {"_originalThumbnailPath": "placeholder.jpg"}
        with patch(
            "catalog_parser.translation.caption_prefill.resolve_english_caption_lines",
            return_value=(
                ["Download this design", "manually from Canva", "DAGxxx"],
                "thumbnail",
            ),
        ), patch(
            "catalog_parser.translation.caption_prefill.translate_metadata_field"
        ) as translate:
            result = translate_record_caption_if_needed(
                record,
                enabled=True,
                config=ChatConfig(api_key="test", provider="openai"),
            )
        self.assertTrue(result.skipped)
        self.assertTrue(any("placeholder" in err for err in result.errors))
        self.assertNotIn("bgCaption", record)
        translate.assert_not_called()

    def test_skips_when_disabled(self) -> None:
        record = {"_originalThumbnailPath": "missing.jpg"}
        result = translate_record_caption_if_needed(record, enabled=False)
        self.assertTrue(result.skipped)
        self.assertNotIn("bgCaption", record)

    def test_skips_when_caption_already_set(self) -> None:
        record = {"bgCaption": "Вече преведено"}
        result = translate_record_caption_if_needed(record, enabled=True)
        self.assertTrue(result.skipped)
        self.assertEqual(record["bgCaption"], "Вече преведено")

    def test_skips_when_no_english_sources(self) -> None:
        record: dict = {"pkgLink": "https://drive.google.com/drive/folders/abc"}
        with patch(
            "catalog_parser.translation.caption_prefill.extract_english_caption_from_thumbnail",
            return_value=[],
        ), patch(
            "catalog_parser.translation.caption_prefill.extract_english_caption_from_drive_tn",
            return_value=[],
        ):
            result = translate_record_caption_if_needed(
                record,
                enabled=True,
                config=ChatConfig(api_key="test", provider="openai"),
            )
        self.assertTrue(result.skipped)
        self.assertTrue(any("no English caption" in err for err in result.errors))
        self.assertNotIn("bgCaption", record)

    def test_prefers_thumbnail_over_drive_tn(self) -> None:
        record = {
            "_originalThumbnailPath": "thumb.jpg",
            "pkgLink": "https://drive.google.com/drive/folders/abc",
        }
        with patch(
            "catalog_parser.translation.caption_prefill.extract_english_caption_from_thumbnail",
            return_value=["FROM THUMB"],
        ) as thumb, patch(
            "catalog_parser.translation.caption_prefill.extract_english_caption_from_drive_tn",
            return_value=["FROM DRIVE"],
        ) as drive:
            lines, source = resolve_english_caption_lines(
                record,
                config=ChatConfig(api_key="test", provider="openai"),
                drive_service=object(),
            )
        self.assertEqual(lines, ["FROM THUMB"])
        self.assertEqual(source, "thumbnail")
        thumb.assert_called_once()
        drive.assert_not_called()

    def test_falls_back_to_drive_tn(self) -> None:
        record = {
            "pkgLink": "https://drive.google.com/drive/folders/abc",
        }
        with patch(
            "catalog_parser.translation.caption_prefill.extract_english_caption_from_thumbnail",
            return_value=[],
        ), patch(
            "catalog_parser.translation.caption_prefill.extract_english_caption_from_drive_tn",
            return_value=["Drive Line"],
        ):
            lines, source = resolve_english_caption_lines(
                record,
                config=ChatConfig(api_key="test", provider="openai"),
                drive_service=object(),
            )
        self.assertEqual(lines, ["Drive Line"])
        self.assertEqual(source, "drive_tn")

    def test_sets_bg_caption_with_stub_translator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pairs = root / "metadata_pairs.jsonl"
            holdout = root / "holdout.json"
            title_index = root / "title_index.json"
            pairs.write_text(
                json.dumps(
                    {
                        "kind": "title",
                        "video_title": "Sample",
                        "en": "Be joyful always",
                        "bg": "Бъдете винаги радостни",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            holdout.write_text(json.dumps({"videos": []}), encoding="utf-8")

            record = {"pkgLink": "https://drive.google.com/drive/folders/abc"}

            def fake_translate(en_text, *, kind, index, config, top_k=8, session=None):
                self.assertEqual(kind, "caption")
                return "БЪДЕТЕ\nРАДОСТНИ"

            with patch(
                "catalog_parser.translation.caption_prefill.resolve_english_caption_lines",
                return_value=(["BE JOYFUL", "ALWAYS"], "thumbnail"),
            ), patch(
                "catalog_parser.translation.caption_prefill.translate_metadata_field",
                side_effect=fake_translate,
            ):
                result = translate_record_caption_if_needed(
                    record,
                    enabled=True,
                    project_root=root,
                    pairs_path=pairs,
                    holdout_path=holdout,
                    title_index_path=title_index,
                    config=ChatConfig(api_key="test", provider="openai"),
                )

            self.assertTrue(result.ok)
            self.assertTrue(result.caption_translated)
            self.assertEqual(result.source, "thumbnail")
            self.assertEqual(record["bgCaption"], "БЪДЕТЕ\nРАДОСТНИ")


if __name__ == "__main__":
    unittest.main()
