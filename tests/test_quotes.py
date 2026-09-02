from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from media_publisher.quotes_pipeline import (
    QuotesPipelineSettings,
    filter_quotes_for_local_date,
    pending_platforms,
    resolve_quote_month,
    run_quotes_pipeline,
)
from media_publisher.quotes_render_pipeline import resolve_quote_days_to_prepare
from media_publisher.sources.quote_pdf import extract_pdf_page_text, normalize_extracted_text
from media_publisher.sources.quotes import (
    discover_monthly_quotes,
    load_quote_state,
    mark_platform_scheduled_in_state,
    parse_quote_date_from_stem,
    platform_permalink,
    quote_canva_design_title,
    quote_state_path,
    save_quote_state,
)


MONTHLY_PDF = Path(__file__).resolve().parents[1] / "downloads" / "canva" / "DAGaqof2VGI.pdf"


class QuoteDateParsingTests(unittest.TestCase):
    def test_parse_iso_date_stem(self) -> None:
        publish_at = parse_quote_date_from_stem(
            "2026-07-05",
            publish_timezone="Europe/Sofia",
            publish_hour=8,
        )
        self.assertIsNotNone(publish_at)
        assert publish_at is not None
        local = publish_at.astimezone(ZoneInfo("Europe/Sofia"))
        self.assertEqual(local.hour, 8)
        self.assertEqual(local.date().isoformat(), "2026-07-05")

    def test_parse_bulgarian_date_stem(self) -> None:
        publish_at = parse_quote_date_from_stem(
            "05 Юли",
            publish_timezone="Europe/Sofia",
            publish_hour=8,
            today=date(2026, 7, 4),
        )
        self.assertIsNotNone(publish_at)
        assert publish_at is not None
        local = publish_at.astimezone(ZoneInfo("Europe/Sofia"))
        self.assertEqual(local.day, 5)
        self.assertEqual(local.month, 7)
        self.assertEqual(local.year, 2026)
        self.assertEqual(local.hour, 8)


class QuoteRenderPlanningTests(unittest.TestCase):
    def test_resolve_quote_days_to_prepare_staggered(self) -> None:
        days = resolve_quote_days_to_prepare(
            year=2026,
            month=7,
            publish_mode="staggered",
            reference_date=date(2026, 7, 15),
        )
        self.assertEqual(days, {15, 16})

    def test_resolve_quote_days_to_prepare_staggered_instagram_only(self) -> None:
        days = resolve_quote_days_to_prepare(
            year=2026,
            month=7,
            publish_mode="staggered",
            reference_date=date(2026, 7, 15),
            platforms=("instagram",),
        )
        self.assertEqual(days, {15})

    def test_resolve_quote_days_to_prepare_staggered_youtube_facebook_only(self) -> None:
        days = resolve_quote_days_to_prepare(
            year=2026,
            month=7,
            publish_mode="staggered",
            reference_date=date(2026, 7, 15),
            platforms=("youtube", "facebook"),
        )
        self.assertEqual(days, {16})

    def test_resolve_quote_days_to_prepare_single_day(self) -> None:
        days = resolve_quote_days_to_prepare(
            year=2026,
            month=7,
            publish_mode="immediate",
            reference_date=date(2026, 7, 15),
        )
        self.assertEqual(days, {15})

    def test_prepare_skips_missing_tomorrow_in_staggered(self) -> None:
        from media_publisher.quotes_render_pipeline import (
            RenderedQuoteImage,
            prepare_quote_posts_for_publish,
        )
        from media_publisher.sources.quotes_sheet import DailyQuoteText

        today_quote = DailyQuoteText(
            day=15,
            publish_date=date(2026, 7, 15),
            date_label="15 Jul 2026",
            text_bg="Днешна цитат",
        )
        rendered = RenderedQuoteImage(
            variant="fbyt",
            day=15,
            stem="2026-07-15",
            image_path=Path("2026-07-15.jpg"),
            caption="Днешна цитат",
            layout_key="default",
            line_count=1,
            background_name="15.jpg",
        )
        ig_rendered = RenderedQuoteImage(
            variant="ig",
            day=15,
            stem="2026-07-15",
            image_path=Path("ig-2026-07-15.jpg"),
            caption="Днешна цитат",
            layout_key="default",
            line_count=1,
            background_name="15.jpg",
        )

        with patch(
            "media_publisher.quotes_render_pipeline.load_monthly_quote_texts",
            return_value=[today_quote],
        ), patch(
            "media_publisher.quotes_render_pipeline.render_monthly_quotes",
            side_effect=[[rendered], [ig_rendered]],
        ) as render_mock:
            posts, ig_images = prepare_quote_posts_for_publish(
                config=unittest.mock.Mock(),
                sheets_client=unittest.mock.Mock(),
                drive_client=unittest.mock.Mock(),
                year=2026,
                month=7,
                publish_timezone="Europe/Sofia",
                publish_hour=8,
                publish_mode="staggered",
                reference_date=date(2026, 7, 15),
            )

        self.assertEqual([post.stem for post in posts], ["2026-07-15"])
        self.assertEqual(set(ig_images), {"2026-07-15"})
        rendered_days = {call.kwargs["day"] for call in render_mock.call_args_list}
        self.assertEqual(rendered_days, {15})


