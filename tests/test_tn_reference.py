from __future__ import annotations

import unittest

from PIL import Image

from media_publisher.sources.tn_reference import (
    _layout_reference_line_styles,
    extract_line_styles_from_reference_thumbnail,
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


if __name__ == "__main__":
    unittest.main()
