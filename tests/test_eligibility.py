from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from catalog_parser.eligibility import (
    explain_catalog_eligibility,
    is_catalog_eligible,
    is_not_duplicate_video_folder,
    is_not_in_airtable,
    needs_bulgarian_translation,
)


class EligibilityTests(unittest.TestCase):
    def test_needs_bulgarian_translation(self) -> None:
        self.assertTrue(
            needs_bulgarian_translation(
                {"pkgBgSrtLk": "https://ea.smartcat.com/open-editor/1"}
            )
        )
        self.assertFalse(needs_bulgarian_translation({"pkgBgSrtLk": None}))
        self.assertFalse(needs_bulgarian_translation({"pkgBgSrtLk": ""}))

    def test_is_not_in_airtable(self) -> None:
        self.assertTrue(
            is_not_in_airtable({"ctTitle": "New Title"}, {"existing title"})
        )
        self.assertFalse(
            is_not_in_airtable({"ctTitle": "Existing Title"}, {"existing title"})
        )

    def test_is_not_duplicate_video_folder(self) -> None:
        record = {
            "pkgLink": "https://drive.google.com/drive/folders/abc123XYZ",
        }
        self.assertTrue(is_not_duplicate_video_folder(record, set()))
        self.assertFalse(is_not_duplicate_video_folder(record, {"abc123XYZ"}))
        self.assertTrue(is_not_duplicate_video_folder({"pkgLink": None}, {"abc123XYZ"}))

    def test_is_catalog_eligible_requires_all_checks(self) -> None:
        drive = MagicMock()
        drive.files.return_value.list.return_value.execute.return_value = {
            "files": [
                {
                    "id": "stems-folder",
                    "name": "Stems",
                    "mimeType": "application/vnd.google-apps.folder",
                }
            ]
        }
        drive.files.return_value.get.return_value.execute.return_value = {
            "id": "stems-folder",
            "name": "Stems",
            "mimeType": "application/vnd.google-apps.folder",
        }

        base_record = {
            "ctTitle": "Eligible Reel",
            "pkgBgSrtLk": "https://ea.smartcat.com/open-editor/1",
            "pkgLink": "https://drive.google.com/drive/folders/pkg-folder",
        }

        with unittest.mock.patch(
            "catalog_parser.eligibility.record_has_mixable_media",
            return_value=True,
        ):
            self.assertTrue(
                is_catalog_eligible(
                    base_record,
                    set(),
                    drive_service=drive,
                )
            )
            self.assertFalse(
                is_catalog_eligible(
                    base_record,
                    set(),
                    existing_folder_ids={"pkg-folder"},
                    drive_service=drive,
                )
            )

        self.assertFalse(
            is_catalog_eligible(
                {**base_record, "pkgBgSrtLk": None},
                set(),
                drive_service=drive,
            )
        )
        self.assertFalse(
            is_catalog_eligible(
                base_record,
                {"eligible reel"},
                drive_service=drive,
            )
        )

    def test_explain_duplicate_video_folder(self) -> None:
        reasons = explain_catalog_eligibility(
            {
                "ctTitle": "FIFA title B",
                "pkgBgSrtLk": "https://ea.smartcat.com/open-editor/1",
                "pkgLink": "https://drive.google.com/drive/folders/shared-pkg",
            },
            set(),
            existing_folder_ids={"shared-pkg"},
            require_mixable_media=False,
        )
        self.assertIn("Already in Airtable (duplicate Video Folder)", reasons)


    def test_explain_catalog_eligibility_reports_smartcat_skip(self) -> None:
        reasons = explain_catalog_eligibility(
            {
                "ctTitle": "Sample",
                "pkgSmLk": "https://ea.smartcat.com/projects/x/files",
                "pkgBgSrtLkSkipReason": "Bulgarian subtitles already completed in Smartcat",
            },
            set(),
            drive_service=MagicMock(),
        )
        self.assertIn("Smartcat: Bulgarian subtitles already completed in Smartcat", reasons)


if __name__ == "__main__":
    unittest.main()
