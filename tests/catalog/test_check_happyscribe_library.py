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
    HappyScribeLibraryLocation,
    HappyScribeTranscription,
)

SHORT = HappyScribeLibraryLocation(
    organization_id="8104266",
    folder_id="53816432",
    folder_name="Short videos",
)
LONG = HappyScribeLibraryLocation(
    organization_id="8104266",
    folder_id="55859422",
    folder_name="Long videos",
)


class CheckHappyScribeLibraryTests(unittest.TestCase):
    def test_build_alert_body_includes_count_and_titles(self) -> None:
        body = check_happyscribe_library.build_alert_body(
            [
                (
                    SHORT,
                    [
                        HappyScribeTranscription(
                            id="t1",
                            name="Sample Reel",
                            state="automatic_done",
                        )
                    ],
                )
            ]
        )
        self.assertIn("Items: 1", body)
        self.assertIn("Sample Reel", body)
        self.assertIn("Short videos", body)
        self.assertIn("53816432", body)
        self.assertIn("folder is not empty", body)

    def test_build_alert_body_lists_each_nonempty_folder(self) -> None:
        body = check_happyscribe_library.build_alert_body(
            [
                (
                    SHORT,
                    [
                        HappyScribeTranscription(
                            id="t1",
                            name="Reel One",
                            state="automatic_done",
                        )
                    ],
                ),
                (
                    LONG,
                    [
                        HappyScribeTranscription(
                            id="t2",
                            name="Video Two",
                            state="automatic_done",
                        )
                    ],
                ),
            ]
        )
        self.assertIn("folders are not empty", body)
        self.assertIn("Short videos", body)
        self.assertIn("Long videos", body)
        self.assertIn("Reel One", body)
        self.assertIn("Video Two", body)

    @patch.object(check_happyscribe_library, "check_library", return_value=[])
    @patch.object(
        check_happyscribe_library,
        "resolve_watch_folders",
        return_value=[SHORT, LONG],
    )
    def test_main_empty_library_exits_ok(
        self,
        _resolve: MagicMock,
        check: MagicMock,
    ) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "check_happyscribe_library.py",
                "--api-key",
                "hs-key",
                "--review-url",
                "https://www.happyscribe.com/v2/8104266/library/workspace",
            ],
        ):
            self.assertEqual(
                check_happyscribe_library.main(),
                check_happyscribe_library.EXIT_OK,
            )
        self.assertEqual(check.call_count, 2)

    @patch.object(check_happyscribe_library, "check_library")
    @patch.object(
        check_happyscribe_library,
        "resolve_watch_folders",
        return_value=[SHORT, LONG],
    )
    def test_main_nonempty_writes_body_and_exits_2(
        self,
        _resolve: MagicMock,
        check: MagicMock,
    ) -> None:
        check.side_effect = [
            [
                HappyScribeTranscription(
                    id="t1",
                    name="Leftover Video",
                    state="automatic_done",
                )
            ],
            [],
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
                    "--review-url",
                    "https://www.happyscribe.com/v2/8104266/library/workspace",
                    "--body-file",
                    str(body_file),
                ],
            ):
                code = check_happyscribe_library.main()

            self.assertEqual(code, check_happyscribe_library.EXIT_NONEMPTY)
            text = body_file.read_text(encoding="utf-8")
            self.assertIn("Leftover Video", text)
            self.assertIn("Items: 1", text)
            self.assertIn("Short videos", text)
            self.assertNotIn("Long videos", text)

    def test_main_skips_when_api_key_missing(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HAPPYSCRIBE_API_KEY": "",
                "HAPPYSCRIBE_REVIEW_URL": (
                    "https://www.happyscribe.com/v2/8104266/library/workspace"
                ),
            },
            clear=False,
        ):
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

    def test_main_skips_when_review_url_missing(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "check_happyscribe_library.py",
                "--api-key",
                "hs-key",
            ],
        ):
            with patch.dict(os.environ, {"HAPPYSCRIBE_REVIEW_URL": ""}, clear=False):
                self.assertEqual(
                    check_happyscribe_library.main(),
                    check_happyscribe_library.EXIT_OK,
                )

    @patch.object(
        check_happyscribe_library,
        "resolve_watch_folders",
        side_effect=HappyScribeError("boom"),
    )
    def test_main_api_error_exits_1(self, _resolve: MagicMock) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "check_happyscribe_library.py",
                "--api-key",
                "hs-key",
                "--review-url",
                "https://www.happyscribe.com/v2/8104266/library/workspace",
            ],
        ):
            self.assertEqual(
                check_happyscribe_library.main(),
                check_happyscribe_library.EXIT_ERROR,
            )


if __name__ == "__main__":
    unittest.main()
