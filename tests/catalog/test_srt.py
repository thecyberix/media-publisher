from __future__ import annotations

import unittest

from catalog_parser.translation.srt import Cue, align_cues, parse_srt, write_srt


class SrtParsingTests(unittest.TestCase):
    SAMPLE = """1
00:00:01,000 --> 00:00:04,000
Hello world

2
00:00:05,000 --> 00:00:07,500
Second cue
"""

    def test_parse_and_write_roundtrip(self) -> None:
        cues = parse_srt(self.SAMPLE)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].text, "Hello world")
        roundtrip = write_srt(cues)
        self.assertIn("Hello world", roundtrip)
        self.assertEqual(parse_srt(roundtrip), cues)

    def test_parse_windows_doubled_crlf(self) -> None:
        doubled = self.SAMPLE.replace("\n", "\r\r\n")
        cues = parse_srt(doubled)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].text, "Hello world")

    def test_parse_blank_line_between_every_row(self) -> None:
        spaced = "\n\n".join(self.SAMPLE.splitlines()) + "\n"
        cues = parse_srt(spaced)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].text, "Hello world")
        self.assertEqual(cues[1].text, "Second cue")

    def test_compare_cue_timings_within_tolerance(self) -> None:
        from catalog_parser.translation.srt import compare_cue_timings

        expected = parse_srt(self.SAMPLE)
        actual = parse_srt(
            "1\n00:00:01,020 --> 00:00:04,000\nHello world\n\n"
            "2\n00:00:05,000 --> 00:00:07,500\nSecond cue\n"
        )
        self.assertEqual(compare_cue_timings(expected, actual, tolerance_ms=40), [])
        deltas = compare_cue_timings(expected, actual, tolerance_ms=0)
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0].field, "start")
        self.assertEqual(deltas[0].delta_ms, 20)

    def test_apply_cue_timings_copies_start_end(self) -> None:
        from catalog_parser.translation.srt import apply_cue_timings

        timing = parse_srt(self.SAMPLE)
        text = parse_srt(
            "1\n01:00:01,000 --> 01:00:04,000\nЗдравей свят\n\n"
            "2\n01:00:05,000 --> 01:00:07,500\nВтори субтитър\n"
        )
        combined = apply_cue_timings(timing, text)
        self.assertEqual(combined[0].start, "00:00:01,000")
        self.assertEqual(combined[0].text, "Здравей свят")
        self.assertEqual(combined[1].end, "00:00:07,500")

    def test_apply_retimed_timings_joins_unequal_cue_counts(self) -> None:
        from catalog_parser.translation.srt import apply_retimed_timings_to_target

        original = parse_srt(
            "1\n00:00:01,000 --> 00:00:02,000\nHello\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\nworld\n"
        )
        retimed = parse_srt(
            "1\n00:00:00,800 --> 00:00:01,900\nHello\n\n"
            "2\n00:00:02,100 --> 00:00:03,900\nworld\n"
        )
        target = parse_srt("1\n00:00:02,000 --> 00:00:04,000\nЗдравей свят\n")
        combined = apply_retimed_timings_to_target(original, retimed, target)
        self.assertEqual(len(combined), 1)
        self.assertEqual(combined[0].start, "00:00:00,800")
        self.assertEqual(combined[0].end, "00:00:03,900")
        self.assertEqual(combined[0].text, "Здравей свят")

    def test_align_cues_by_timestamp(self) -> None:
        source = parse_srt(self.SAMPLE)
        target = parse_srt(
            """1
00:00:01,000 --> 00:00:04,000
Здравей свят

2
00:00:05,000 --> 00:00:07,500
Втори субтитър
"""
        )
        aligned, _issues = align_cues(source, target)
        self.assertEqual(len(aligned), 2)
        self.assertEqual(aligned[0].target_text, "Здравей свят")
        self.assertEqual(aligned[0].source_text, "Hello world")

    def test_align_joins_preceding_source_fragments_to_bg_anchor(self) -> None:
        """Smartcat BG cue timing often equals only the last EN fragment."""
        source = parse_srt(
            """1
00:00:05,280 --> 00:00:07,400
You will sleep better

2
00:00:07,680 --> 00:00:09,120
because it will take away

3
00:00:09,120 --> 00:00:10,160
certain things.

4
00:00:10,160 --> 00:00:11,840
When you shower,
"""
        )
        target = parse_srt(
            """1
00:00:03,600 --> 00:00:05,200
ДА СЕ ИЗКЪПЕТЕ.

2
00:00:09,120 --> 00:00:10,160
ЩЕ СПИТЕ ПО-ДОБРЕ, ЗАЩОТО ТОВА ОТМИВА ОПРЕДЕЛЕНИ НЕЩА.

3
00:00:13,720 --> 00:00:15,200
КОГАТО СЕ КЪПЕТЕ
"""
        )
        aligned, issues = align_cues(source, target)
        sleep_pair = next(p for p in aligned if "ОПРЕДЕЛЕНИ" in p.target_text)
        self.assertEqual(
            sleep_pair.source_text,
            "You will sleep better because it will take away certain things.",
        )
        self.assertTrue(any("joined multiple source cues" in issue for issue in issues))

    def test_align_reports_count_mismatch(self) -> None:
        source = parse_srt(self.SAMPLE)
        target = [Cue(index=1, start="00:00:01,000", end="00:00:04,000", text="Един")]
        aligned, issues = align_cues(source, target)
        self.assertEqual(len(aligned), 1)
        self.assertTrue(any("cue count differs" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