class QuoteCanvaTitleTests(unittest.TestCase):
    def test_quote_canva_design_title(self) -> None:
        self.assertEqual(
            quote_canva_design_title(2026, 7),
            "Юли 2026 FB/YT DMQ Template Final",
        )
        self.assertEqual(
            quote_canva_design_title(2026, 1),
            "Януари 2026 FB/YT DMQ Template Final",
        )

    def test_quote_canva_ig_design_title(self) -> None:
        from media_publisher.sources.quotes import quote_canva_ig_design_title

        self.assertEqual(
            quote_canva_ig_design_title(2026, 7),
            "Юли 2026 IG DMQ Template Final",
        )
        self.assertEqual(
            quote_canva_ig_design_title(2026, 1),
            "Януари 2026 IG DMQ Template Final",
        )


class MonthlyQuoteDiscoveryTests(unittest.TestCase):
    def test_discover_monthly_quotes_from_canva_pdf(self) -> None:
        if not MONTHLY_PDF.is_file():
            self.skipTest("monthly Canva PDF sample not present")

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            posts = discover_monthly_quotes(
                MONTHLY_PDF,
                year=2026,
                month=7,
                work_dir=work_dir,
            )

            self.assertEqual(len(posts), 31)
            self.assertEqual(posts[0].stem, "2026-07-01")
            self.assertEqual(posts[4].stem, "2026-07-05")
            self.assertIn("карма", posts[4].caption.casefold())
            self.assertTrue(posts[4].image_path.is_file())

    def test_extract_pdf_page_text_deduplicates_canva_duplicates(self) -> None:
        if not MONTHLY_PDF.is_file():
            self.skipTest("monthly Canva PDF sample not present")

        caption = extract_pdf_page_text(MONTHLY_PDF, 4)
        self.assertEqual(caption.count("кармата на този момент"), 1)


class QuoteStateTests(unittest.TestCase):
    def test_pending_platforms_can_be_limited(self) -> None:
        state = {
            "2026-07-05": {
                "platforms": {
                    "youtube": {"permalink": "https://www.youtube.com/watch?v=abc"},
                }
            }
        }
        from media_publisher.sources.quotes import LocalQuotePost

        post = LocalQuotePost(
            stem="2026-07-05",
            image_path=Path("quote.png"),
            caption="Caption",
            publish_at=datetime(2026, 7, 5, 8, 0, tzinfo=ZoneInfo("Europe/Sofia")),
            source_path=Path("quotes-2026-07.pdf"),
        )
        self.assertEqual(
            pending_platforms(post, state, platforms=("instagram",)),
            ["instagram"],
        )

    def test_mark_and_reload_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            state: dict[str, dict[str, object]] = {}
            publish_at = datetime(2026, 7, 5, 8, 0, tzinfo=ZoneInfo("Europe/Sofia"))
            mark_platform_scheduled_in_state(
                state,
                image_name="2026-07-05",
                platform="facebook",
                permalink="https://www.facebook.com/photo/?fbid=123",
                publish_at=publish_at,
                source_pdf="quotes-2026-07.pdf",
            )
            save_quote_state(work_dir, state)

            loaded = load_quote_state(work_dir)
            self.assertEqual(
                platform_permalink(loaded["2026-07-05"], "facebook"),
                "https://www.facebook.com/photo/?fbid=123",
            )
            self.assertTrue(quote_state_path(work_dir).is_file())


