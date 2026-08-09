from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from catalog_parser.__main__ import build_eligible_catalog_records
from catalog_parser.airtable import make_title_identity_key


class EligibleCatalogEarlySkipTests(unittest.TestCase):
    def test_duplicate_title_same_type_skips_smartcat_drive_and_ai(self) -> None:
        candidates = [
            {
                "ctTitle": "Farm vs Supermarket: Ian Somerhalder & Sadhguru Guess",
                "ctDuration": 400,
                "pkgLink": "https://drive.google.com/drive/folders/new-pkg",
                "ctLink": "https://youtu.be/brandnewvideoid",
                "pkgSmLk": "https://ea.smartcat.com/projects/x/files",
            },
        ]
        existing_titles = {
            make_title_identity_key(
                "farm vs supermarket: ian somerhalder & sadhguru guess",
                "Video",
            ),
        }

        with (
            patch(
                "catalog_parser.__main__.enrich_single_record_with_smartcat_api",
                side_effect=AssertionError("Smartcat should not run for duplicate"),
            ) as smartcat_api,
            patch(
                "catalog_parser.__main__.enrich_records_with_yt_titles",
                side_effect=AssertionError("Drive ytTitle should not run for duplicate"),
            ),
            patch(
                "catalog_parser.__main__.enrich_records_with_original_video_thumbnails",
                side_effect=AssertionError("Thumbnails should not run for duplicate"),
            ),
        ):
            eligible, scanned = build_eligible_catalog_records(
                candidates,
                target_count=1,
                existing_titles=set(existing_titles),
                existing_folder_ids=set(),
                existing_original_video_names=set(),
                existing_original_video_keys=set(),
                smartcat_enabled=True,
                smartcat_api=True,
                smartcat_language="Bulgarian",
                web_client=None,
                drive_docs_enabled=True,
                drive_service=MagicMock(),
                docs_service=MagicMock(),
                canva_client=None,
                require_mixable_media=True,
                thumbnail_staging_dir=Path("."),
                video_type="Video",
            )

        self.assertEqual(scanned, 1)
        self.assertEqual(eligible, [])
        smartcat_api.assert_not_called()

    def test_same_title_different_type_is_not_early_skipped(self) -> None:
        candidates = [
            {
                "ctTitle": "Farm vs Supermarket",
                "ctDuration": 400,
                "pkgLink": "https://drive.google.com/drive/folders/eligible-pkg",
                "ctLink": "https://youtu.be/eligiblevid001",
                "pkgSmLk": "https://ea.smartcat.com/projects/y/files",
            },
        ]

        def enrich_smartcat(record, *, smartcat_language):
            out = dict(record)
            out["pkgBgSrtLk"] = "https://ea.smartcat.com/open-editor/1"
            return out

        def enrich_yt(records, drive, docs):
            out = dict(records[0])
            out["ytTitle"] = "Farm vs Supermarket Unique YT"
            return [out]

        def enrich_thumbs(records, drive, docs, **kwargs):
            return [dict(records[0])]

        with (
            patch(
                "catalog_parser.__main__.enrich_single_record_with_smartcat_api",
                side_effect=enrich_smartcat,
            ) as smartcat_api,
            patch(
                "catalog_parser.__main__.enrich_records_with_yt_titles",
                side_effect=enrich_yt,
            ),
            patch(
                "catalog_parser.__main__.enrich_records_with_original_video_thumbnails",
                side_effect=enrich_thumbs,
            ),
            patch(
                "catalog_parser.__main__.record_has_mixable_media",
                return_value=True,
            ),
            patch(
                "catalog_parser.eligibility.record_has_mixable_media",
                return_value=True,
            ),
            patch(
                "catalog_parser.eligibility.check_mixable_media",
            ) as mix_check,
            patch(
                "catalog_parser.translation.prefill.ai_prefill_enabled",
                return_value=False,
            ),
        ):
            mix_check.return_value = MagicMock(ok=True, error=None)
            eligible, scanned = build_eligible_catalog_records(
                candidates,
                target_count=1,
                existing_titles={
                    make_title_identity_key("farm vs supermarket", "Reel"),
                },
                existing_folder_ids=set(),
                existing_original_video_names=set(),
                existing_original_video_keys=set(),
                smartcat_enabled=True,
                smartcat_api=True,
                smartcat_language="Bulgarian",
                web_client=None,
                drive_docs_enabled=True,
                drive_service=MagicMock(),
                docs_service=MagicMock(),
                canva_client=None,
                require_mixable_media=True,
                thumbnail_staging_dir=Path("."),
                video_type="Video",
            )

        self.assertEqual(scanned, 1)
        self.assertEqual(len(eligible), 1)
        self.assertEqual(
            eligible[0].get("pkgBgSrtLk"),
            "https://ea.smartcat.com/open-editor/1",
        )
        smartcat_api.assert_called_once()

    def test_dry_run_skips_ai_prefill_writes(self) -> None:
        candidates = [
            {
                "ctTitle": "Dry Run Prefill Skip",
                "ctDuration": 400,
                "pkgLink": "https://drive.google.com/drive/folders/dry-run-pkg",
                "ctLink": "https://youtu.be/dryrunvid00001",
                "pkgSmLk": "https://ea.smartcat.com/projects/z/files",
            },
        ]

        def enrich_smartcat(record, *, smartcat_language):
            out = dict(record)
            out["pkgBgSrtLk"] = "https://ea.smartcat.com/open-editor/dry"
            return out

        with (
            patch(
                "catalog_parser.__main__.enrich_single_record_with_smartcat_api",
                side_effect=enrich_smartcat,
            ),
            patch(
                "catalog_parser.__main__.enrich_records_with_yt_titles",
                side_effect=lambda records, drive, docs: [dict(records[0])],
            ),
            patch(
                "catalog_parser.__main__.enrich_records_with_original_video_thumbnails",
                side_effect=lambda records, drive, docs, **kwargs: [dict(records[0])],
            ),
            patch(
                "catalog_parser.__main__.record_has_mixable_media",
                return_value=True,
            ),
            patch(
                "catalog_parser.eligibility.record_has_mixable_media",
                return_value=True,
            ),
            patch(
                "catalog_parser.translation.prefill.ai_prefill_enabled",
                return_value=True,
            ),
            patch(
                "catalog_parser.translation.prefill.prefill_record_if_needed",
                side_effect=AssertionError("AI prefill must not run in dry-run"),
            ) as prefill,
        ):
            eligible, scanned = build_eligible_catalog_records(
                candidates,
                target_count=1,
                existing_titles=set(),
                existing_folder_ids=set(),
                existing_original_video_names=set(),
                existing_original_video_keys=set(),
                smartcat_enabled=True,
                smartcat_api=True,
                smartcat_language="Bulgarian",
                web_client=None,
                drive_docs_enabled=True,
                drive_service=MagicMock(),
                docs_service=MagicMock(),
                canva_client=None,
                require_mixable_media=True,
                thumbnail_staging_dir=Path("."),
                video_type="Video",
                dry_run=True,
            )

        self.assertEqual(scanned, 1)
        self.assertEqual(len(eligible), 1)
        prefill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
