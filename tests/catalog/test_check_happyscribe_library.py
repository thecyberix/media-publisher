from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_CATALOG = REPO_ROOT / "scripts" / "catalog"
sys.path.insert(0, str(SCRIPTS_CATALOG))
sys.path.insert(0, str(REPO_ROOT / "src"))

import check_happyscribe_library  # noqa: E402
from media_publisher.sources.happyscribe import (  # noqa: E402
    HappyScribeError,
    HappyScribeTranscription,
)


class CheckHappyScribeLibraryTests(unittest.TestCase):
    def test_build_alert_body_includes_count_and_titles(self) -> None:
        body = check_happyscribe_library.build_alert_body(
            library_url=check_happyscribe_library.DEFAULT_WATCH_LIBRARY_URL,
            transcriptions=[
                HappyScribeTranscription(
                    id="t1",
                    name="Sample Reel",
                    state="automatic_done",
                )
            ],
        )
        self.assertIn("Items: 1", body)
        self.assertIn("Sample Reel", body)
        self.assertIn(check_happyscribe_library.DEFAULT_WATCH_LIBRARY_URL, body)

    @patch.object(check_happyscribe_library, "check_library", return_value=[])
    def test_main_empty_library_exits_ok(self, _check: MagicMock) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "check_happyscribe_library.py",
                "--api-key",
                "hs-key",
            ],
        ):
            self.assertEqual(
                check_happyscribe_library.main(),
                check_happyscribe_library.EXIT_OK,
            )

    @patch.object(check_happyscribe_library, "check_library")
    def test_main_nonempty_writes_body_and_exits_2(self, check: MagicMock) -> None:
        check.return_value = [
            HappyScribeTranscription(
                id="t1",
                name="Leftover Video",
                state="automatic_done",
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            body_file = Path(tmp) / "alert.txt"
            with patch.object(
                sys,
                "argv",
                [
                    "check_happyscribe_library.py",
                    "--api-key",
                    "hs-key",
                    "--body-file",
                    str(body_file),
                ],
            ):
                code = check_happyscribe_library.main()

            self.assertEqual(code, check_happyscribe_library.EXIT_NONEMPTY)
            text = body_file.read_text(encoding="utf-8")
            self.assertIn("Leftover Video", text)
            self.assertIn("Items: 1", text)

    def test_main_skips_when_api_key_missing(self) -> None:
        with patch.dict(os.environ, {"HAPPYSCRIBE_API_KEY": ""}, clear=False):
            with patch.object(
                sys,
                "argv",
                [
                    "check_happyscribe_library.py",
                    "--skip-if-missing",
                ],
            ):
                self.assertEqual(
                    check_happyscribe_library.main(),
                    check_happyscribe_library.EXIT_OK,
                )

    @patch.object(
        check_happyscribe_library,
        "check_library",
        side_effect=HappyScribeError("boom"),
    )
    def test_main_api_error_exits_1(self, _check: MagicMock) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "check_happyscribe_library.py",
                "--api-key",
                "hs-key",
            ],
        ):
            self.assertEqual(
                check_happyscribe_library.main(),
                check_happyscribe_library.EXIT_ERROR,
            )


if __name__ == "__main__":
    unittest.main()
