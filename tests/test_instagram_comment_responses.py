"""Tests for Instagram keyword replies and DMs."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from catalog_parser.playlists import (
    END_CARD_INNER_ENGINEERING,
    PlaylistConfig,
    PlaylistRule,
    comment_response_rules,
    load_playlist_config,
)
from media_publisher.instagram_comment_responses import (
    CommentThread,
    InstagramComment,
    SentDirectMessage,
    backfill_instagram_comment_responses,
    comment_matches_keyword,
    commented_videos_from_report,
    direct_message_ids,
    load_response_state,
    pending_responses,
    public_reply_ids,
    remember_commented_video,
    run_instagram_comment_responses,
)
from media_publisher.publishers.meta import MetaClient, MetaError


CTA = (
    '"Вътрешно изграждане" е онлайн програма. '
    'Напишете "Вътрешно изграждане" в коментар, за да научите повече.'
)


def _rule() -> PlaylistRule:
    return PlaylistRule(
        "Вътрешно Изграждане",
        END_CARD_INNER_ENGINEERING,
        comment=CTA,
        keyword="Вътрешно изграждане",
        reply="Благодарим ви.",
        dm="Ето повече информация.",
    )


def _config() -> PlaylistConfig:
    return PlaylistConfig(rules=(_rule(), PlaylistRule("Съзнателна Планета")))


def _watch_list(media_id: str = "media1") -> dict:
    return {
        "videos": {
            media_id: {
                "permalink": "https://instagram.com/reel/abc",
                "end_cards": ["inner_engineering"],
            }
        },
        "comments": {},
    }


class CommentResponseTests(unittest.TestCase):
    def test_keyword_matches_ignoring_case_and_space(self) -> None:
        self.assertTrue(
            comment_matches_keyword("  вътрешно   изграждане! ", "Вътрешно изграждане")
        )
        self.assertFalse(comment_matches_keyword("хубаво видео", "Вътрешно изграждане"))

    def test_checked_in_rules_cover_inner_engineering_and_miracle_of_mind(self) -> None:
        rules = comment_response_rules(load_playlist_config())
        self.assertEqual(
            [rule.end_card for rule in rules],
            ["inner_engineering", "miracle_of_mind"],
        )
        for rule in rules:
            self.assertTrue(rule.keyword)
            self.assertTrue(rule.reply)
            self.assertTrue(rule.dm)

    def test_keyword_without_a_message_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "playlists.json"
            path.write_text(
                json.dumps(
                    {
                        "video_playlists": [
                            {"name": "Always"},
                            {"name": "IE", "keyword": "hello"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                load_playlist_config(path)

    def test_pending_skips_the_account_and_the_prompt_comment(self) -> None:
        now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
        comments = [
            InstagramComment("cta", CTA, username="page"),
            InstagramComment("own", "Вътрешно изграждане", username="page"),
            InstagramComment("fan", "Вътрешно изграждане", username="fan"),
            InstagramComment("other", "Благодаря", username="fan"),
        ]
        pending = pending_responses(
            comments,
            comment_response_rules(_config()),
            own_username="@page",
            handled={},
            now=now,
        )
        self.assertEqual([item.comment.comment_id for item in pending], ["fan"])
        self.assertTrue(pending[0].send_reply)
        self.assertTrue(pending[0].send_dm)

    def test_old_comment_still_gets_a_reply_but_not_a_dm(self) -> None:
        now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
        comments = [
            InstagramComment("cta", CTA, username="page"),
            InstagramComment(
                "old",
                "Вътрешно изграждане",
                username="fan",
                timestamp=now - timedelta(days=8),
            ),
        ]
        pending = pending_responses(
            comments,
            comment_response_rules(_config()),
            own_username="page",
            handled={},
            now=now,
        )
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0].send_reply)
        self.assertFalse(pending[0].send_dm)
        self.assertTrue(pending[0].skip_dm_window)

    def test_completed_comment_is_not_sent_again(self) -> None:
        comments = [
            InstagramComment("cta", CTA, username="page"),
            InstagramComment("fan", "Вътрешно изграждане", username="fan"),
        ]
        pending = pending_responses(
            comments,
            comment_response_rules(_config()),
            own_username="page",
            handled={"fan": {"reply_id": "r1", "dm_id": "m1"}},
        )
        self.assertEqual(pending, [])

    def test_report_keeps_videos_whose_comment_was_posted(self) -> None:
        report = {
            "videos": [
                {
                    "media_id": "ie1",
                    "permalink": "https://instagram.com/reel/ie",
                    "posted": ["inner_engineering"],
                    "already": [],
                    "pending": [],
                },
                {
                    "media_id": "mom1",
                    "permalink": "https://instagram.com/reel/mom",
                    "posted": [],
                    "already": ["miracle_of_mind"],
                    "pending": [],
                },
                {
                    "media_id": "soil1",
                    "permalink": "https://instagram.com/reel/soil",
                    "posted": ["save_soil"],
                    "already": [],
                    "pending": [],
                },
                {
                    "media_id": "later",
                    "permalink": "https://instagram.com/reel/later",
                    "posted": [],
                    "already": [],
                    "pending": ["inner_engineering"],
                },
            ]
        }
        videos = commented_videos_from_report(
            report,
            (
                _rule(),
                PlaylistRule(
                    "Чудото на Ума",
                    "miracle_of_mind",
                    comment="mom comment",
                    keyword="чудо",
                    reply="reply",
                    dm="dm",
                ),
            ),
        )
        self.assertEqual(
            [(item["media_id"], item["end_cards"]) for item in videos],
            [("ie1", ["inner_engineering"]), ("mom1", ["miracle_of_mind"])],
        )

    def test_publish_adds_a_video_to_the_watch_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            remember_commented_video(
                state_path,
                media_id="media1",
                end_cards=["inner_engineering"],
                permalink="https://instagram.com/reel/abc",
            )
            remember_commented_video(
                state_path,
                media_id="media1",
                end_cards=["miracle_of_mind"],
            )
            saved = load_response_state(state_path)
            self.assertEqual(
                saved["videos"]["media1"]["end_cards"],
                ["inner_engineering", "miracle_of_mind"],
            )
            self.assertEqual(
                saved["videos"]["media1"]["permalink"],
                "https://instagram.com/reel/abc",
            )

    def test_apply_sends_reply_and_dm_once(self) -> None:
        class _Client:
            def __init__(self) -> None:
                self.replies: list[str] = []
                self.dms: list[str] = []

            def list_instagram_comments(self, media_id: str) -> list[dict]:
                return [
                    {"id": "cta", "text": CTA, "username": "page"},
                    {"id": "fan", "text": "Вътрешно изграждане", "username": "fan"},
                ]

            def reply_to_instagram_comment(self, *, comment_id: str, message: str) -> str:
                self.replies.append(comment_id)
                return "reply1"

            def send_instagram_comment_dm(
                self, *, page_id: str, comment_id: str, message: str
            ) -> str:
                self.dms.append(comment_id)
                return "dm1"

        client = _Client()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps(_watch_list()), encoding="utf-8")
            code = run_instagram_comment_responses(
                client,  # type: ignore[arg-type]
                page_id="ig1",
                own_username="page",
                config=_config(),
                state_path=state_path,
                apply=True,
                log=lambda _line: None,
            )
            self.assertEqual(code, 0)
            self.assertEqual(client.replies, ["fan"])
            self.assertEqual(client.dms, ["fan"])
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["comments"]["fan"]["reply_id"], "reply1")
            self.assertEqual(saved["comments"]["fan"]["dm_id"], "dm1")

            client.replies.clear()
            client.dms.clear()
            again = run_instagram_comment_responses(
                client,  # type: ignore[arg-type]
                page_id="ig1",
                own_username="page",
                config=_config(),
                state_path=state_path,
                apply=True,
                log=lambda _line: None,
            )
            self.assertEqual(again, 0)
            self.assertEqual(client.replies, [])
            self.assertEqual(client.dms, [])

    def test_dry_run_does_not_write_state(self) -> None:
        class _Client:
            def list_instagram_comments(self, media_id: str) -> list[dict]:
                return [
                    {"id": "cta", "text": CTA, "username": "page"},
                    {"id": "fan", "text": "Вътрешно изграждане", "username": "fan"},
                ]

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            original = json.dumps(_watch_list())
            state_path.write_text(original, encoding="utf-8")
            code = run_instagram_comment_responses(
                _Client(),  # type: ignore[arg-type]
                page_id="ig1",
                own_username="page",
                config=_config(),
                state_path=state_path,
                apply=False,
                log=lambda _line: None,
            )
            self.assertEqual(code, 0)
            self.assertEqual(state_path.read_text(encoding="utf-8"), original)


class ExistingResponseTests(unittest.TestCase):
    def test_pairs_each_keyword_reply_with_the_next_page_reply(self) -> None:
        start = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
        thread = CommentThread(
            parent=InstagramComment("cta", CTA, username="page", timestamp=start),
            replies=(
                InstagramComment(
                    "early",
                    "Благодарим ви.",
                    username="page",
                    timestamp=start + timedelta(minutes=1),
                ),
                InstagramComment(
                    "fan1",
                    "Вътрешно изграждане",
                    username="fan1",
                    timestamp=start + timedelta(minutes=2),
                ),
                InstagramComment(
                    "reply1",
                    "Благодарим ви.",
                    username="page",
                    timestamp=start + timedelta(minutes=3),
                ),
                InstagramComment(
                    "fan2",
                    "вътрешно изграждане",
                    username="fan2",
                    timestamp=start + timedelta(minutes=4),
                ),
                InstagramComment(
                    "reply2",
                    "Благодарим ви.",
                    username="@page",
                    timestamp=start + timedelta(minutes=5),
                ),
            ),
        )
        found = public_reply_ids(
            thread,
            comment_response_rules(_config()),
            own_username="page",
        )
        self.assertEqual(
            {comment_id: reply_id for comment_id, (_rule, reply_id) in found.items()},
            {"fan1": "reply1", "fan2": "reply2"},
        )

    def test_direct_messages_pair_in_time_order(self) -> None:
        start = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
        rule = _rule()
        comments = [
            (
                InstagramComment("old", "keyword", username="fan", timestamp=start),
                rule,
            ),
            (
                InstagramComment(
                    "new",
                    "keyword",
                    username="fan",
                    timestamp=start + timedelta(hours=2),
                ),
                rule,
            ),
        ]
        messages = [
            SentDirectMessage(
                "too-early",
                "fan",
                rule.dm or "",
                start - timedelta(minutes=5),
            ),
            SentDirectMessage(
                "first",
                "Fan",
                rule.dm or "",
                start + timedelta(minutes=1),
            ),
            SentDirectMessage(
                "second",
                "fan",
                rule.dm or "",
                start + timedelta(hours=3),
            ),
        ]
        self.assertEqual(
            direct_message_ids(comments, messages),
            {"old": "first", "new": "second"},
        )

    def test_backfill_records_existing_reply_and_dm(self) -> None:
        now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
        old = now - timedelta(days=8)
        recent = now - timedelta(days=1)

        class _Client:
            def __init__(self) -> None:
                self.inbox_calls = 0

            def list_instagram_comment_threads(self, media_id: str) -> list[dict]:
                return [
                    {
                        "id": "cta",
                        "text": CTA,
                        "username": "page",
                        "timestamp": old.isoformat(),
                        "replies": [
                            {
                                "id": "old-fan",
                                "text": "Вътрешно изграждане",
                                "username": "oldfan",
                                "timestamp": old.isoformat(),
                            },
                            {
                                "id": "old-reply",
                                "text": "Благодарим ви.",
                                "username": "page",
                                "timestamp": (old + timedelta(minutes=1)).isoformat(),
                            },
                            {
                                "id": "new-fan",
                                "text": "Вътрешно изграждане",
                                "username": "newfan",
                                "timestamp": recent.isoformat(),
                            },
                            {
                                "id": "new-reply",
                                "text": "Благодарим ви.",
                                "username": "page",
                                "timestamp": (recent + timedelta(minutes=1)).isoformat(),
                            },
                            {
                                "id": "open-fan",
                                "text": "Вътрешно изграждане",
                                "username": "openfan",
                                "timestamp": (recent + timedelta(hours=1)).isoformat(),
                            },
                        ],
                    }
                ]

            def list_instagram_outbound_messages(
                self,
                page_id: str,
                *,
                own_username: str,
                since: datetime,
            ) -> list[dict]:
                self.inbox_calls += 1
                return [
                    {
                        "id": "dm-new",
                        "text": "Ето повече информация.",
                        "username": "newfan",
                        "timestamp": (recent + timedelta(minutes=2)).isoformat(),
                    }
                ]

        client = _Client()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps(_watch_list()), encoding="utf-8")
            code = backfill_instagram_comment_responses(
                client,  # type: ignore[arg-type]
                page_id="page1",
                own_username="page",
                config=_config(),
                state_path=state_path,
                apply=True,
                now=now,
                log=lambda _line: None,
            )
            self.assertEqual(code, 0)
            self.assertEqual(client.inbox_calls, 1)
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["comments"]["old-fan"]["reply_id"], "old-reply")
            self.assertEqual(saved["comments"]["old-fan"]["dm_skipped"], "window")
            self.assertNotIn("dm_id", saved["comments"]["old-fan"])
            self.assertEqual(saved["comments"]["new-fan"]["reply_id"], "new-reply")
            self.assertEqual(saved["comments"]["new-fan"]["dm_id"], "dm-new")
            self.assertNotIn("open-fan", saved["comments"])

            client.inbox_calls = 0
            again = backfill_instagram_comment_responses(
                client,  # type: ignore[arg-type]
                page_id="page1",
                own_username="page",
                config=_config(),
                state_path=state_path,
                apply=False,
                now=now,
                log=lambda _line: None,
            )
            self.assertEqual(again, 0)
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8")),
                saved,
            )

    def test_one_short_word_still_counts_as_our_reply(self) -> None:
        start = datetime(2026, 10, 21, 15, tzinfo=timezone.utc)
        configured = (
            'Намаскарам! Линк към приложението "Чудото на ума" '
            "ви беше изпратен на лично съобщение. 🙏"
        )
        posted = configured.replace(" на ", " в ", 1)
        rule = PlaylistRule(
            "Чудото на Ума",
            "miracle_of_mind",
            comment="prompt",
            keyword="чудо",
            reply=configured,
            dm="dm",
        )
        thread = CommentThread(
            parent=InstagramComment(
                "fan",
                "Чудото на ума",
                username="fan",
                timestamp=start,
            ),
            replies=(
                InstagramComment(
                    "reply",
                    posted,
                    username="page",
                    timestamp=start + timedelta(minutes=1),
                ),
            ),
        )
        found = public_reply_ids(thread, (rule,), own_username="page")
        self.assertEqual(found["fan"][1], "reply")


class InstagramCommentApiTests(unittest.TestCase):
    def test_private_reply_posts_json_to_the_page(self) -> None:
        client = MetaClient("token-test")
        with patch.object(
            client,
            "_request",
            return_value={"message_id": "m1"},
        ) as request:
            message_id = client.send_instagram_comment_dm(
                page_id="page1",
                comment_id="c1",
                message="hello",
            )
        self.assertEqual(message_id, "m1")
        request.assert_called_once_with(
            "POST",
            "page1/messages",
            json_body={
                "recipient": {"comment_id": "c1"},
                "message": {"text": "hello"},
            },
        )

    def test_comment_threads_keep_replies_nested(self) -> None:
        client = MetaClient("token-test")

        def request(method: str, path: str, **kwargs: object) -> dict:
            if path == "media1/comments":
                return {"data": [{"id": "parent", "text": "nice", "username": "fan"}]}
            if path == "parent/replies":
                return {
                    "data": [
                        {
                            "id": "reply",
                            "text": "Вътрешно изграждане",
                            "username": "other",
                        }
                    ]
                }
            raise AssertionError(path)

        with patch.object(client, "_request", side_effect=request):
            threads = client.list_instagram_comment_threads("media1")
        self.assertEqual(threads[0]["id"], "parent")
        self.assertEqual([item["id"] for item in threads[0]["replies"]], ["reply"])

    def test_outbound_messages_keep_page_messages_since(self) -> None:
        client = MetaClient("token-test")
        since = datetime(2026, 10, 14, tzinfo=timezone.utc)

        def request(method: str, path: str, **kwargs: object) -> dict:
            query = kwargs.get("query")
            after = query.get("after") if isinstance(query, dict) else None
            if path == "page/conversations" and not after:
                return {
                    "data": [
                        {
                            "id": "c1",
                            "updated_time": "2026-10-15T12:00:00+0000",
                            "participants": {
                                "data": [
                                    {"id": "p", "username": "page"},
                                    {"id": "f", "username": "fan"},
                                ]
                            },
                        },
                        {
                            "id": "c2",
                            "updated_time": "2026-10-01T12:00:00+0000",
                            "participants": {
                                "data": [
                                    {"id": "p", "username": "page"},
                                    {"id": "o", "username": "other"},
                                ]
                            },
                        },
                    ]
                }
            if path == "c1/messages":
                return {
                    "data": [
                        {
                            "id": "m1",
                            "message": "Ето",
                            "from": {"id": "p", "username": "page"},
                            "created_time": "2026-10-15T12:05:00+0000",
                        },
                        {
                            "id": "m2",
                            "message": "hi",
                            "from": {"id": "f", "username": "fan"},
                            "created_time": "2026-10-15T12:01:00+0000",
                        },
                        {
                            "id": "m0",
                            "message": "old",
                            "from": {"id": "p", "username": "page"},
                            "created_time": "2026-10-01T12:00:00+0000",
                        },
                    ]
                }
            raise AssertionError(path)

        with patch.object(client, "_request", side_effect=request):
            messages = client.list_instagram_outbound_messages(
                "page",
                own_username="@page",
                since=since,
            )
        self.assertEqual(
            messages,
            [
                {
                    "id": "m1",
                    "text": "Ето",
                    "username": "fan",
                    "timestamp": "2026-10-15T12:05:00+0000",
                }
            ],
        )

    def test_comment_list_includes_replies(self) -> None:
        client = MetaClient("token-test")

        def request(method: str, path: str, **kwargs: object) -> dict:
            if path == "media1/comments":
                return {
                    "data": [{"id": "parent", "text": "nice", "username": "fan"}]
                }
            if path == "parent/replies":
                return {
                    "data": [
                        {
                            "id": "reply",
                            "text": "Вътрешно изграждане",
                            "username": "other",
                        }
                    ]
                }
            raise AssertionError(path)

        with patch.object(client, "_request", side_effect=request):
            comments = client.list_instagram_comments("media1")
        self.assertEqual([item["id"] for item in comments], ["parent", "reply"])

    def test_public_reply_uses_the_replies_edge(self) -> None:
        client = MetaClient("token-test")
        with patch.object(
            client,
            "_request",
            return_value={"id": "r1"},
        ) as request:
            reply_id = client.reply_to_instagram_comment(comment_id="c1", message="thanks")
        self.assertEqual(reply_id, "r1")
        request.assert_called_once_with(
            "POST",
            "c1/replies",
            body={"message": "thanks"},
        )


class RateLimitTests(unittest.TestCase):
    def test_rate_limit_stops_and_saves(self) -> None:
        class _Client:
            def list_instagram_comments(self, media_id: str) -> list[dict]:
                raise MetaError(
                    "rate limit",
                    payload={"code": 4},
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps(_watch_list()), encoding="utf-8")
            code = run_instagram_comment_responses(
                _Client(),  # type: ignore[arg-type]
                page_id="ig1",
                own_username="page",
                config=_config(),
                state_path=state_path,
                apply=True,
                log=lambda _line: None,
            )
            self.assertEqual(code, 1)
            self.assertTrue(state_path.is_file())
