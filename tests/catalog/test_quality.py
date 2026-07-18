from __future__ import annotations

import unittest

from catalog_parser.translation.quality import (
    looks_mostly_cyrillic,
    passes_bilingual_gates,
    score_srt_text,
    scorecard_pair,
)


class QualityTests(unittest.TestCase):
    def test_cyrillic_detection(self) -> None:
        self.assertTrue(looks_mostly_cyrillic("Здравей свят"))
        self.assertFalse(looks_mostly_cyrillic("Hello world"))

    def test_score_srt_text(self) -> None:
        srt = """1
00:00:01,000 --> 00:00:02,000
Здравей

2
00:00:02,000 --> 00:00:03,000
Hello
"""
        score = score_srt_text(srt)
        self.assertEqual(score["cue_count"], 2)
        self.assertEqual(score["cyrillic_rate"], 0.5)

    def test_passes_bilingual_gates(self) -> None:
        source = """1
00:00:01,000 --> 00:00:02,000
Hello
"""
        target_good = """1
00:00:01,000 --> 00:00:02,000
Здравей
"""
        target_bad = """1
00:00:01,000 --> 00:00:02,000
Hello
"""
        ok, _ = passes_bilingual_gates(source, target_good)
        self.assertTrue(ok)
        bad, reason = passes_bilingual_gates(source, target_bad)
        self.assertFalse(bad)
        self.assertIn("Cyrillic", reason)

    def test_scorecard_identical(self) -> None:
        srt = """1
00:00:01,000 --> 00:00:02,000
Same
"""
        card = scorecard_pair(srt, srt)
        self.assertEqual(card["identical_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
