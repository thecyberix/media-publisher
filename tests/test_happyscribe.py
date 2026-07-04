from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_publisher.models import PublishJob
from media_publisher.sources.happyscribe import (
    METADATA_TRANSCRIPTION_ID,
    HappyScribeClient,
    HappyScribeError,
    HappyScribeLibraryLocation,
    HappyScribeTranscription,
    burn_subtitles_into_video,
    enrich_job_from_happyscribe,
    parse_library_url,
    resolve_library_location,
    resolve_subtitled_transcription,
    subtitled_export_name,
    transcription_id_from_job,
    video_destination_path,
)


class HappyScribeLibraryTests(unittest.TestCase):
    def test_parse_library_url(self) -> None:
        location = parse_library_url(
            "https://www.happyscribe.com/v2/3310225/library/23100499"
        )
        self.assertEqual(location.organization_id, "3310225")
        self.assertEqual(location.folder_id, "23100499")

    def test_resolve_library_location_from_ids(self) -> None:
        location = resolve_library_location(
            organization_id="3310225",
            folder_id="23100499",
        )
        self.assertEqual(location.organization_id, "3310225")
        self.assertEqual(location.folder_id, "23100499")

    def test_resolve_library_location_prefers_url(self) -> None:
        location = resolve_library_location(
            library_url="https://www.happyscribe.com/v2/3310225/library/23100499",
            organization_id="999",
            folder_id="888",
        )
        self.assertEqual(location.organization_id, "3310225")
        self.assertEqual(location.folder_id, "23100499")

    def test_video_destination_path_strips_srt_suffix(self) -> None:
        path = video_destination_path(Path("downloads"), "Sample(bg).srt")
        self.assertEqual(path, Path("downloads/Sample(bg).mp4"))

    def test_subtitled_export_name(self) -> None:
        self.assertEqual(
            subtitled_export_name("Sample(bg)"),
            "Sample(bg).srt",
        )
        self.assertEqual(
            subtitled_export_name("Sample(bg).srt"),
            "Sample(bg).srt",
        )

    def test_resolve_subtitled_transcription(self) -> None:
        source = HappyScribeTranscription(
            id="src1",
            name="Sample(bg)",
            state="automatic_done",
        )
        exported = HappyScribeTranscription(
            id="exp1",
            name="Sample(bg).srt",
            state="automatic_done",
        )
        resolved = resolve_subtitled_transcription([source, exported], source)
        self.assertEqual(resolved.id, "exp1")

    def test_list_library_transcriptions_uses_folder_filter(self) -> None:
        client = HappyScribeClient("hs-test", organization_id="3310225")
        location = HappyScribeLibraryLocation(
            organization_id="3310225",
            folder_id="23100499",
        )
        with patch.object(client, "list_transcriptions") as list_mock:
            list_mock.return_value = []
            client.list_library_transcriptions(location)

        list_mock.assert_called_once_with(
            organization_id="3310225",
            folder_id="23100499",
        )


