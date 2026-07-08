from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_publisher.models import PublishJob
from media_publisher.sources.canva import (
    CANVA_LONG_VIDEO_THUMBNAILS_URL,
    CANVA_SHORT_VIDEO_THUMBNAILS_URL,
    DEFAULT_SCOPES,
    CanvaClient,
    CanvaDesignPageInfo,
    CanvaDesignSummary,
    CanvaError,
    CanvaThumbnailTarget,
    CanvaToken,
    catalog_video_name_from_job,
    find_cached_thumbnail_path,
    ensure_catalog_thumbnail_from_canva,
    parse_canva_resource,
    resolve_thumbnail_target,
    save_token,
    thumbnail_catalog_url_for_format,
    thumbnail_destination_path,
    titles_match,
)


class CanvaThumbnailHelperTests(unittest.TestCase):
    def test_thumbnail_catalog_url_for_format(self) -> None:
        self.assertEqual(
            thumbnail_catalog_url_for_format("post"),
            CANVA_LONG_VIDEO_THUMBNAILS_URL,
        )
        self.assertEqual(
            thumbnail_catalog_url_for_format("short_form"),
            CANVA_SHORT_VIDEO_THUMBNAILS_URL,
        )

    def test_catalog_video_name_from_job_uses_original_video_name(self) -> None:
        job = PublishJob(
            title="Преведено заглавие",
            metadata={"Original Video Name": "Original English Title"},
        )
        self.assertEqual(catalog_video_name_from_job(job), "Original English Title")

    def test_titles_match_is_case_insensitive(self) -> None:
        self.assertTrue(titles_match("Launch Video", "launch video"))

    def test_thumbnail_destination_path_uses_safe_filename(self) -> None:
        path = thumbnail_destination_path(Path("downloads/canva"), 'Title: "Demo"')
        self.assertEqual(path, Path("downloads/canva/Title_ _Demo_.png"))

    def test_find_cached_thumbnail_path_accepts_trailing_comma_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            download_dir = Path(tmpdir)
            cached = download_dir / (
                "Responding To A Question From Bibek Debroy Sadhguru Explains Why "
                "Shiva Has A Cobra Around His Neck And The Significance Of The "
                "Sacred Serpent In The,.png"
            )
            cached.write_bytes(b"png")
            found = find_cached_thumbnail_path(
                download_dir,
                (
                    "Responding To A Question From Bibek Debroy Sadhguru Explains Why "
                    "Shiva Has A Cobra Around His Neck And The Significance Of The "
                    "Sacred Serpent In The"
                ),
            )
        self.assertEqual(found, cached)

    def test_find_cached_thumbnail_path_accepts_youtube_thumb_jpg(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            download_dir = Path(tmpdir)
            cached = download_dir / "Launch video.youtube-thumb.jpg"
            cached.write_bytes(b"jpg")
            found = find_cached_thumbnail_path(download_dir, "Launch video")
        self.assertEqual(found, cached)

    def test_parse_canva_resource_detects_folder(self) -> None:
        with patch(
            "media_publisher.sources.canva.resolve_canva_url",
            return_value="https://www.canva.com/folder/FAF2lZtloor",
        ):
            resource_type, resource_id = parse_canva_resource("https://canva.link/example")
        self.assertEqual(resource_type, "folder")
        self.assertEqual(resource_id, "FAF2lZtloor")

    def test_parse_canva_resource_detects_long_thumbnail_folder_shortlink(self) -> None:
        with patch(
            "media_publisher.sources.canva.resolve_canva_url",
            return_value="https://www.canva.com/folder/FAHOmUvMRtk",
        ):
            resource_type, resource_id = parse_canva_resource(
                CANVA_LONG_VIDEO_THUMBNAILS_URL
            )
        self.assertEqual(resource_type, "folder")
        self.assertEqual(resource_id, "FAHOmUvMRtk")


class CanvaThumbnailClientTests(unittest.TestCase):
    def _client(self, tmpdir: str) -> CanvaClient:
        token_path = Path(tmpdir) / "token.json"
        save_token(
            token_path,
            CanvaToken(
                access_token="access",
                refresh_token="refresh",
                expires_at=9999999999.0,
            ),
        )
        return CanvaClient("client-id", "client-secret", token_path)

    def test_resolve_thumbnail_target_matches_page_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            client = self._client(tmpdir)
            with (
                patch.object(
                    client,
                    "list_design_pages_info",
                    return_value=[
                        CanvaDesignPageInfo(page_number=1, title="Other"),
                        CanvaDesignPageInfo(page_number=2, title="Launch video"),
                    ],
                ),
                patch(
                    "media_publisher.sources.canva.parse_canva_resource",
                    return_value=("design", "DAG123"),
                ),
            ):
                target = client.resolve_thumbnail_target(
                    CANVA_LONG_VIDEO_THUMBNAILS_URL,
                    "Launch video",
                )
        self.assertEqual(target, CanvaThumbnailTarget(design_id="DAG123", page_number=2))

    def test_resolve_thumbnail_target_matches_design_in_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            client = self._client(tmpdir)
            with (
                patch.object(
                    client,
                    "find_design_in_folder",
                    return_value=CanvaDesignSummary(id="DAG999", title="Launch video"),
                ),
                patch(
                    "media_publisher.sources.canva.parse_canva_resource",
                    return_value=("folder", "FAF123"),
                ),
                patch(
                    "media_publisher.sources.canva.decode_access_token_scopes",
                    return_value=DEFAULT_SCOPES,
                ),
            ):
                target = client.resolve_thumbnail_target(
                    CANVA_SHORT_VIDEO_THUMBNAILS_URL,
                    "Launch video",
                )
        self.assertEqual(target, CanvaThumbnailTarget(design_id="DAG999"))

    def test_resolve_thumbnail_target_falls_back_to_design_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            client = self._client(tmpdir)
            with (
                patch.object(
                    client,
                    "list_design_pages_info",
                    return_value=[CanvaDesignPageInfo(page_number=1, title="Other")],
                ),
                patch.object(
                    client,
                    "find_design_by_title",
                    return_value=CanvaDesignSummary(id="DAG555", title="Launch video"),
                ),
                patch(
                    "media_publisher.sources.canva.parse_canva_resource",
                    return_value=("design", "DAG123"),
                ),
                patch(
                    "media_publisher.sources.canva.decode_access_token_scopes",
                    return_value=DEFAULT_SCOPES,
                ),
            ):
                target = resolve_thumbnail_target(
                    client,
                    CANVA_LONG_VIDEO_THUMBNAILS_URL,
                    "Launch video",
                )
        self.assertEqual(target, CanvaThumbnailTarget(design_id="DAG555"))

    def test_resolve_thumbnail_target_reports_missing_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            client = self._client(tmpdir)
            with (
                patch.object(
                    client,
                    "list_design_pages_info",
                    return_value=[CanvaDesignPageInfo(page_number=1, title="Other")],
                ),
                patch(
                    "media_publisher.sources.canva.parse_canva_resource",
                    return_value=("design", "DAG123"),
                ),
                patch(
                    "media_publisher.sources.canva.decode_access_token_scopes",
                    return_value=("design:content:read",),
                ),
            ):
                with self.assertRaises(CanvaError) as ctx:
                    resolve_thumbnail_target(
                        client,
                        CANVA_LONG_VIDEO_THUMBNAILS_URL,
                        "Launch video",
                    )
        self.assertIn("design:meta:read", str(ctx.exception))
        self.assertIn("--canva-auth", str(ctx.exception))

    def test_ensure_catalog_thumbnail_from_canva_uses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            download_dir = Path(tmpdir)
            cached = thumbnail_destination_path(download_dir, "Launch video")
            cached.write_bytes(b"png")
            job = PublishJob(
                title="Преведено заглавие",
                metadata={"Original Video Name": "Launch video"},
                video_format="post",
            )
            client = self._client(tmpdir)
            with patch.object(client, "resolve_thumbnail_target") as resolve_mock:
                enriched = ensure_catalog_thumbnail_from_canva(
                    job,
                    client=client,
                    download_dir=download_dir,
                )
            resolve_mock.assert_not_called()
            self.assertEqual(enriched.thumbnail_path, str(cached))

    def test_ensure_catalog_thumbnail_from_canva_downloads_png(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            download_dir = Path(tmpdir)
            destination = thumbnail_destination_path(download_dir, "Launch video")
            job = PublishJob(
                title="Преведено заглавие",
                metadata={"Original Video Name": "Launch video"},
                video_format="short_form",
            )
            client = self._client(tmpdir)
            with (
                patch.object(
                    client,
                    "resolve_thumbnail_target",
                    return_value=CanvaThumbnailTarget(design_id="DAG123", page_number=3),
                ) as resolve_mock,
                patch.object(
                    client,
                    "download_thumbnail_target",
                    return_value=destination,
                ) as download_mock,
            ):
                enriched = ensure_catalog_thumbnail_from_canva(
                    job,
                    client=client,
                    download_dir=download_dir,
                )
            resolve_mock.assert_called_once_with(
                CANVA_SHORT_VIDEO_THUMBNAILS_URL,
                "Launch video",
            )
            download_mock.assert_called_once()
            self.assertEqual(enriched.thumbnail_path, str(destination))


if __name__ == "__main__":
    unittest.main()
