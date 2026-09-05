from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from media_publisher.quotes_drive_sync import (
    GeneratedQuoteChange,
    current_and_next_months,
    format_generated_quotes_email,
    generated_quotes_notify_recipients,
    pair_fingerprint,
    state_key,
    substitute_quote_fingerprint,
    upload_published_substitute_quote,
)
from media_publisher.sources.google_drive import (
    DriveFile,
    DriveUploadResult,
    QuoteBackgroundImage,
    format_month_folder_name,
    local_file_md5,
)


class QuotesDriveSyncHelpersTests(unittest.TestCase):
    def test_current_and_next_months_rolls_year(self) -> None:
        self.assertEqual(
            current_and_next_months(date(2026, 12, 15)),
            [(2026, 12), (2027, 1)],
        )
        self.assertEqual(
            current_and_next_months(date(2026, 7, 18)),
            [(2026, 7), (2026, 8)],
        )

    def test_pair_fingerprint_changes_with_text_or_background(self) -> None:
        background = QuoteBackgroundImage(
            day=1,
            file_id="bg1",
            name="Jul-1-photo.jpg",
            variant="fbyt",
            md5_checksum="abc",
        )
        first = pair_fingerprint(background=background, text="Hello")
        second = pair_fingerprint(background=background, text="Hello world")
        third = pair_fingerprint(
            background=QuoteBackgroundImage(
                day=1,
                file_id="bg1",
                name="Jul-1-photo.jpg",
                variant="fbyt",
                md5_checksum="def",
            ),
            text="Hello",
        )
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, third)
        self.assertEqual(first, pair_fingerprint(background=background, text="Hello"))

    def test_state_key_and_month_folder_name(self) -> None:
        self.assertEqual(state_key(year=2026, month=7, day=18), "2026-07-18:fbyt")
        self.assertEqual(
            format_month_folder_name(
                "{month:02d} {month_abbr} {year}",
                year=2026,
                month=7,
            ),
            "07 Jul 2026",
        )

    def test_format_generated_quotes_email(self) -> None:
        changes = [
            GeneratedQuoteChange(
                action="added",
                year=2026,
                month=7,
                day=1,
                drive_name="2026-07-01.jpg",
                caption="First quote",
                fingerprint="a",
            ),
            GeneratedQuoteChange(
                action="updated",
                year=2026,
                month=7,
                day=2,
                drive_name="2026-07-02.jpg",
                caption="Second quote",
                fingerprint="b",
            ),
        ]
        subject, body = format_generated_quotes_email(changes)
        self.assertIn("1 added, 1 updated", subject)
        self.assertIn("2026-07-01.jpg", body)
        self.assertIn("2026-07-02.jpg", body)
        self.assertIn("First quote", body)
        self.assertIn("drive.google.com/drive/folders/", body)

        subject, body = format_generated_quotes_email(
            [
                GeneratedQuoteChange(
                    action="added",
                    year=2026,
                    month=9,
                    day=5,
                    drive_name="2026-09-05.jpg",
                    caption="Edited text",
                    fingerprint="c",
                    source="edited",
                )
            ]
        )
        self.assertIn("2026-09-05.jpg", body)
        self.assertIn("edited substitute", body)

    def test_generated_quotes_notify_recipients_from_env_list(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(
            os.environ,
            {
                "GENERATED_QUOTES_NOTIFY_EMAIL": (
                    "quotes@example.com, ops@example.com"
                ),
                "NOTIFY_EMAIL": "should-not-appear@example.com",
            },
            clear=False,
        ):
            recipients = generated_quotes_notify_recipients()
        self.assertEqual(
            recipients,
            ["quotes@example.com", "ops@example.com"],
        )

        with patch.dict(os.environ, {"GENERATED_QUOTES_NOTIFY_EMAIL": ""}, clear=False):
            self.assertEqual(generated_quotes_notify_recipients(), [])

    def test_local_file_md5(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.jpg"
            path.write_bytes(b"quote-bytes")
            self.assertEqual(local_file_md5(path), local_file_md5(path))

    def test_upload_or_update_skips_matching_md5(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-07-01.jpg"
            path.write_bytes(b"same-bytes")
            digest = local_file_md5(path)
            client = MagicMock()
            client.find_child_by_name.return_value = DriveFile(
                id="file1",
                name="2026-07-01.jpg",
                mime_type="image/jpeg",
                md5_checksum=digest,
            )
            # Bind real method.
            from media_publisher.sources.google_drive import GoogleDriveClient

            result = GoogleDriveClient.upload_or_update_file(
                client,
                "parent",
                path,
                name="2026-07-01.jpg",
            )
            self.assertEqual(result.action, "unchanged")
            client._drive.files.assert_not_called()

    def test_upload_published_substitute_quote_writes_drive_and_state(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "2026-09-05.jpg"
            image.write_bytes(b"quote-bytes")
            drive = MagicMock()
            drive.ensure_folder.return_value = DriveFile(
                id="folder-sep",
                name="09 Sep 2026",
                mime_type="application/vnd.google-apps.folder",
            )
            drive.upload_or_update_file.return_value = DriveUploadResult(
                action="added",
                file=DriveFile(
                    id="file-1",
                    name="2026-09-05.jpg",
                    mime_type="image/jpeg",
                ),
            )
            with patch(
                "media_publisher.quotes_drive_sync.resolve_quotes_folder_id",
                return_value="quotes-root",
            ):
                change, warnings = upload_published_substitute_quote(
                    drive_client=drive,
                    project_root=root,
                    image_path=image,
                    year=2026,
                    month=9,
                    day=5,
                    caption="Edited substitute",
                    source="edited",
                )
            self.assertEqual(warnings, [])
            self.assertIsNotNone(change)
            self.assertEqual(change.action, "added")
            self.assertEqual(change.source, "edited")
            self.assertEqual(change.drive_name, "2026-09-05.jpg")
            drive.upload_or_update_file.assert_called_once()
            state_path = root / "downloads/quotes/generated-sync-state.json"
            self.assertTrue(state_path.is_file())
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))[
                    state_key(year=2026, month=9, day=5)
                ]["fingerprint"],
                substitute_quote_fingerprint(
                    image_path=image, caption="Edited substitute"
                ),
            )


if __name__ == "__main__":
    unittest.main()
