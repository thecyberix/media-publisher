from __future__ import annotations

import unittest

from media_publisher.sources.tn_docx import (
    english_lines_for_render,
    extract_tn_text,
    normalize_psd_text,
)
from media_publisher.sources.tn_psd import (
    ImageSize,
    TnTextSegment,
    aspect_ratio_label,
    aspects_match,
    best_aspect_matches,
    TnLineStyle,
)
from media_publisher.sources.tn_renderer import (
    _max_upscale_target,
    _should_upscale_font,
)
from media_publisher.sources.tn_text_mapping import (
    assign_english_to_line_styles,
    consolidate_line_styles,
    map_english_to_placeholder_lines,
    _is_mahashiv_layout,
)


class TnHelperTests(unittest.TestCase):
    def test_upscale_targets_small_farm_and_krishna_lines(self) -> None:
        self.assertTrue(_should_upscale_font(16, 80))
        self.assertTrue(_should_upscale_font(40, 79))
        self.assertTrue(_should_upscale_font(35, 85))
        self.assertLess(_max_upscale_target(16, 80), 55)
        self.assertGreater(_max_upscale_target(40, 79), 70)

    def test_farm_title_formatting(self) -> None:
        from media_publisher.sources.tn_text_mapping import (
            _format_farm_title,
            apply_typography_preferences,
            prepare_layout_line_styles,
        )

        self.assertEqual(_format_farm_title("Spot the Difference"), "SPOT the Difference")
        styles = [
            TnLineStyle("SPOT the", "SPOT the", (0, 0, 100, 40), 40, "#FFFFFF", segments=(
                TnTextSegment(text="SPOT ", font_size_px=40, color_hex="#ff7a00", font_index=6),
                TnTextSegment(text="the", font_size_px=40, color_hex="#ffffff", font_index=3),
            )),
            TnLineStyle("Difference", "Difference", (0, 40, 100, 80), 37, "#FFFFFF"),
            TnLineStyle("Ian Somerhalder", "Ian Somerhalder", (0, 80, 100, 120), 16, "#FFFFFF"),
            TnLineStyle("with Sadhguru", "with Sadhguru", (0, 120, 100, 160), 16, "#FFFFFF"),
        ]
        prepared = prepare_layout_line_styles(styles)
        self.assertEqual(len(prepared), 2)
        styled = apply_typography_preferences(
            [
                TnLineStyle(
                    placeholder_text=prepared[0].placeholder_text,
                    rendered_text="Spot the Difference",
                    bbox=prepared[0].bbox,
                    font_size_px=prepared[0].font_size_px,
                    color_hex=prepared[0].color_hex,
                    segments=prepared[0].segments,
                ),
                TnLineStyle(
                    placeholder_text=prepared[1].placeholder_text,
                    rendered_text="Ian Somerhalder with Sadhguru",
                    bbox=prepared[1].bbox,
                    font_size_px=prepared[1].font_size_px,
                    color_hex=prepared[1].color_hex,
                ),
            ],
            farm_layout=True,
            krishna_layout=False,
        )
        self.assertTrue(styled[0].rendered_text.startswith("SPOT"))
        self.assertTrue(styled[0].faux_bold)
        self.assertFalse(styled[1].faux_bold)

    def test_kailash_template_layout(self) -> None:
        from media_publisher.sources.tn_text_mapping import (
            apply_typography_preferences,
            assign_english_to_line_styles,
            kailash_template_line_styles,
        )

        styles = kailash_template_line_styles(1280, 720)
        self.assertEqual(len(styles), 2)
        assigned = assign_english_to_line_styles(
            "Rapid-Fire with Sadhguru\nOcean or Mountains?",
            styles,
        )
        self.assertEqual(assigned[0].rendered_text, "Rapid-Fire with Sadhguru")
        self.assertEqual(assigned[0].block_line_parts, ("Rapid-Fire", "with Sadhguru"))
        self.assertEqual(assigned[0].stacked_line_backgrounds, (None, "#FEEEA2"))
        self.assertTrue(assigned[0].stacked_line_match_widths)
        self.assertEqual(assigned[1].block_line_parts, ("Ocean or", "Mountains?"))
        styled = apply_typography_preferences(assigned, kailash_layout=True)
        self.assertEqual(len(styled), 2)

    def test_krishna_uppercase(self) -> None:
        from media_publisher.sources.tn_text_mapping import apply_typography_preferences

        styled = apply_typography_preferences(
            [
                TnLineStyle(
                    placeholder_text="KRISHNA'S",
                    rendered_text="Krishna's",
                    bbox=(0, 0, 100, 40),
                    font_size_px=55,
                    color_hex="#FFFFFF",
                ),
                TnLineStyle(
                    placeholder_text="LIFE & MISSION",
                    rendered_text="Life & Mission",
                    bbox=(0, 40, 100, 80),
                    font_size_px=20,
                    color_hex="#FFFFFF",
                ),
            ],
            farm_layout=False,
            krishna_layout=True,
        )
        self.assertEqual(styled[0].rendered_text, "KRISHNA'S")
        self.assertEqual(styled[1].rendered_text, "LIFE & MISSION")
        self.assertFalse(styled[0].faux_bold)
        self.assertFalse(styled[1].faux_bold)
        self.assertFalse(styled[0].allow_auto_bold)

    def test_upscale_skips_shiva_and_past_lines(self) -> None:
        self.assertFalse(_should_upscale_font(104, 139))
        self.assertFalse(_should_upscale_font(150, 139))
        self.assertFalse(_should_upscale_font(78, 89))
        self.assertEqual(_max_upscale_target(104, 139), 119)
        self.assertEqual(_max_upscale_target(78, 89), 89)

    def test_upscale_increases_consciousness_block(self) -> None:
        self.assertTrue(_should_upscale_font(97, 407))
        self.assertGreater(_max_upscale_target(97, 407), 110)

    def test_normalize_psd_text(self) -> None:
        self.assertEqual(
            normalize_psd_text("Does\x03Shiva Linga\x03Look"),
            "Does\nShiva Linga\nLook",
        )

    def test_extract_tn_text(self) -> None:
        values = extract_tn_text(
            [
                ["English", "Language"],
                ["Line one", "Едно"],
                ["Line two", "Две"],
            ]
        )
        self.assertEqual(values["english"], "Line one\nLine two")
        self.assertEqual(values["bulgarian"], "Едно\nДве")

    def test_mahashiv_layout_keeps_three_title_lines(self) -> None:
        styles = [
            TnLineStyle("2025", "2025", (690, 322, 981, 409), 130, "#f6f8fa"),
            TnLineStyle("A Mystical", "A Mystical", (79, 454, 668, 581), 130, "#ffffff"),
            TnLineStyle("Magical", "Magical", (79, 581, 668, 707), 130, "#ffffff"),
            TnLineStyle("Night", "Night", (79, 707, 668, 834), 130, "#ffffff"),
        ]
        self.assertTrue(_is_mahashiv_layout(styles))
        consolidated = consolidate_line_styles(styles)
        self.assertEqual(len(consolidated), 4)

    def test_aspect_match(self) -> None:
        original = ImageSize(1280, 720, "original")
        candidate = ImageSize(1920, 1080, "artboard:YT")
        self.assertTrue(aspects_match(original, candidate))
        self.assertEqual(aspect_ratio_label(1280, 720), "16:9")

    def test_map_english_to_four_placeholder_lines(self) -> None:
        mapped = map_english_to_placeholder_lines(
            "Does Shiva Linga Look Like A Sexual Organ?",
            ("Does", "Shiva Linga", "Look Like A", "Sexual Organ?"),
        )
        self.assertEqual(len(mapped), 4)

    def test_assign_english_preserves_alignment_and_segments(self) -> None:
        styles = [
            TnLineStyle(
                placeholder_text="HOW TO SPOT the Difference",
                rendered_text="HOW TO SPOT the Difference",
                bbox=(0, 0, 400, 40),
                font_size_px=80,
                color_hex="#FFFFFF",
                alignment="center",
                segments=(
                    TnTextSegment(text="HOW TO ", font_size_px=80, color_hex="#FFFFFF"),
                    TnTextSegment(text="SPOT", font_size_px=80, color_hex="#D4AF37"),
                    TnTextSegment(text=" the ", font_size_px=80, color_hex="#FFFFFF"),
                    TnTextSegment(
                        text="Difference",
                        font_size_px=80,
                        color_hex="#FFFFFF",
                        faux_bold=True,
                    ),
                ),
            ),
        ]
        assigned = assign_english_to_line_styles(
            "How to Spot the Difference",
            styles,
        )
        self.assertEqual(len(assigned[0].segments), 4)
        self.assertEqual(assigned[0].segments[1].text.strip(), "Spot")
        self.assertEqual(assigned[0].segments[1].color_hex, "#D4AF37")
        self.assertTrue(assigned[0].segments[3].faux_bold)
        self.assertEqual(assigned[0].alignment, "center")

    def test_map_english_preserves_existing_line_breaks(self) -> None:
        mapped = map_english_to_placeholder_lines(
            "Inspiring the World\nSadhguru in 2023",
            ("Inspiring the World", "Sadhguru in 2023"),
        )
        self.assertEqual(mapped, ["Inspiring the World", "Sadhguru in 2023"])

    def test_assign_english_to_line_styles(self) -> None:
        styles = [
            TnLineStyle(
                placeholder_text="Line one",
                rendered_text="Line one",
                bbox=(0, 0, 100, 40),
                font_size_px=80,
                color_hex="#FFFFFF",
            ),
            TnLineStyle(
                placeholder_text="Line two",
                rendered_text="Line two",
                bbox=(0, 40, 100, 80),
                font_size_px=60,
                color_hex="#FFFFFF",
            ),
        ]
        assigned = assign_english_to_line_styles("Alpha\nBeta", styles)
        self.assertEqual(assigned[0].rendered_text, "Alpha")
        self.assertEqual(assigned[1].rendered_text, "Beta")
        self.assertEqual(assigned[0].font_size_px, 80)
        self.assertEqual(assigned[1].font_size_px, 60)
        self.assertEqual(assigned[0].alignment, "center")

    def test_consciousness_six_line_layout(self) -> None:
        from media_publisher.sources.tn_text_mapping import (
            apply_typography_preferences,
            assign_english_to_line_styles,
            consolidate_line_styles,
            map_english_to_placeholder_lines,
        )

        styles = [
            TnLineStyle("Is", "Is", (0, 0, 100, 20), 97, "#FFFFFF"),
            TnLineStyle("Consciousness", "Consciousness", (0, 20, 100, 40), 97, "#FFFFFF"),
            TnLineStyle("a Miracle?", "a Miracle?", (0, 40, 100, 60), 97, "#FFFFFF"),
            TnLineStyle("Prof. Steven", "Prof. Steven", (0, 60, 100, 80), 97, "#FFBB38"),
            TnLineStyle("Pinker &", "Pinker &", (0, 80, 100, 100), 97, "#FFBB38"),
            TnLineStyle("Sadhguru", "Sadhguru", (0, 100, 100, 120), 97, "#FFBB38"),
        ]
        merged = consolidate_line_styles(styles)
        self.assertEqual(len(merged), 6)

        placeholders = tuple(style.placeholder_text for style in styles)
        mapped = map_english_to_placeholder_lines(
            "Is\nConsciousness\na Miracle?\nSteven Pinker\n& Sadhguru",
            placeholders,
        )
        self.assertEqual(
            mapped,
            ["Is", "Consciousness", "a Miracle?", "Prof. Steven", "Pinker &", "Sadhguru"],
        )

        assigned = assign_english_to_line_styles(
            "Is\nConsciousness\na Miracle?\nSteven Pinker\n& Sadhguru",
            styles,
        )
        self.assertEqual(len(assigned), 6)
        self.assertEqual(assigned[3].rendered_text, "Prof. Steven")
        self.assertEqual(assigned[4].rendered_text, "Pinker &")
        self.assertEqual(assigned[5].rendered_text, "Sadhguru")

        styled = apply_typography_preferences(
            assigned,
            consciousness_layout=True,
        )
        self.assertEqual(len(styled), 6)
        self.assertEqual(styled[0].block_line_parts, ())
        self.assertGreater(styled[0].font_size_px, 97)

    def test_consciousness_uniform_font_uses_sadhguru_size(self) -> None:
        from media_publisher.sources.tn_renderer import _consciousness_uniform_font_sizes

        styles = [
            TnLineStyle("Is", "Is", (0, 0, 865, 136), 184, "#FFFFFF"),
            TnLineStyle("Consciousness", "Consciousness", (0, 136, 865, 272), 184, "#FFFFFF"),
            TnLineStyle("a Miracle?", "a Miracle?", (0, 272, 865, 408), 184, "#FFFFFF"),
            TnLineStyle("Prof. Steven", "Prof. Steven", (0, 408, 865, 544), 184, "#FFBB38"),
            TnLineStyle("Pinker &", "Pinker &", (0, 544, 865, 680), 184, "#FFBB38"),
            TnLineStyle("Sadhguru", "Sadhguru", (0, 680, 865, 816), 184, "#FFBB38"),
        ]
        uniform = _consciousness_uniform_font_sizes(styles)
        sadhguru_size = uniform[-1].fixed_font_size_px
        self.assertIsNotNone(sadhguru_size)
        for style in uniform:
            self.assertEqual(style.fixed_font_size_px, sadhguru_size)

    def test_consolidate_consciousness_title_block(self) -> None:
        styles = [
            TnLineStyle("Is", "Is", (0, 0, 100, 20), 97, "#FFFFFF"),
            TnLineStyle("Consciousness", "Consciousness", (0, 20, 100, 40), 97, "#FFFFFF"),
            TnLineStyle("a Miracle?", "a Miracle?", (0, 40, 100, 60), 97, "#FFFFFF"),
            TnLineStyle("Prof. Steven", "Prof. Steven", (0, 60, 100, 80), 97, "#FFBB38"),
            TnLineStyle("Pinker &", "Pinker &", (0, 80, 100, 100), 97, "#FFBB38"),
            TnLineStyle("Sadhguru", "Sadhguru", (0, 100, 100, 120), 97, "#FFBB38"),
        ]
        merged = consolidate_line_styles(styles)
        self.assertEqual(len(merged), 6)
        self.assertEqual(merged[0].placeholder_text, "Is")
        self.assertEqual(merged[5].placeholder_text, "Sadhguru")

    def test_map_english_two_block_merge(self) -> None:
        mapped = map_english_to_placeholder_lines(
            "Is\nConsciousness\na Miracle?\nSteven Pinker\n& Sadhguru",
            ("Is Consciousness a Miracle?", "Prof. Steven Pinker & Sadhguru"),
        )
        self.assertEqual(
            mapped,
            ["Is Consciousness a Miracle?", "Steven Pinker & Sadhguru"],
        )

    def test_assign_preserves_prof_prefix(self) -> None:
        styles = [
            TnLineStyle(
                placeholder_text="Prof. Steven Pinker & Sadhguru",
                rendered_text="Prof. Steven Pinker & Sadhguru",
                bbox=(0, 0, 100, 40),
                font_size_px=97,
                color_hex="#FFBB38",
            ),
        ]
        assigned = assign_english_to_line_styles(
            "Steven Pinker & Sadhguru",
            styles,
        )
        self.assertEqual(assigned[0].rendered_text, "Prof. Steven Pinker & Sadhguru")

        self.assertEqual(
            english_lines_for_render("Line one\nLine two"),
            ["Line one", "Line two"],
        )


if __name__ == "__main__":
    unittest.main()
