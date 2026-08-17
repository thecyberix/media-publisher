from __future__ import annotations

import unittest

from catalog_parser.translation.srt import Cue, ms_to_timestamp, parse_srt, write_srt
from catalog_parser.translation.srt_retime import (
    WordTiming,
    detect_hour_offset_ms,
    format_retime_summary,
    retime_cues,
    retimed_cues,
    shift_cues,
)


class MsTimestampTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        self.assertEqual(ms_to_timestamp(4_120), "00:00:04,120")
        self.assertEqual(ms_to_timestamp(3_661_001), "01:01:01,001")


class RetimeCuesTests(unittest.TestCase):
    def test_shifts_cues_to_spoken_words(self) -> None:
        cues = parse_srt(
            "1\n00:00:01,000 --> 00:00:02,000\nHello world\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\nSecond cue\n"
        )
        words = [
            WordTiming("Hello", 520, 780),
            WordTiming("world", 800, 1100),
            WordTiming("Second", 2500, 2800),
            WordTiming("cue", 2820, 3000),
        ]
        results = retime_cues(cues, words, pad_start_ms=80, pad_end_ms=120)
        self.assertTrue(all(item.used_audio for item in results))
        self.assertEqual(results[0].cue.start, "00:00:00,440")
        self.assertEqual(results[0].cue.end, "00:00:01,220")
        self.assertEqual(results[1].cue.start, "00:00:02,420")
        self.assertEqual(results[1].cue.end, "00:00:03,120")
        rewritten = write_srt(retimed_cues(results))
        self.assertIn("Hello world", rewritten)

    def test_keeps_original_when_match_is_weak(self) -> None:
        cues = [Cue(1, "00:00:01,000", "00:00:02,000", "Unrelated caption text")]
        words = [WordTiming("hello", 0, 200), WordTiming("there", 210, 400)]
        results = retime_cues(cues, words)
        self.assertFalse(results[0].used_audio)
        self.assertEqual(results[0].cue.start, "00:00:01,000")
        self.assertEqual(results[0].cue.end, "00:00:02,000")

    def test_summary_counts_retimed_cues(self) -> None:
        cues = parse_srt("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
        words = [WordTiming("Hello", 400, 700)]
        summary = format_retime_summary(retime_cues(cues, words))
        self.assertIn("retimed=1", summary)
        self.assertIn("kept_original=0", summary)

    def test_hour_offset_when_cues_start_after_audio(self) -> None:
        cues = parse_srt("1\n01:00:01,159 --> 01:00:03,400\nHello\n")
        offset = detect_hour_offset_ms(cues, audio_duration_ms=90_000)
        self.assertEqual(offset, 3_600_000)
        shifted = shift_cues(cues, -offset)
        self.assertEqual(shifted[0].start, "00:00:01,159")
        self.assertEqual(shifted[0].end, "00:00:03,400")

    def test_no_hour_offset_when_cues_fit_audio(self) -> None:
        cues = parse_srt("1\n00:00:01,000 --> 00:00:03,000\nHello\n")
        self.assertEqual(detect_hour_offset_ms(cues, 90_000), 0)


class WhisperXCacheTests(unittest.TestCase):
    def test_warm_loads_align_model_once(self) -> None:
        from unittest.mock import patch

        from catalog_parser.translation import srt_retime

        srt_retime._WHISPERX_ALIGN_MODEL_CACHE.clear()
        fake_model = object()
        dummy = type("W", (), {})()
        dummy.load_align_model = staticmethod(
            lambda language_code, device: (
                fake_model,
                {"lang": language_code, "device": device},
            )
        )
        with patch.dict("sys.modules", {"whisperx": dummy}):
            first = srt_retime.load_whisperx_align_model(language="en", device="cpu")
            second = srt_retime.load_whisperx_align_model(language="en", device="cpu")
        self.assertIs(first[0], fake_model)
        self.assertIs(second[0], fake_model)
        self.assertEqual(first[1]["lang"], "en")
        srt_retime._WHISPERX_ALIGN_MODEL_CACHE.clear()
