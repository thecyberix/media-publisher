from __future__ import annotations

import unittest

from catalog_parser.translation.metadata_corpus import (
    MetadataCandidate,
    build_pairs_for_candidate,
    metadata_candidate_from_record,
)


class MetadataCorpusTests(unittest.TestCase):
    def test_metadata_candidate_from_record(self) -> None:
        candidate = metadata_candidate_from_record(
            {
                "id": "rec1",
                "fields": {
                    "Title": "Hello World",
                    "Video name translated": "Здравей свят",
                    "Video description translated": "Описание",
                    "Video Folder": "https://drive.google.com/drive/folders/abc123",
                    "Status": "5. Synchronization done",
                    "Type": "Video",
                },
            },
            source="2026",
            base_id="app1",
            table_name="Table",
        )
        assert candidate is not None
        self.assertEqual(candidate.bg_title, "Здравей свят")
        self.assertEqual(candidate.video_folder, "https://drive.google.com/drive/folders/abc123")

    def test_build_pairs_uses_airtable_en_without_drive(self) -> None:
        candidate = MetadataCandidate(
            record_id="rec1",
            title="Hello",
            status="done",
            record_type="Video",
            source="2026",
            base_id="app1",
            table_name="T",
            video_folder=None,
            bg_title="Здравей",
            bg_description="Описание на видеото",
            en_title_airtable="Hello",
            en_description_airtable="A description",
        )
        pairs, notes = build_pairs_for_candidate(candidate, drive_cache=None)
        self.assertEqual(notes, [])
        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0].kind, "title")
        self.assertEqual(pairs[0].en_origin, "airtable")
        self.assertEqual(pairs[1].kind, "description")


if __name__ == "__main__":
    unittest.main()
