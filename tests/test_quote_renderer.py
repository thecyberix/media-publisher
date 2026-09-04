from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from media_publisher.sources.quote_layouts import load_quote_layout_config
from media_publisher.sources.quote_renderer import (
    select_render_plan,
    wrap_quote_text,
    wrap_quote_text_balanced,
    load_font,
    resolve_font_path,
)
from media_publisher.sources.quotes_sheet import parse_quote_sheet_date


class QuoteSheetDateTests(unittest.TestCase):
    def test_parse_quote_sheet_date(self) -> None:
        from datetime import date

        self.assertEqual(parse_quote_sheet_date("1 Jul 2026"), date(2026, 7, 1))
        self.assertEqual(parse_quote_sheet_date("31 July 2022"), date(2022, 7, 31))
        self.assertEqual(parse_quote_sheet_date("09 December 2024"), date(2024, 12, 9))
        self.assertEqual(parse_quote_sheet_date("12-Apr-24"), date(2024, 4, 12))
        self.assertEqual(parse_quote_sheet_date("02-Jun-20"), date(2020, 6, 2))
        self.assertIsNone(parse_quote_sheet_date(""))
        self.assertIsNone(parse_quote_sheet_date("not a date"))


class QuoteRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.font_path = resolve_font_path()
        except RuntimeError:
            raise unittest.SkipTest("No serif font available for renderer tests")

    def test_wrap_and_select_single_line_layout(self) -> None:
        root = Path(__file__).resolve().parents[1]
        layout_config = load_quote_layout_config(
            root / "config" / "quote_layouts_fbyt.json",
            template_dir=root / "config" / "quote_templates" / "fbyt",
        )
        text = "Кратък цитат."
        plan = select_render_plan(
            layout_config,
            text,
            font_path=self.font_path,
        )
        self.assertEqual(plan.layout_key, "1")
        self.assertEqual(plan.lines, (text,))

    def test_wrap_respects_max_width(self) -> None:
        font = load_font(22, font_path=self.font_path)
        lines = wrap_quote_text(
            "Това е по-дълъг български цитат, който трябва да се пренесе на нов ред.",
            font=font,
            max_width=300,
        )
        self.assertGreaterEqual(len(lines), 2)

    def test_balanced_wrap_prefers_even_line_lengths(self) -> None:
        font = load_font(22, font_path=self.font_path)
        text = (
            "Това е по-дълъг български цитат, който трябва да се пренесе "
            "на няколко реда с по-равномерна дължина."
        )
        greedy = wrap_quote_text(text, font=font, max_width=300)
        balanced = wrap_quote_text_balanced(
            text,
            font=font,
            max_width=300,
            line_count=len(greedy),
        )
        self.assertEqual(len(balanced), len(greedy))

        def widths(lines: list[str]) -> list[int]:
            return [_measure_width(font, line) for line in lines]

        def _measure_width(fnt, line: str) -> int:
            from PIL import Image, ImageDraw

            image = Image.new("RGB", (1, 1))
            draw = ImageDraw.Draw(image)
            box = draw.textbbox((0, 0), line, font=fnt)
            return box[2] - box[0]

        greedy_spread = max(widths(greedy)) - min(widths(greedy))
        balanced_spread = max(widths(balanced)) - min(widths(balanced))
        self.assertLessEqual(balanced_spread, greedy_spread)


if __name__ == "__main__":
    unittest.main()
