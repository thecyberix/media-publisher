from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from media_publisher.events.format import (
    format_bulgarian_datetime,
    parse_event_date,
    parse_event_time,
)
from media_publisher.events.page import (
    EMPTY_STATE_TEXT,
    SMARTLINK_ACCENT,
    SMARTLINK_BACKGROUND,
    SMARTLINK_TEXT,
    append_event,
    load_events,
    prune_past_events,
    rebuild_index,
)
from media_publisher.events.facebook_event import resolve_facebook_image_from_drive
from media_publisher.events.publish import (
    REQUIRED_EVENT_META_SCOPES,
    check_event_meta_scopes,
    publish_event,
)
from media_publisher.events.templates import (
    BHUTA_SHUDDHI_LEARN_MORE_URL,
    EVENT_TYPE_BHUTA_SHUDDHI,
    EVENT_TYPE_SURYA_KRIYA,
    SURYA_KRIYA_LEARN_MORE_LABEL,
    SURYA_KRIYA_LEARN_MORE_URL,
    city_preposition,
    render_event,
)
from media_publisher.publishers.meta import MetaClient, MetaError
from media_publisher.sources.google_drive import DriveFile
from media_publisher.sources.google_drive import DriveFile


class EventFormatTests(unittest.TestCase):
    def test_parse_and_format_bulgarian_datetime(self) -> None:
        self.assertEqual(parse_event_date("2026-09-15"), date(2026, 9, 15))
        self.assertEqual(parse_event_time("9:05"), time(9, 5))
        self.assertEqual(
            format_bulgarian_datetime(date(2026, 9, 15), time(18, 0)),
            "15 септември 2026 г., 18:00",
        )

    def test_rejects_invalid_date_and_time(self) -> None:
        with self.assertRaises(ValueError):
            parse_event_date("15/09/2026")
        with self.assertRaises(ValueError):
            parse_event_time("25:00")


class EventTemplateTests(unittest.TestCase):
    def test_render_surya_kriya_bulgarian(self) -> None:
        rendered = render_event(
            event_type=EVENT_TYPE_SURYA_KRIYA,
            city="София",
            country="България",
            event_date=date(2026, 9, 15),
            event_time=time(18, 0),
            registration_link="https://example.com/register",
        )
        self.assertIn("Суря крия", rendered.title)
        self.assertIn("София", rendered.title)
        self.assertIn("\nв София, България", rendered.title)
        self.assertIn("15 септември 2026 г., 18:00", rendered.full_text)
        self.assertIn("https://example.com/register", rendered.full_text)
        self.assertIn(SURYA_KRIYA_LEARN_MORE_URL, rendered.full_text)
        self.assertIn("https://example.com/register", rendered.facebook_post_text)
        self.assertIn(SURYA_KRIYA_LEARN_MORE_URL, rendered.facebook_post_text)
        self.assertIn("👉 Регистрация тук:", rendered.facebook_post_text)
        self.assertIn(
            f"Вижте какво казва Садгуру: {SURYA_KRIYA_LEARN_MORE_URL}",
            rendered.facebook_post_text,
        )
        self.assertNotIn(SURYA_KRIYA_LEARN_MORE_LABEL, rendered.facebook_post_text)
        self.assertIn(
            '☀️ Програма "Суря крия" в София, България ☀️',
            rendered.facebook_post_text,
        )
        self.assertTrue(
            rendered.facebook_post_text.startswith(
                '☀️ Програма "Суря крия" в София, България ☀️\n'
            )
        )
        self.assertIn("Регистрация", rendered.html_body)
        self.assertIn("<br>в София, България</h2>", rendered.html_body)
        self.assertIn('class="yt-link"', rendered.html_body)
        self.assertIn('class="yt-title"', rendered.html_body)
        self.assertIn(SURYA_KRIYA_LEARN_MORE_LABEL, rendered.html_body)
        self.assertIn(
            f'href="{SURYA_KRIYA_LEARN_MORE_URL}"',
            rendered.html_body,
        )

    def test_render_bhuta_shuddhi_bulgarian(self) -> None:
        rendered = render_event(
            event_type=EVENT_TYPE_BHUTA_SHUDDHI,
            city="Пловдив",
            country="България",
            event_date=date(2026, 10, 5),
            event_time=time(11, 0),
            registration_link="https://example.com/bhuta",
        )
        self.assertEqual(rendered.event_type, EVENT_TYPE_BHUTA_SHUDDHI)
        self.assertIn("Бута Шудди", rendered.title)
        self.assertIn("\nв Пловдив, България", rendered.title)
        self.assertIn("петте елемента", rendered.full_text)
        self.assertIn(BHUTA_SHUDDHI_LEARN_MORE_URL, rendered.full_text)
        self.assertIn(
            f"Вижте видеото: {BHUTA_SHUDDHI_LEARN_MORE_URL}",
            rendered.facebook_post_text,
        )
        self.assertIn(
            '💫 Програма "Бута Шудди" в Пловдив, България 💫',
            rendered.facebook_post_text,
        )
        self.assertIn("Регистрация", rendered.html_body)
        self.assertEqual(rendered.facebook_image_name, "bhuta-shuddhi-fb.jpg")

    def test_city_preposition_vv_before_v(self) -> None:
        self.assertEqual(city_preposition("София"), "в")
        self.assertEqual(city_preposition("Варна"), "във")
        self.assertEqual(city_preposition("варна"), "във")
        rendered = render_event(
            event_type=EVENT_TYPE_SURYA_KRIYA,
            city="Варна",
            country="България",
            event_date=date(2026, 9, 25),
            event_time=time(10, 0),
            registration_link="https://example.com/varna",
        )
        self.assertIn("\nвъв Варна, България", rendered.title)
        self.assertIn("<br>във Варна, България</h2>", rendered.html_body)

    def test_unsupported_event_type(self) -> None:
        with self.assertRaises(ValueError):
            render_event(
                event_type="yogasana",
                city="София",
                country="България",
                event_date=date(2026, 9, 15),
                event_time=time(18, 0),
                registration_link="https://example.com/register",
            )


