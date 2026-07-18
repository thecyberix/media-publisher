from __future__ import annotations

import unittest
import urllib.parse

from catalog_parser.airtable import (
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TRANSLATION_RESOURCES,
    STATUS_EDITING_DONE,
    STATUS_SYNC_DONE,
    STATUS_TRANSLATION_DONE,
)
from catalog_parser.translation.corpus import (
    CorpusCandidate,
    build_corpus_query_filter,
    build_corpus_selection,
    dedupe_candidates,
    resolve_record_title,
    select_holdout_titles,
    split_current_table_holdout,
)


def _candidate(title: str, *, source: str = "2026") -> CorpusCandidate:
    search = urllib.parse.quote_plus(title)
    return CorpusCandidate(
        record_id=f"rec-{title}",
        title=title,
        status=STATUS_SYNC_DONE,
        record_type="Video",
        smartcat_link=(
            "https://ea.smartcat.com/projects/d1b6348b-541f-473a-9583-2a03d5315fef/"
            f"files?folderMode=true&search={search}"
        ),
        translated_title=f"{title} BG",
        source=source,
        base_id="app-main",
        table_name="Translator's Paradise",
    )


class CorpusSelectionTests(unittest.TestCase):
    def test_query_excludes_translation_done(self) -> None:
        formula = build_corpus_query_filter()
        self.assertIn(STATUS_EDITING_DONE, formula)
        self.assertIn(STATUS_SYNC_DONE, formula)
        self.assertIn('FIND("Done & Published"', formula)
        self.assertNotIn(STATUS_TRANSLATION_DONE, formula)

    def test_resolve_record_title_prefers_configured_fields(self) -> None:
        title = resolve_record_title(
            {"Original Video Name": "Archive Title"},
            title_fields=("Original Video Name", "Title"),
        )
        self.assertEqual(title, "Archive Title")

    def test_holdout_is_deterministic(self) -> None:
        candidates = [_candidate(f"Video {index}") for index in range(10)]
        first = select_holdout_titles(candidates, holdout_count=3, seed="seed-a")
        second = select_holdout_titles(candidates, holdout_count=3, seed="seed-a")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 3)

    def test_split_current_table_holdout(self) -> None:
        candidates = [_candidate(f"Video {index}") for index in range(5)]
        export_rows, holdout_rows = split_current_table_holdout(
            candidates,
            holdout_count=2,
            seed="seed-a",
        )
        self.assertEqual(len(export_rows) + len(holdout_rows), 5)
        self.assertEqual(len(holdout_rows), 2)
        holdout_titles = {row.title for row in holdout_rows}
        self.assertTrue(all(row.title not in holdout_titles for row in export_rows))

    def test_dedupe_prefers_archive_source(self) -> None:
        shared = "Shared Title"
        archive = _candidate(shared, source="2024 archive")
        current = _candidate(shared, source="2026")
        deduped = dedupe_candidates([current, archive])
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0].source, "2024 archive")

    def test_dedupe_prefers_video_over_reel_same_title(self) -> None:
        title = "How Shiva Established Kedarnath | Sadhguru"
        reel = _candidate(title, source="2026")
        reel = CorpusCandidate(**{**reel.__dict__, "record_type": "Reel", "record_id": "rec-reel"})
        video = CorpusCandidate(
            **{**_candidate(title, source="2026").__dict__, "record_type": "Video", "record_id": "rec-video"}
        )
        deduped = dedupe_candidates([reel, video])
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0].record_type, "Video")
        self.assertEqual(deduped[0].record_id, "rec-video")

    def test_dedupe_prefers_video_even_when_archive_is_reel(self) -> None:
        title = "Shared Clip Title"
        archive_reel = CorpusCandidate(
            **{
                **_candidate(title, source="2025 archive").__dict__,
                "record_type": "Reel",
                "record_id": "rec-archive-reel",
            }
        )
        current_video = CorpusCandidate(
            **{
                **_candidate(title, source="2026").__dict__,
                "record_type": "Video",
                "record_id": "rec-current-video",
            }
        )
        deduped = dedupe_candidates([archive_reel, current_video])
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0].record_id, "rec-current-video")

    def test_build_corpus_selection_splits_current_table(self) -> None:
        class FakeAirtable:
            base_id = "app-main"
            table_name = "Translator's Paradise"

            def list_records(self, *, filter_formula=None, base_id=None, table_name=None):
                if base_id not in (None, self.base_id):
                    return []
                return [
                    {
                        "id": "rec-1",
                        "fields": {
                            FIELD_TITLE: "Current One",
                            FIELD_STATUS: STATUS_SYNC_DONE,
                            FIELD_TRANSLATION_RESOURCES: _candidate("Current One").smartcat_link,
                        },
                    },
                    {
                        "id": "rec-2",
                        "fields": {
                            FIELD_TITLE: "Current Two",
                            FIELD_STATUS: STATUS_SYNC_DONE,
                            FIELD_TRANSLATION_RESOURCES: _candidate("Current Two").smartcat_link,
                        },
                    },
                ]

        selection = build_corpus_selection(
            FakeAirtable(),  # type: ignore[arg-type]
            current_year="2026",
            holdout_count=1,
            include_archives=False,
            include_current=True,
        )
        self.assertEqual(len(selection.holdout_candidates), 1)
        self.assertEqual(len(selection.export_candidates), 1)


if __name__ == "__main__":
    unittest.main()