class QuotesPipelineTests(unittest.TestCase):
    def _settings(self, work_dir: Path, **kwargs) -> QuotesPipelineSettings:
        defaults = {
            "work_dir": work_dir,
            "project_root": work_dir,
            "quotes_sources_config": work_dir / "quotes_sources.json",
            "google_service_account": work_dir / "service-account.json",
            "publish_timezone": "Europe/Sofia",
            "publish_hour": 8,
            "template_urls": {},
            "meta_page_id": "page",
            "meta_instagram_account_id": "ig",
            "meta_access_token": "token",
            "meta_app_id": "app",
            "youtube_client_secrets": Path("auth/youtube-client.json"),
            "youtube_token": Path("auth/youtube-token.json"),
            "youtube_channel_handle": "SadhguruBulgarian",
            "youtube_playlist_id": None,
            "youtube_daily_playlist_id": None,
            "ffmpeg_path": "ffmpeg",
        }
        defaults.update(kwargs)
        return QuotesPipelineSettings(**defaults)

    def _sample_posts(
        self,
        work_dir: Path,
        *,
        stem: str = "2026-07-05",
        caption: str = "Кармата на този момент е твоята отговорност.",
    ) -> tuple:
        from media_publisher.sources.quotes import LocalQuotePost

        year, month, day = (int(part) for part in stem.split("-"))
        image = work_dir / f"{stem}.jpg"
        image.write_bytes(b"jpg")
        ig_image = work_dir / f"{stem}-ig.jpg"
        ig_image.write_bytes(b"jpg")
        post = LocalQuotePost(
            stem=stem,
            image_path=image,
            caption=caption,
            publish_at=datetime(
                year,
                month,
                day,
                8,
                0,
                tzinfo=ZoneInfo("Europe/Sofia"),
            ),
            source_path=image,
        )
        return post, ig_image

    def test_resolve_quote_month_from_publish_date(self) -> None:
        self.assertEqual(
            resolve_quote_month(date(2026, 7, 5), publish_timezone="Europe/Sofia"),
            (2026, 7),
        )

    def test_run_quotes_pipeline_uses_rendered_posts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            post, ig_image = self._sample_posts(work_dir, stem="2026-12-31")
            settings = self._settings(
                work_dir,
                publish_mode="scheduled",
                reference_date=date(2026, 12, 31),
            )

            with patch(
                "media_publisher.quotes_pipeline.facebook_can_schedule",
                return_value=True,
            ), patch(
                "media_publisher.quotes_pipeline.quote_is_due",
                return_value=True,
            ), patch(
                "media_publisher.quotes_pipeline.prepare_quote_posts_for_publish",
                return_value=([post], {post.stem: ig_image}),
            ), patch(
                "media_publisher.quotes_pipeline.publish_local_quote",
                side_effect=[
                    "https://www.youtube.com/watch?v=abc123",
                    "https://www.facebook.com/photo/?fbid=123",
                ],
            ) as publish_mock:
                exit_code, results = run_quotes_pipeline(
                    settings,
                    meta_client=unittest.mock.Mock(),
                    sheets_client=unittest.mock.Mock(),
                    drive_client=unittest.mock.Mock(),
                    quotes_config=unittest.mock.Mock(),
                    print_line=lambda _: None,
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(results), 2)
            self.assertTrue(all(result.success for result in results))
            self.assertEqual(publish_mock.call_count, 2)
            self.assertEqual(
                publish_mock.call_args_list[0].kwargs["caption"],
                "Кармата на този момент е твоята отговорност.",
            )

            state = load_quote_state(work_dir)
            self.assertIn("2026-12-31", state)

    def test_run_quotes_pipeline_publish_today_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            post, ig_image = self._sample_posts(work_dir)
            settings = self._settings(
                work_dir,
                publish_mode="scheduled",
                private_test=True,
                reference_date=date(2026, 7, 5),
            )

            with patch(
                "media_publisher.quotes_pipeline.prepare_quote_posts_for_publish",
                return_value=([post], {post.stem: ig_image}),
            ), patch(
                "media_publisher.quotes_pipeline.facebook_can_schedule",
                return_value=True,
            ), patch(
                "media_publisher.quotes_pipeline.quote_is_due",
                return_value=True,
            ), patch(
                "media_publisher.quotes_pipeline.publish_local_quote",
                side_effect=[
                    "https://www.youtube.com/watch?v=abc123",
                    "https://www.facebook.com/photo/?fbid=123",
                ],
            ) as publish_mock:
                exit_code, results = run_quotes_pipeline(
                    settings,
                    meta_client=unittest.mock.Mock(),
                    sheets_client=unittest.mock.Mock(),
                    drive_client=unittest.mock.Mock(),
                    quotes_config=unittest.mock.Mock(),
                    print_line=lambda _: None,
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(results), 2)
            self.assertEqual({result.platform for result in results}, {"youtube", "facebook"})
            self.assertTrue(all(result.success for result in results))
            self.assertEqual(publish_mock.call_count, 2)
            facebook_call = next(
                call
                for call in publish_mock.call_args_list
                if call.kwargs["platform"] == "facebook"
            )
            self.assertIsNotNone(facebook_call.kwargs["publish_at"])
            for call in publish_mock.call_args_list:
                self.assertFalse(call.kwargs["private"])
                self.assertIn(call.kwargs["platform"], ("youtube", "facebook"))

    def test_run_quotes_pipeline_limits_platform(self) -> None:
        from media_publisher.sources.quotes import LocalQuotePost

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            post, ig_image = self._sample_posts(work_dir)
            settings = self._settings(
                work_dir,
                publish_mode="immediate",
                reference_date=date(2026, 7, 5),
                platforms=("instagram",),
            )

            with patch(
                "media_publisher.quotes_pipeline.prepare_quote_posts_for_publish",
                return_value=([post], {post.stem: ig_image}),
            ), patch(
                "media_publisher.quotes_pipeline.publish_local_quote",
                return_value="https://www.instagram.com/p/abc123/",
            ) as publish_mock:
                exit_code, results = run_quotes_pipeline(
                    settings,
                    meta_client=unittest.mock.Mock(),
                    sheets_client=unittest.mock.Mock(),
                    drive_client=unittest.mock.Mock(),
                    quotes_config=unittest.mock.Mock(),
                    print_line=lambda _: None,
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].platform, "instagram")
            publish_mock.assert_called_once()
            self.assertEqual(publish_mock.call_args.kwargs["platform"], "instagram")
            self.assertEqual(
                publish_mock.call_args.kwargs["caption"],
                "Кармата на този момент е твоята отговорност.",
            )

    def test_filter_quotes_for_local_date(self) -> None:
        if not MONTHLY_PDF.is_file():
            self.skipTest("monthly Canva PDF sample not present")

        with tempfile.TemporaryDirectory() as tmpdir:
            posts = discover_monthly_quotes(
                MONTHLY_PDF,
                year=2026,
                month=7,
                work_dir=Path(tmpdir),
            )
        filtered = filter_quotes_for_local_date(
            posts,
            date(2026, 7, 5),
            publish_timezone="Europe/Sofia",
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].stem, "2026-07-05")


