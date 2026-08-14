from __future__ import annotations

import unittest

from PIL import Image

from media_publisher.sources.tn_reference import (
    _enlightenment_top_cover_vertical_bounds,
    _layout_reference_line_styles,
    extract_line_styles_from_reference_thumbnail,
    extract_top_reference_line_styles,
    has_split_top_bottom_reference_layout,
    pdf_has_baked_placeholder_text,
)


class TnReferenceTests(unittest.TestCase):
    def test_extract_line_styles_from_bottom_text(self) -> None:
        draw = Image.new("RGB", (1080, 1920))
        pixels = draw.load()
        for y in range(1500, 1560):
            for x in range(300, 780):
                pixels[x, y] = (245, 245, 245)
        for y in range(1580, 1640):
            for x in range(250, 830):
                pixels[x, y] = (245, 245, 245)

        styles = extract_line_styles_from_reference_thumbnail(
            draw,
            (1080, 1920),
            caption_line_count=2,
        )
        self.assertEqual(len(styles), 2)
        self.assertGreater(styles[0].bbox[1], 900)
        self.assertEqual(styles[0].alignment, "center")

    def test_layout_uses_uniform_body_font_and_smaller_label(self) -> None:
        bands = [
            (100, 1500, 980, 1580, "#FFFFFF", None),
            (100, 1600, 980, 1680, "#FFFFFF", None),
            (100, 1700, 980, 1780, "#FFFFFF", None),
            (650, 1850, 1020, 1920, "#FFFFFF", "#3D7992"),
        ]
        styles = _layout_reference_line_styles(bands, (1080, 1920))
        self.assertEqual(len(styles), 4)
        body_fonts = {style.font_size_px for style in styles[:3]}
        self.assertEqual(len(body_fonts), 1)
        self.assertLess(styles[3].fixed_font_size_px or 0, styles[0].font_size_px or 0)
        self.assertLess(styles[1].bbox[1] - styles[0].bbox[3], 40)

    def test_layout_left_aligns_when_english_hugs_left_edge(self) -> None:
        bands = [
            (0, 1103, 869, 1162, "#FCFAF9", "#4B2222"),
            (0, 1215, 891, 1274, "#FBF9F9", "#4B2222"),
        ]
        styles = _layout_reference_line_styles(bands, (1080, 1920))
        self.assertEqual(len(styles), 2)
        self.assertEqual(styles[0].alignment, "left")
        self.assertEqual(styles[1].alignment, "left")
        self.assertEqual(styles[0].fixed_font_size_px, styles[1].fixed_font_size_px)
        self.assertEqual(styles[0].font_size_px, styles[1].font_size_px)
        self.assertEqual(styles[0].bbox[0], 0)
        self.assertEqual(styles[1].bbox[0], 0)
        self.assertLessEqual(styles[1].bbox[1], styles[0].bbox[3])
        self.assertGreater(styles[1].bbox[3] - styles[1].bbox[1], 139)
        self.assertGreaterEqual(styles[0].bbox[2], 869)
        self.assertEqual(styles[0].stacked_line_backgrounds, ("#4B2222",))
        self.assertEqual(styles[1].stacked_line_backgrounds, ("#4B2222",))
        self.assertGreater(styles[0].font_size_px, 65.0)

    def test_pdf_has_baked_placeholder_text_detects_top_text(self) -> None:
        template = Image.new("RGB", (1080, 1920), (20, 20, 20))
        reference = Image.new("RGB", (1080, 1920), (20, 20, 20))
        template_pixels = template.load()
        for y in range(80, 180):
            for x in range(120, 960):
                template_pixels[x, y] = (250, 250, 250)

        self.assertTrue(pdf_has_baked_placeholder_text(template, reference))

    def test_pdf_has_baked_placeholder_text_ignores_bottom_reference_text(self) -> None:
        template = Image.new("RGB", (1080, 1920), (20, 20, 20))
        reference = Image.new("RGB", (1080, 1920), (20, 20, 20))
        reference_pixels = reference.load()
        for y in range(1500, 1600):
            for x in range(300, 780):
                reference_pixels[x, y] = (250, 250, 250)

        self.assertFalse(pdf_has_baked_placeholder_text(template, reference))

    def test_split_top_bottom_layout_uses_top_bands_only(self) -> None:
        draw = Image.new("RGB", (720, 1280), (40, 40, 40))
        pixels = draw.load()
        for y in range(120, 190):
            for x in range(160, 560):
                pixels[x, y] = (245, 245, 245)
        for y in range(210, 300):
            for x in range(150, 570):
                pixels[x, y] = (245, 245, 245)
        for y in range(930, 1090):
            for x in range(120, 600):
                pixels[x, y] = (245, 245, 245)

        self.assertTrue(has_split_top_bottom_reference_layout(draw, draw.size))
        styles = extract_top_reference_line_styles(
            draw,
            draw.size,
            caption_line_count=2,
        )
        self.assertEqual(len(styles), 2)
        self.assertLess(max(style.bbox[3] for style in styles), 500)
        self.assertGreater(min(style.bbox[1] for style in styles), 50)

    def test_enlightenment_layout_boosts_ecstasy_line_and_splits_sadhguru(self) -> None:
        draw = Image.new("RGB", (720, 1280), (40, 40, 40))
        pixels = draw.load()
        for y in range(118, 128):
            for x in range(160, 360):
                pixels[x, y] = (182, 156, 123)
        for y in range(129, 185):
            for x in range(155, 572):
                pixels[x, y] = (181, 156, 125)
        for y in range(217, 245):
            for x in range(142, 560):
                pixels[x, y] = (179, 155, 133)
        for y in range(270, 306):
            for x in range(171, 547):
                pixels[x, y] = (230, 182, 105)
        for y in range(986, 1026):
            for x in range(120, 600):
                pixels[x, y] = (245, 245, 245)
        for y in range(1064, 1092):
            for x in range(120, 600):
                pixels[x, y] = (245, 245, 245)

        styles = extract_top_reference_line_styles(draw, draw.size, caption_line_count=4)
        self.assertEqual(len(styles), 4)
        self.assertEqual(styles[0].fixed_font_size_px, 62.0)
        self.assertEqual(len(styles[1].segments), 2)
        self.assertEqual(styles[1].segments[1].font_size_px, 62.0)
        self.assertIsNone(styles[1].fixed_font_size_px)
        self.assertGreater(styles[1].segments[0].font_size_px, 62.0)
        self.assertEqual(styles[2].fixed_font_size_px, 62.0)
        self.assertGreater(styles[2].bbox[1], 900)
        self.assertEqual(styles[3].fixed_font_size_px, 62.0)
        self.assertGreater(styles[3].bbox[1], 1000)
        self.assertEqual(styles[3].segments[0].color_hex, "#FFFFFF")
        self.assertTrue(styles[3].segments[1].faux_bold)
        cover_top, cover_bottom = _enlightenment_top_cover_vertical_bounds(
            [
                (160, 118, 360, 128, "#B69C7B", None),
                (155, 129, 572, 185, "#B59C7D", None),
                (142, 217, 560, 245, "#B39B85", None),
                (171, 270, 547, 306, "#E6B669", None),
            ],
            draw.size,
        )
        block_top = min(styles[0].bbox[1], styles[1].bbox[1])
        block_bottom = max(styles[0].bbox[3], styles[1].bbox[3])
        cover_center = (cover_top + cover_bottom) // 2
        block_center = (block_top + block_bottom) // 2
        self.assertGreaterEqual(block_top, cover_top - 2)
        self.assertLessEqual(block_bottom, cover_bottom + 2)
        self.assertLess(abs(block_center - cover_center), 8)


if __name__ == "__main__":
    unittest.main()
