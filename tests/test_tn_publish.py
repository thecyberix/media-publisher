from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from media_publisher.sources.google_drive import DriveFile
from media_publisher.sources.tn_publish import (
    TnPublishSettings,
    generate_catalog_tn_thumbnail,
    reference_thumbnail_size,
)
from media_publisher.sources.tn_psd import ImageSize


class ReferenceThumbnailSizeTests(unittest.TestCase):
    def test_uses_drive_video_dimensions_first(self) -> None:
        drive = MagicMock()
        with patch(
            "catalog_parser.drive_video_size.video_size_from_pkg_folder",
            return_value=(1080, 1920),
        ):
            size = reference_thumbnail_size(
                {
                    "Original Video": "https://youtu.be/demo",
                    "Video Folder": "https://drive.google.com/drive/folders/folder123",
                },
                title="Demo",
                original_dir=Path("downloads/original-thumbnails"),
                drive=drive,
                folder_id="folder123",
            )

        assert size is not None
        self.assertEqual((size.width, size.height), (1080, 1920))
        self.assertEqual(size.source, "drive-video")

    def test_falls_back_to_original_video_dimensions(self) -> None:
        drive = MagicMock()
        with patch(
            "catalog_parser.drive_video_size.video_size_from_pkg_folder",
            return_value=None,
        ):
            with patch(
                "media_publisher.sources.tn_publish.video_size_from_source_url",
                return_value=(1920, 1080),
            ):
                size = reference_thumbnail_size(
                    {"Original Video": "https://youtu.be/demo"},
                    title="Demo",
                    original_dir=Path("downloads/original-thumbnails"),
                    drive=drive,
                    folder_id="folder123",
                )

        assert size is not None
        self.assertEqual(size.width, 1920)
        self.assertEqual(size.height, 1080)
        self.assertEqual(size.source, "original-video")

    def test_falls_back_to_airtable_attachment_dimensions(self) -> None:
        with patch(
            "media_publisher.sources.tn_publish.video_size_from_source_url",
            side_effect=AssertionError("should not call when URL missing"),
        ):
            size = reference_thumbnail_size(
                {
                    "Original Video Thumbnail": [
                        {"width": 900, "height": 1600},
                    ]
                },
                title="Demo",
                original_dir=Path("downloads/original-thumbnails"),
            )

        assert size is not None
        self.assertEqual((size.width, size.height), (900, 1600))
        self.assertEqual(size.source, "airtable-thumbnail")


class GenerateCatalogTnThumbnailTests(unittest.TestCase):
    def test_picks_psd_layer_with_matching_aspect_ratio(self) -> None:
        drive = MagicMock()
        drive.list_children.return_value = [
            DriveFile(
                id="psd1",
                name="Template.psd",
                mime_type="image/vnd.adobe.photoshop",
            )
        ]

        def _fake_collect(_path: Path) -> list[ImageSize]:
            return [
                ImageSize(width=1920, height=1080, source="artboard:landscape"),
                ImageSize(width=1080, height=1920, source="artboard:portrait"),
            ]

        def _write_render(**kwargs):
            dest = kwargs["destination"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"jpg")
            return MagicMock(destination=dest)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            settings = TnPublishSettings(
                original_dir=tmp / "original",
                cache_dir=tmp / "cache",
                output_dir=tmp / "rendered",
                english_override_file=tmp / "overrides.json",
            )
            with (
                patch(
                    "media_publisher.sources.tn_publish.reference_thumbnail_size",
                    return_value=ImageSize(width=1080, height=1920, source="original-video"),
                ),
                patch(
                    "media_publisher.sources.tn_publish.collect_image_sizes",
                    side_effect=_fake_collect,
                ),
                patch(
                    "media_publisher.sources.tn_publish.load_template_image",
                    return_value=(MagicMock(), []),
                ) as load_mock,
                patch(
                    "media_publisher.sources.tn_publish.render_tn_thumbnail",
                    side_effect=_write_render,
                ) as render_mock,
            ):
                result = generate_catalog_tn_thumbnail(
                    title="Demo reel",
                    record_fields={
                        "Video caption translated": "Line one\nLine two",
                        "Video Folder": "https://drive.google.com/drive/folders/folder123",
                    },
                    drive=drive,
                    settings=settings,
                )

        self.assertTrue(result.name.endswith(".tn-render.jpg"))
        load_mock.assert_called_once()
        matched = load_mock.call_args.args[1]
        self.assertEqual((matched.width, matched.height), (1080, 1920))
        render_mock.assert_called_once()
        self.assertEqual(render_mock.call_args.kwargs["english_text"], "Line one\nLine two")


class ComposeOfflineTnBackgroundTests(unittest.TestCase):
    def test_prefers_drive_psd_pixels_and_keeps_original_styles(self) -> None:
        from PIL import Image

        from media_publisher.sources.tn_psd import TnLineStyle
        from media_publisher.sources.tn_publish import (
            DriveTnTemplate,
            compose_offline_tn_background,
        )

        original = Image.new("RGB", (100, 200), (10, 10, 10))
        covered = Image.new("RGB", (100, 200), (20, 20, 20))
        drive_image = Image.new("RGB", (200, 400), (30, 30, 30))
        styles = [
            TnLineStyle(
                placeholder_text="Hello",
                rendered_text="Hello",
                bbox=(10, 20, 90, 50),
                font_size_px=20.0,
                color_hex="#FFFFFF",
            )
        ]
        template, scaled, source = compose_offline_tn_background(
            original=original,
            covered_original=covered,
            drive_template=DriveTnTemplate(
                image=drive_image,
                line_styles=[],
                cached_path=Path("Template.psd"),
            ),
            cover_mode="bottom",
            line_styles=styles,
            caption_line_count=1,
        )
        self.assertEqual(source, "drive-template")
        self.assertEqual(template.size, (200, 400))
        self.assertEqual(template.getpixel((0, 0)), (30, 30, 30))
        self.assertEqual(scaled[0].bbox, (20, 40, 180, 100))
        self.assertEqual(scaled[0].font_size_px, 40.0)

    def test_falls_back_to_covered_original_without_drive_template(self) -> None:
        from PIL import Image

        from media_publisher.sources.tn_psd import TnLineStyle
        from media_publisher.sources.tn_publish import compose_offline_tn_background

        original = Image.new("RGB", (100, 200), (10, 10, 10))
        covered = Image.new("RGB", (100, 200), (20, 20, 20))
        styles = [
            TnLineStyle(
                placeholder_text="Hello",
                rendered_text="Hello",
                bbox=(10, 20, 90, 50),
                font_size_px=20.0,
                color_hex="#FFFFFF",
            )
        ]
        template, result_styles, source = compose_offline_tn_background(
            original=original,
            covered_original=covered,
            drive_template=None,
            cover_mode="bottom",
            line_styles=styles,
            caption_line_count=1,
        )
        self.assertEqual(source, "original-thumbnail")
        self.assertEqual(template.getpixel((0, 0)), (20, 20, 20))
        self.assertEqual(result_styles[0].bbox, (10, 20, 90, 50))


if __name__ == "__main__":
    unittest.main()
