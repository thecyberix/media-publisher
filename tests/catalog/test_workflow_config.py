from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog_parser.workflow.config import load_catalog_id, load_workflow_config


PROFILES = {
    "translators": [{"name": "T", "weekly_capacity_reels": 4}],
    "editors": [{"name": "E", "weekly_capacity_reels": 4}],
    "timing_editors": [{"name": "TE", "weekly_capacity_reels": 4}],
}


def _write_shared(root: Path, payload: dict) -> None:
    config_dir = root / "config"
    config_dir.mkdir()
    (config_dir / "workflow_config.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


class LoadCatalogIdTests(unittest.TestCase):
    def test_shared_config_catalog_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_shared(root, {"catalog_id": "sheetFromFile01"})
            self.assertEqual(load_catalog_id(root), "sheetFromFile01")

    def test_missing_catalog_id_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(RuntimeError):
                load_catalog_id(Path(tmpdir))


class LoadWorkflowConfigTests(unittest.TestCase):
    def test_shared_ratio_and_max_video_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_shared(
                root,
                {
                    "catalog_id": "sheetFromFile01",
                    "target_reel_to_video_ratio": 6,
                    "max_video_seconds": 900,
                },
            )
            (root / "workflow_config.json").write_text(
                json.dumps(
                    {
                        "drive_url": "https://drive.google.com/drive/folders/abc",
                        "profiles": PROFILES,
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"DRIVE_URL": "", "WORKFLOW_PROFILES_JSON": ""},
                clear=False,
            ):
                config = load_workflow_config(root)
            self.assertEqual(config.catalog_id, "sheetFromFile01")
            self.assertEqual(config.target_reel_to_video_ratio, 6)
            self.assertEqual(config.max_video_seconds, 900)

    def test_missing_ratio_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_shared(
                root, {"catalog_id": "sheetFromFile01", "max_video_seconds": 900}
            )
            (root / "workflow_config.json").write_text(
                json.dumps(
                    {
                        "drive_url": "https://drive.google.com/drive/folders/abc",
                        "profiles": PROFILES,
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"DRIVE_URL": "", "WORKFLOW_PROFILES_JSON": ""},
                clear=False,
            ):
                with self.assertRaises(RuntimeError):
                    load_workflow_config(root)
