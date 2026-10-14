from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from media_publisher.ekadashi import EkadashiEntry
from media_publisher.ekadashi_pipeline import (
    load_publish_state,
    pending_platforms,
    publish_ekadashi_to_youtube,
    run_ekadashi_publish,
)
from media_publisher.post_templates import (
    build_ekadashi_youtube_title,
    is_published_youtube_quote,
)


def _entry() -> EkadashiEntry:
    return EkadashiEntry(
        year=2026,
        month=10,
        day=22,
        name="Ekadashi",
        caption="Пост на Екадаши. #Екадаши",
        page_number=4,
    )


class EkadashiPublishTests(unittest.TestCase):
    def test_quiet_day_does_not_publish(self) -> None:
        with patch(
            "media_publisher.ekadashi_pipeline.publish_local_quote_to_facebook"
        ) as facebook:
            code = run_ekadashi_publish(
                project_root=Path("."),
                publish_timezone="Europe/Sofia",
                canva_client=MagicMock(),
                page_id="page",
                instagram_account_id="ig",
                access_token="token",
                today=date(2026, 10, 20),
                entries=(_entry(),),
                print_line=lambda _message: None,
            )
        self.assertEqual(code, 0)
        facebook.assert_not_called()

    def test_due_post_is_published_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            image = root / "page.jpg"
            image.write_bytes(b"jpg")
            state_path = root / "state.json"
            design = MagicMock(design_id="design", is_fallback=False, title="Ekadashi 2026")
            with patch(
                "media_publisher.ekadashi_pipeline.find_ekadashi_design",
                return_value=design,
            ):
                with patch(
                    "media_publisher.ekadashi_pipeline.export_ekadashi_page",
                    return_value=image,
                ) as export:
                    with patch(
                        "media_publisher.ekadashi_pipeline.publish_local_quote_to_facebook",
                        return_value="https://facebook.example/1",
                    ) as facebook:
                        with patch(
                            "media_publisher.ekadashi_pipeline.publish_local_quote_to_instagram",
                            return_value="https://instagram.example/1",
                        ) as instagram:
                            with patch(
                                "media_publisher.ekadashi_pipeline.publish_ekadashi_to_youtube",
                                return_value="https://youtube.example/1",
                            ) as youtube:
                                code = run_ekadashi_publish(
                                    project_root=root,
                                    publish_timezone="Europe/Sofia",
                                    canva_client=MagicMock(),
                                    page_id="page",
                                    instagram_account_id="ig",
                                    access_token="token",
                                    youtube_client_secrets=root / "client.json",
                                    youtube_token=root / "token.json",
                                    today=date(2026, 10, 21),
                                    entries=(_entry(),),
                                    state_path=state_path,
                                    print_line=lambda _message: None,
                                )
                                again = run_ekadashi_publish(
                                    project_root=root,
                                    publish_timezone="Europe/Sofia",
                                    canva_client=MagicMock(),
                                    page_id="page",
                                    instagram_account_id="ig",
                                    access_token="token",
                                    youtube_client_secrets=root / "client.json",
                                    youtube_token=root / "token.json",
                                    today=date(2026, 10, 21),
                                    entries=(_entry(),),
                                    state_path=state_path,
                                    print_line=lambda _message: None,
                                )

            self.assertEqual(code, 0)
            self.assertEqual(again, 0)
            export.assert_called_once()
            facebook.assert_called_once()
            instagram.assert_called_once()
            youtube.assert_called_once()
            state = load_publish_state(state_path)
            self.assertEqual(
                pending_platforms(
                    state, "2026-10-22", ("facebook", "instagram", "youtube")
                ),
                [],
            )
            self.assertEqual(
                state["2026-10-22"]["platforms"]["facebook"]["permalink"],
                "https://facebook.example/1",
            )

    def test_dry_run_does_not_export_or_post(self) -> None:
        with patch(
            "media_publisher.ekadashi_pipeline.find_ekadashi_design"
        ) as design:
            with patch(
                "media_publisher.ekadashi_pipeline.publish_local_quote_to_facebook"
            ) as facebook:
                code = run_ekadashi_publish(
                    project_root=Path("."),
                    publish_timezone="Europe/Sofia",
                    canva_client=MagicMock(),
                    page_id="page",
                    instagram_account_id="ig",
                    access_token="token",
                    today=date(2026, 10, 21),
                    entries=(_entry(),),
                    dry_run=True,
                    print_line=lambda _message: None,
                )
        self.assertEqual(code, 0)
        design.assert_not_called()
        facebook.assert_not_called()

    def test_youtube_uses_the_quote_video_and_ekadashi_playlist(self) -> None:
        caption = "Утре си вземете почивка от храната. #Екадаши"
        title = build_ekadashi_youtube_title(caption)
        self.assertTrue(title.endswith("#Екадаши"))
        self.assertNotIn("#Садгуру", title)
        self.assertFalse(is_published_youtube_quote(title, caption))
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            image = root / "still.jpg"
            video = root / "still.mp4"
            image.write_bytes(b"jpg")
            video.write_bytes(b"mp4")
            secrets = root / "client.json"
            token = root / "token.json"
            secrets.write_text("{}", encoding="utf-8")
            token.write_text("{}", encoding="utf-8")
            with patch(
                "media_publisher.ekadashi_pipeline.ensure_quote_video",
                return_value=video,
            ) as ensure:
                with patch(
                    "media_publisher.ekadashi_pipeline.publish_to_youtube",
                    return_value="vid123",
                ) as publish:
                    url = publish_ekadashi_to_youtube(
                        image_path=image,
                        caption=caption,
                        client_secrets_path=secrets,
                        token_path=token,
                        expected_channel_handle="@channel",
                        work_dir=root,
                    )
        self.assertEqual(url, "https://www.youtube.com/watch?v=vid123")
        ensure.assert_called_once()
        self.assertIsNone(ensure.call_args.kwargs.get("duration_seconds"))
        self.assertEqual(publish.call_args.kwargs["quote_playlist_title"], "Екадаши")
        self.assertIsNone(publish.call_args.kwargs.get("playlist_id"))
        self.assertIsNone(publish.call_args.kwargs.get("daily_playlist_id"))
        job = publish.call_args.args[0]
        self.assertEqual(job.video_format, "short_form")
        self.assertEqual(job.content_kind, "image")
        self.assertEqual(job.title, title)