class HappyScribeClientTests(unittest.TestCase):
    def test_list_organizations(self) -> None:
        client = HappyScribeClient("hs-test")
        with patch.object(client, "_request") as request_mock:
            request_mock.return_value = {
                "organizations": [
                    {"id": 123, "name": "Acme Corp", "role": "owner"},
                ]
            }
            organizations = client.list_organizations()

        self.assertEqual(len(organizations), 1)
        self.assertEqual(organizations[0].id, "123")
        self.assertEqual(organizations[0].name, "Acme Corp")

    def test_resolve_organization_id_uses_configured_value(self) -> None:
        client = HappyScribeClient("hs-test", organization_id="456")
        self.assertEqual(client.resolve_organization_id(), "456")

    def test_iter_transcriptions_paginates(self) -> None:
        client = HappyScribeClient("hs-test", organization_id="123")
        with patch.object(client, "_request") as request_mock:
            request_mock.side_effect = [
                {
                    "results": [
                        {
                            "id": "tx1",
                            "name": "clip1.mp4",
                            "state": "automatic_done",
                        }
                    ],
                    "_links": {"next": {"url": "https://example.com?page=1"}},
                },
                {
                    "results": [
                        {
                            "id": "tx2",
                            "name": "clip2.mp4",
                            "state": "automatic_done",
                        }
                    ],
                },
            ]
            transcriptions = client.list_transcriptions()

        self.assertEqual([item.id for item in transcriptions], ["tx1", "tx2"])
        self.assertEqual(request_mock.call_count, 2)

    def test_get_video_download_url_prefers_video_url(self) -> None:
        client = HappyScribeClient("hs-test")
        with patch.object(client, "get_transcription") as get_mock:
            get_mock.return_value = type(
                "Transcription",
                (),
                {
                    "state": "automatic_done",
                    "video_url": "https://media.happyscribe.com/video.mp4",
                },
            )()
            url = client.get_video_download_url("tx1")

        self.assertEqual(url, "https://media.happyscribe.com/video.mp4")

    def test_get_video_download_url_falls_back_to_mp4_export(self) -> None:
        client = HappyScribeClient("hs-test")
        with patch.object(client, "get_transcription") as get_mock, patch.object(
            client, "create_export"
        ) as export_mock:
            get_mock.return_value = type(
                "Transcription",
                (),
                {"state": "automatic_done", "video_url": None},
            )()
            export_mock.return_value = type(
                "Export",
                (),
                {
                    "id": "exp1",
                    "state": "ready",
                    "download_link": "https://media.happyscribe.com/export.mp4",
                },
            )()
            url = client.get_video_download_url("tx1")

        self.assertEqual(url, "https://media.happyscribe.com/export.mp4")
        export_mock.assert_called_once_with("tx1", export_format="mp4")

    def test_download_video_writes_file(self) -> None:
        client = HappyScribeClient("hs-test")
        with patch.object(
            client, "get_video_download_url", return_value="https://example.com/video.mp4"
        ), patch.object(
            client, "download_file", return_value=Path("downloads/happyscribe/video.mp4")
        ) as download_mock:
            destination = Path("downloads/happyscribe/video.mp4")
            result = client.download_video("tx1", destination)

        self.assertEqual(result, destination)
        download_mock.assert_called_once_with("https://example.com/video.mp4", destination)


class HappyScribeEnrichmentTests(unittest.TestCase):
    def test_transcription_id_from_job(self) -> None:
        job = PublishJob(
            title="Sample",
            metadata={METADATA_TRANSCRIPTION_ID: "tx123"},
        )
        self.assertEqual(transcription_id_from_job(job), "tx123")

    def test_enrich_job_from_happyscribe_downloads_video(self) -> None:
        job = PublishJob(
            title="Sample",
            metadata={METADATA_TRANSCRIPTION_ID: "tx123"},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            download_dir = Path(tmpdir)
            destination = download_dir / "clip(bg)-subtitled.mp4"
            with patch("media_publisher.sources.happyscribe.HappyScribeClient") as client_cls, patch(
                "media_publisher.sources.happyscribe_web.export_video_for_transcription_name",
                return_value=destination,
            ) as export_mock:
                client = client_cls.return_value
                client.get_transcription.return_value = type(
                    "Transcription",
                    (),
                    {
                        "id": "tx123",
                        "name": "clip(bg)",
                        "state": "automatic_done",
                    },
                )()

                enriched = enrich_job_from_happyscribe(
                    job,
                    api_key="hs-test",
                    download_dir=download_dir,
                    browser_state_path=download_dir / "session.json",
                )

            self.assertEqual(enriched.video_path, str(destination))
            self.assertEqual(enriched.metadata[METADATA_TRANSCRIPTION_ID], "tx123")
            self.assertEqual(enriched.metadata["happyscribe_subtitled"], "True")
            self.assertEqual(enriched.metadata["happyscribe_export"], "web")
            export_mock.assert_called_once()

    def test_burn_subtitles_into_video_runs_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            video = root / "video.mp4"
            subtitles = root / "subs.srt"
            output = root / "output.mp4"
            video.write_bytes(b"video")
            subtitles.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")

            with patch(
                "media_publisher.sources.happyscribe.resolve_ffmpeg_path",
                return_value="ffmpeg",
            ), patch(
                "media_publisher.sources.happyscribe.subprocess.run",
                return_value=type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
            ) as run_mock, patch.object(Path, "unlink", autospec=True):
                output.write_bytes(b"done")
                burn_subtitles_into_video(video, subtitles, output)

            self.assertEqual(run_mock.call_args.args[0][0], "ffmpeg")

    def test_enrich_job_from_happyscribe_requires_transcription_id(self) -> None:
        job = PublishJob(title="Sample")
        with self.assertRaises(HappyScribeError):
            enrich_job_from_happyscribe(
                job,
                api_key="hs-test",
                download_dir=Path("downloads/happyscribe"),
            )


if __name__ == "__main__":
    unittest.main()