class EventPageTests(unittest.TestCase):
    def test_append_event_and_skip_duplicate(self) -> None:
        rendered = render_event(
            event_type=EVENT_TYPE_SURYA_KRIYA,
            city="Пловдив",
            country="България",
            event_date=date(2026, 10, 1),
            event_time=time(19, 30),
            registration_link="https://example.com/plovdiv",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, created = append_event(
                root,
                rendered,
                facebook_post_id="page_1",
                facebook_permalink="https://www.facebook.com/page_1",
            )
            self.assertTrue(created)
            self.assertTrue((root / "data" / "events.json").is_file())
            self.assertTrue((root / "index.html").is_file())
            html = (root / "index.html").read_text(encoding="utf-8")
            self.assertIn("Пловдив", html)
            self.assertNotIn("Facebook пост", html)
            self.assertIn("Събития", html)
            self.assertIn("assets/sadhguru.png", html)
            self.assertNotIn("Обявени програми", html)
            self.assertNotIn("Доброволци от Иша · Садгуру България", html)
            self.assertIn("justify-content: center", html)
            self.assertIn("border-radius: 16px", html)
            self.assertIn("text-overflow: ellipsis", html)
            self.assertIn("yt-link", html)

            second, created_again = append_event(root, rendered)
            self.assertFalse(created_again)
            self.assertEqual(second.id, first.id)
            events = load_events(root)
            self.assertEqual(len(events), 1)

    def test_rebuild_index_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir(parents=True)
            (root / "data" / "events.json").write_text(
                json.dumps({"events": []}),
                encoding="utf-8",
            )
            path = rebuild_index(root)
            html = path.read_text(encoding="utf-8")
            self.assertIn(EMPTY_STATE_TEXT, html)
            self.assertIn(SMARTLINK_BACKGROUND, html)
            self.assertIn(SMARTLINK_TEXT, html)
            self.assertIn(SMARTLINK_ACCENT, html)
            self.assertIn("Merriweather", html)
            self.assertIn("coming-soon", html)
            self.assertIn("is-empty", html)

    def test_prune_past_events(self) -> None:
        past = render_event(
            event_type=EVENT_TYPE_SURYA_KRIYA,
            city="София",
            country="България",
            event_date=date(2020, 1, 1),
            event_time=time(10, 0),
            registration_link="https://example.com/past",
        )
        future = render_event(
            event_type=EVENT_TYPE_SURYA_KRIYA,
            city="Варна",
            country="България",
            event_date=date(2099, 6, 1),
            event_time=time(18, 0),
            registration_link="https://example.com/future",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = datetime(2019, 12, 31, 12, 0, tzinfo=ZoneInfo("Europe/Sofia"))
            append_event(root, past, now=before)
            append_event(root, future, now=before)
            self.assertEqual(len(load_events(root)), 2)
            kept, removed = prune_past_events(
                root,
                now=datetime(2026, 8, 11, 12, 0, tzinfo=ZoneInfo("Europe/Sofia")),
            )
            self.assertEqual(len(removed), 1)
            self.assertEqual(removed[0]["city"], "София")
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0]["city"], "Варна")
            html = (root / "index.html").read_text(encoding="utf-8")
            self.assertIn("Варна", html)
            self.assertNotIn("София", html)
            self.assertEqual(len(load_events(root)), 1)


