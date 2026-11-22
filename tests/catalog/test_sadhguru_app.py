from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from catalog_parser.inner_engineering import frame_looks_like_inner_engineering
from catalog_parser.miracle_of_mind import frame_looks_like_miracle_of_mind
from catalog_parser.sadhguru_app import (
    frame_looks_like_sadhguru_app,
    sadhguru_app_image_kind,
)
from catalog_parser.save_soil import frame_looks_like_save_soil


def _write_app_card(path: Path) -> None:
    image = Image.new("RGB", (180, 102), (242, 236, 210))
    pixels = image.load()
    for y in range(0, 41):
        for x in range(180):
            pixels[x, y] = (234, 192, 72)
    for y in range(78, 92):
        for x in range(92, 136):
            pixels[x, y] = (8, 8, 8)
    image.save(path)


def _write_gold_title(path: Path) -> None:
    image = Image.new("RGB", (180, 102), (220, 160, 60))
    image.save(path)


def _write_navy(path: Path) -> None:
    image = Image.new("RGB", (180, 102), (40, 40, 109))
    image.save(path)


def _write_reel_card(path: Path) -> None:
    image = Image.new("RGB", (90, 160), (242, 230, 190))
    pixels = image.load()
    for y in range(0, 36):
        for x in range(90):
            pixels[x, y] = (234, 192, 72)
    for y in range(48, 68):
        for x in range(12, 78):
            pixels[x, y] = (8, 8, 8)
    for y in range(88, 141):
        for x in range(18, 74):
            pixels[x, y] = (246, 246, 246)
    image.save(path)


class SadhguruAppFrameTests(unittest.TestCase):
    def test_yellow_card_with_store_badges_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "app.jpg"
            _write_app_card(path)
            self.assertTrue(frame_looks_like_sadhguru_app(path))
            self.assertFalse(frame_looks_like_save_soil(path))
            self.assertFalse(frame_looks_like_inner_engineering(path))
            self.assertFalse(frame_looks_like_miracle_of_mind(path))

    def test_gold_title_and_other_cards_do_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            gold = root / "gold.jpg"
            navy = root / "navy.jpg"
            _write_gold_title(gold)
            _write_navy(navy)
            self.assertFalse(frame_looks_like_sadhguru_app(gold))
            self.assertFalse(frame_looks_like_sadhguru_app(navy))

    def test_portrait_card_matches_and_a_tall_gold_card_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            reel = root / "reel.jpg"
            gold = root / "gold.jpg"
            _write_reel_card(reel)
            Image.new("RGB", (90, 160), (220, 160, 60)).save(gold)
            self.assertTrue(frame_looks_like_sadhguru_app(reel))
            self.assertFalse(frame_looks_like_sadhguru_app(gold))

    def test_image_names_encode_orientation(self) -> None:
        self.assertEqual(sadhguru_app_image_kind("SadhguruAppVideo.jpg"), "horizontal")
        self.assertEqual(sadhguru_app_image_kind("SadhguruAppVideo.jpeg"), "horizontal")
        self.assertEqual(sadhguru_app_image_kind("SadhguruAppReel.jpg"), "vertical")
        self.assertIsNone(sadhguru_app_image_kind("SaveSoilVideo.jpeg"))
