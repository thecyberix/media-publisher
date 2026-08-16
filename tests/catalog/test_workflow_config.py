from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog_parser.workflow.config import load_catalog_id


class LoadCatalogIdTests(unittest.TestCase):
    def test_catalog_url_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {
                    "CATALOG_URL": (
                        "https://docs.google.com/spreadsheets/d/"
                        "1BGxTfnvs3zezyJVTSXroy9N0l7j5QHbzPzRj_TSjO-c/edit"
                    ),
                    "CATALOG_ID": "",
                },
                clear=False,
            ):
                self.assertEqual(
                    load_catalog_id(Path(tmpdir)),
                    "1BGxTfnvs3zezyJVTSXroy9N0l7j5QHbzPzRj_TSjO-c",
                )

    def test_file_catalog_id_when_env_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "workflow_config.json").write_text(
                '{"catalog_id": "sheetFromFile01"}',
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"CATALOG_URL": "", "CATALOG_ID": ""},
                clear=False,
            ):
                self.assertEqual(load_catalog_id(root), "sheetFromFile01")

    def test_missing_catalog_id_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {"CATALOG_URL": "", "CATALOG_ID": ""},
                clear=False,
            ):
                with self.assertRaises(RuntimeError):
                    load_catalog_id(Path(tmpdir))