class EventPublishTests(unittest.TestCase):
    def test_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            events_root = project / "events"
            result = publish_event(
                event_type="surya_kriya",
                city="Варна",
                country="България",
                date_text="2026-11-02",
                time_text="17:00",
                registration_link="https://example.com/varna",
                project_root=project,
                events_root=events_root,
                dry_run=True,
            )
            self.assertTrue(result.dry_run)
            self.assertFalse(events_root.exists())

    def test_skip_facebook_writes_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            events_root = project / "events"
            result = publish_event(
                event_type="surya_kriya",
                city="Варна",
                country="България",
                date_text="2026-11-02",
                time_text="17:00",
                registration_link="https://example.com/varna",
                project_root=project,
                events_root=events_root,
                skip_facebook=True,
            )
            self.assertTrue(result.created_on_page)
            self.assertIsNone(result.facebook_post_id)
            self.assertTrue((events_root / "index.html").is_file())

            again = publish_event(
                event_type="surya_kriya",
                city="Варна",
                country="България",
                date_text="2026-11-02",
                time_text="17:00",
                registration_link="https://example.com/varna",
                project_root=project,
                events_root=events_root,
                skip_facebook=True,
            )
            self.assertTrue(again.skipped_duplicate)
            self.assertFalse(again.created_on_page)

    def test_check_event_meta_scopes_reports_missing(self) -> None:
        fake_info = MagicMock()
        fake_info.scopes = ("pages_manage_posts", "pages_show_list")
        with patch(
            "media_publisher.events.publish.inspect_access_token",
            return_value=fake_info,
        ):
            info, missing = check_event_meta_scopes(
                access_token="token",
                app_id="app",
                app_secret="secret",
                api_version="v21.0",
            )
        self.assertIs(info, fake_info)
        self.assertEqual(missing, [])
        self.assertIn("pages_manage_posts", REQUIRED_EVENT_META_SCOPES)


class EventFacebookImageTests(unittest.TestCase):
    def test_resolve_facebook_image_from_drive(self) -> None:
        drive = MagicMock()
        drive.find_child_by_name.return_value = DriveFile(
            id="file123",
            name="surya-kriya-fb.jpg",
            mime_type="image/jpeg",
        )

        def _download(_file_id: str, destination: Path) -> Path:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"fake-image")
            return destination

        drive.download_file.side_effect = _download
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = resolve_facebook_image_from_drive(
                project_root=root,
                event_type=EVENT_TYPE_SURYA_KRIYA,
                drive_client=drive,
            )
            self.assertTrue(path.is_file())
            self.assertEqual(path.name, "surya-kriya-fb.jpg")
            drive.find_child_by_name.assert_called_once()
            drive.download_file.assert_called_once()


class MetaFeedAndCommentTests(unittest.TestCase):
    def test_create_facebook_feed_post(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        with patch.object(client, "_request", return_value={"id": "page_99"}) as request_mock:
            post_id = client.create_facebook_feed_post(
                page_id="page123",
                message="Hello event",
            )
        self.assertEqual(post_id, "page_99")
        request_mock.assert_called_once_with(
            "POST",
            "page123/feed",
            body={"message": "Hello event", "published": "true"},
        )

    def test_create_facebook_photo_post(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "event.jpg"
            image.write_bytes(b"fake-image")
            with patch.object(
                client,
                "_multipart_request",
                return_value={"id": "photo_1", "post_id": "page123_99"},
            ) as request_mock:
                post_id = client.create_facebook_photo_post(
                    page_id="page123",
                    message="Hello event",
                    image_path=image,
                )
        self.assertEqual(post_id, "page123_99")
        request_mock.assert_called_once()
        args, kwargs = request_mock.call_args
        self.assertEqual(args[0], "page123/photos")
        self.assertEqual(kwargs["fields"]["published"], "true")
        self.assertEqual(kwargs["fields"]["caption"], "Hello event")
        self.assertIn("source", kwargs["files"])

    def test_create_facebook_comment(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        with patch.object(client, "_request", return_value={"id": "comment_1"}) as request_mock:
            comment_id = client.create_facebook_comment(
                object_id="page_99",
                message="👉 Регистрация тук: https://example.com",
            )
        self.assertEqual(comment_id, "comment_1")
        request_mock.assert_called_once_with(
            "POST",
            "page_99/comments",
            body={"message": "👉 Регистрация тук: https://example.com"},
        )

    def test_create_facebook_feed_post_requires_message(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        with self.assertRaises(MetaError):
            client.create_facebook_feed_post(page_id="page123", message="  ")


if __name__ == "__main__":
    unittest.main()
