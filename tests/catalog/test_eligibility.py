from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from catalog_parser.airtable import make_title_identity_key
from catalog_parser.eligibility import (
    airtable_identity_collision_reasons,
    explain_catalog_eligibility,
    is_catalog_eligible,
    is_not_duplicate_original_video,
    is_not_duplicate_video_folder,
    is_not_duplicate_yt_title,
    is_not_in_airtable,
    needs_bulgarian_translation,
    smartcat_ready_for_ingest,
    smartcat_translation_completed,
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
        existing = {make_title_identity_key("existing title", "Reel")}
        self.assertTrue(
            is_not_in_airtable(
                {"ctTitle": "New Title", "ctDuration": 60},
                existing,
                video_type="Reel",
            )
        )
        self.assertFalse(
            is_not_in_airtable(
                {"ctTitle": "Existing Title", "ctDuration": 60},
                existing,
                video_type="Reel",
            )
        )
        # Same title, different type is allowed.
        self.assertTrue(
            is_not_in_airtable(
                {"ctTitle": "Existing Title", "ctDuration": 300},
                existing,
                video_type="Video",
            )
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
                {make_title_identity_key("eligible reel", "Reel")},
                drive_service=drive,
                video_type="Reel",
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

    def test_is_not_duplicate_yt_title(self) -> None:
        record = {"ytTitle": "You're Misunderstanding Karma Completely"}
        self.assertTrue(is_not_duplicate_yt_title(record, set()))
        self.assertFalse(
            is_not_duplicate_yt_title(
                record,
                {"you're misunderstanding karma completely"},
            )
        )
        # curly vs straight apostrophe
        self.assertFalse(
            is_not_duplicate_yt_title(
                {"ytTitle": "You’re Misunderstanding Karma Completely"},
                {"you're misunderstanding karma completely"},
            )
        )
        self.assertTrue(is_not_duplicate_yt_title({}, {"some title"}))

    def test_is_catalog_eligible_rejects_duplicate_yt_title(self) -> None:
        record = {
            "ctTitle": "It Can Bring You Wisdom Or Bind You And Poison Your Life",
            "ytTitle": "You're Misunderstanding Karma Completely",
            "pkgBgSrtLk": "https://ea.smartcat.com/open-editor/1",
            "pkgLink": "https://drive.google.com/drive/folders/other-pkg",
        }
        with unittest.mock.patch(
            "catalog_parser.eligibility.record_has_mixable_media",
            return_value=True,
        ):
            self.assertFalse(
                is_catalog_eligible(
                    record,
                    set(),
                    existing_folder_ids=set(),
                    existing_original_video_names={
                        "you're misunderstanding karma completely"
                    },
                    drive_service=MagicMock(),
                )
            )

    def test_explain_duplicate_yt_title(self) -> None:
        reasons = explain_catalog_eligibility(
            {
                "ctTitle": "Poison title",
                "ytTitle": "You're Misunderstanding Karma Completely",
                "pkgBgSrtLk": "https://ea.smartcat.com/open-editor/1",
                "pkgLink": "https://drive.google.com/drive/folders/other-pkg",
            },
            set(),
            existing_folder_ids=set(),
            existing_original_video_names={
                "you're misunderstanding karma completely"
            },
            require_mixable_media=False,
        )
        self.assertIn(
            "Already in Airtable (duplicate Original Video Name)",
            reasons,
        )

    def test_is_not_duplicate_original_video(self) -> None:
        record = {"ctLink": "https://www.instagram.com/p/DXwoY7rzBzu"}
        self.assertTrue(is_not_duplicate_original_video(record, set()))
        self.assertFalse(
            is_not_duplicate_original_video(record, {"ig:DXwoY7rzBzu"})
        )
        self.assertFalse(
            is_not_duplicate_original_video(
                {"ctLink": "https://www.instagram.com/reel/DXwoY7rzBzu/"},
                {"ig:DXwoY7rzBzu"},
            )
        )
        self.assertFalse(
            is_not_duplicate_original_video(
                {"ctLink": "https://youtu.be/8xl1cANaov0"},
                {"yt:8xl1cANaov0"},
            )
        )
        self.assertTrue(is_not_duplicate_original_video({}, {"ig:abc"}))

    def test_explain_duplicate_original_video(self) -> None:
        reasons = explain_catalog_eligibility(
            {
                "ctTitle": "Prayer Means You Are Trying To Tell God What To Do",
                "ctLink": "https://www.instagram.com/p/DXwoY7rzBzu",
                "pkgBgSrtLk": "https://ea.smartcat.com/open-editor/1",
                "pkgLink": "https://drive.google.com/drive/folders/other-pkg",
            },
            set(),
            existing_folder_ids=set(),
            existing_original_video_keys={"ig:DXwoY7rzBzu"},
            require_mixable_media=False,
        )
        self.assertIn("Already in Airtable (duplicate Original Video)", reasons)

    def test_explain_catalog_eligibility_reports_smartcat_skip(self) -> None:
        reasons = explain_catalog_eligibility(
            {
                "ctTitle": "Sample",
                "pkgSmLk": "https://ea.smartcat.com/projects/x/files",
                "pkgBgSrtLkSkipReason": "No Bulgarian target language on document",
            },
            set(),
            drive_service=MagicMock(),
            require_mixable_media=False,
        )
        self.assertIn("Smartcat: No Bulgarian target language on document", reasons)

    def test_smartcat_completed_is_ready_for_ingest(self) -> None:
        record = {
            "pkgBgSrtLkSkipReason": "Bulgarian subtitles already completed in Smartcat",
        }
        self.assertTrue(smartcat_translation_completed(record))
        self.assertTrue(smartcat_ready_for_ingest(record))
        self.assertTrue(
            is_catalog_eligible(
                {
                    "ctTitle": "Completed Video",
                    "ctDuration": 400,
                    **record,
                    "pkgLink": "https://drive.google.com/drive/folders/pkg",
                },
                set(),
                require_mixable_media=False,
                video_type="Video",
            )
        )

    def test_airtable_identity_collision_reasons_title_and_folder(self) -> None:
        reasons = airtable_identity_collision_reasons(
            {
                "ctTitle": "Farm vs Supermarket",
                "ctDuration": 400,
                "pkgLink": "https://drive.google.com/drive/folders/pkg1",
                "ctLink": "https://youtu.be/abc12345678",
            },
            {make_title_identity_key("farm vs supermarket", "Video")},
            existing_folder_ids={"pkg1"},
            existing_original_video_keys={"yt:abc12345678"},
            video_type="Video",
        )
        self.assertIn("Already in Airtable (duplicate title for this Type)", reasons)
        self.assertIn("Already in Airtable (duplicate Video Folder)", reasons)
        self.assertIn("Already in Airtable (duplicate Original Video)", reasons)

    def test_airtable_identity_allows_same_title_different_type(self) -> None:
        reasons = airtable_identity_collision_reasons(
            {
                "ctTitle": "Farm vs Supermarket",
                "ctDuration": 400,
                "pkgLink": "https://drive.google.com/drive/folders/new-pkg",
            },
            {make_title_identity_key("farm vs supermarket", "Reel")},
            existing_folder_ids=set(),
            video_type="Video",
        )
        self.assertEqual(reasons, [])


if __name__ == "__main__":
    unittest.main()