class QuoteCaptionNormalizationTests(unittest.TestCase):
    def test_normalize_extracted_text_deduplicates_blocks(self) -> None:
        raw = "Line one\nLine two\n\nLine one\nLine two\n\nFooter"
        self.assertEqual(
            normalize_extracted_text(raw),
            "Line one Line two\n\nFooter",
        )


class QuotePublishTests(unittest.TestCase):
    def test_publish_local_quote_to_instagram_uses_image(self) -> None:
        from media_publisher.publishers.quotes import publish_local_quote_to_instagram

        with tempfile.TemporaryDirectory() as tmpdir:
            image = Path(tmpdir) / "quote.png"
            image.write_bytes(b"png")

            with patch("media_publisher.publishers.quotes.MetaClient") as client_cls:
                client_cls.return_value.schedule_instagram_image.return_value = "ig_media_1"
                media_id = publish_local_quote_to_instagram(
                    image_path=image,
                    caption="Caption",
                    publish_at=None,
                    page_id="page",
                    instagram_account_id="ig",
                    access_token="token",
                    app_id="app",
                )

            self.assertEqual(media_id, "ig_media_1")
            client_cls.return_value.schedule_instagram_image.assert_called_once_with(
                instagram_account_id="ig",
                caption="Caption",
                image_path=image,
                page_id="page",
                publish_at=None,
            )


if __name__ == "__main__":
    unittest.main()
