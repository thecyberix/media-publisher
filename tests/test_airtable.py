from __future__ import annotations

import unittest
from unittest.mock import patch

from media_publisher.sources.airtable import (
    AirtableClient,
    AirtableRecord,
    record_to_publish_job,
)


class AirtableMappingTests(unittest.TestCase):
    def test_record_to_publish_job(self) -> None:
        job = record_to_publish_job(
            AirtableRecord(
                id="recABC",
                fields={
                    "Original Video Name": "Sample Title",
                    "Original Video": "https://example.com/video",
                    "Duration": 120,
                    "Type": "Short",
                    "Video Folder": "https://drive.google.com/folder/1",
                    "Translation resources": "https://ea.smartcat.com/editor/1",
                },
            )
        )
        self.assertEqual(job.title, "Sample Title")
        self.assertEqual(job.video_url, "https://example.com/video")
        self.assertEqual(job.airtable_record_id, "recABC")
        self.assertEqual(job.tags, ["Short"])
        self.assertEqual(job.metadata["Duration"], "120")
        self.assertEqual(
            job.metadata["Video Folder"],
            "https://drive.google.com/folder/1",
        )

    def test_record_to_publish_job_maps_canva_design(self) -> None:
        job = record_to_publish_job(
            AirtableRecord(
                id="recABC",
                fields={
                    "Original Video Name": "Sample Title",
                    "Canva Design": "https://www.canva.com/design/DAGabc123/view",
                },
            )
        )
        self.assertEqual(job.metadata["canva_design_id"], "https://www.canva.com/design/DAGabc123/view")
        self.assertEqual(job.metadata["Canva Design"], "https://www.canva.com/design/DAGabc123/view")


class AirtableClientTests(unittest.TestCase):
    def test_list_records_paginates(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        with patch.object(client, "_request") as request_mock:
            request_mock.side_effect = [
                {
                    "records": [
                        {"id": "rec1", "fields": {"Original Video Name": "A"}},
                    ],
                    "offset": "itr123",
                },
                {
                    "records": [
                        {"id": "rec2", "fields": {"Original Video Name": "B"}},
                    ],
                },
            ]
            records = client.list_records()

        self.assertEqual([record.id for record in records], ["rec1", "rec2"])
        self.assertEqual(request_mock.call_count, 2)
        self.assertEqual(request_mock.call_args_list[1].kwargs["query"]["offset"], "itr123")

    def test_update_record(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        with patch.object(client, "_request") as request_mock:
            request_mock.return_value = {
                "id": "rec1",
                "fields": {"Original Video Name": "Updated"},
            }
            record = client.update_record("rec1", {"Original Video Name": "Updated"})

        self.assertEqual(record.id, "rec1")
        self.assertEqual(record.fields["Original Video Name"], "Updated")
        request_mock.assert_called_once()
        self.assertEqual(request_mock.call_args.args[0], "PATCH")
        self.assertEqual(
            request_mock.call_args.kwargs["body"],
            {"fields": {"Original Video Name": "Updated"}},
        )

    def test_update_records_batches(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        updates = [(f"rec{i}", {"Original Video Name": f"Title {i}"}) for i in range(11)]

        with patch.object(client, "_request") as request_mock:
            request_mock.side_effect = [
                {"records": [{"id": f"rec{i}", "fields": {}} for i in range(10)]},
                {"records": [{"id": "rec10", "fields": {}}]},
            ]
            updated = client.update_records(updates)

        self.assertEqual(len(updated), 11)
        self.assertEqual(request_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
