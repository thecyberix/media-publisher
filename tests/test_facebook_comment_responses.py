"""Tests for Facebook keyword comment emails."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from catalog_parser.playlists import PlaylistConfig, PlaylistRule
from media_publisher.facebook_comment_responses import (
    EMAIL_SUBJECT,
    load_facebook_response_state,
    remember_facebook_prompt_post,
    run_facebook_comment_responses,
)
from media_publisher.publishers.facebook import remember_facebook_playlist_post
from media_publisher.publishers.meta import MetaError


PROMPT = 'Напишете "Календар" в коментар за датите на Екадаши.'
REPLY = "Намаскарам! Календарът пътува към Вас."
DM = "Ето календарът."


def _config() -> PlaylistConfig:
    return PlaylistConfig(
        rules=(),
        ekadashi_comment=PlaylistRule(
            "Екадаши",
            "ekadashi",
            comment=PROMPT,
            keyword="Календар",
            reply=REPLY,
            dm=DM,
        ),
    )


def _state(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "posts": {
                    "post1": {
                        "permalink": "https://www.facebook.com/photo.php?fbid=1",
                        "end_cards": ["ekadashi"],
                    }
                },
                "comments": {},
            }
        ),
        encoding="utf-8",
    )


class FacebookKeywordEmailTests(unittest.TestCase):
    def test_apply_emails_a_keyword_once_and_keeps_bot_fields(self) -> None:
        client = MagicMock()
        client.list_facebook_comments.return_value = [
            {"id": "cta", "message": PROMPT, "permalink_url": "https://fb.example/cta"},
            {
                "id": "fan",
                "message": "Календар",
                "permalink_url": "https://www.facebook.com/posts/1?comment_id=fan",
            },
            {"id": "reply", "message": REPLY},
            {"id": "thanks", "message": "Благодаря"},
        ]
        sent: list[tuple[str, str, tuple[str, ...]]] = []

        def send(subject: str, body: str, recipients: list[str]) -> None:
            sent.append((subject, body, tuple(recipients)))

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            _state(state_path)
            with unittest.mock.patch.dict("os.environ", {"NOTIFY_EMAIL": "ops@example.com"}):
                code = run_facebook_comment_responses(
                    client,
                    config=_config(),
                    state_path=state_path,
                    apply=True,
                    send=send,
                )
                again = run_facebook_comment_responses(
                    client,
                    config=_config(),
                    state_path=state_path,
                    apply=True,
                    send=send,
                )
            saved = load_facebook_response_state(state_path)
        self.assertEqual(code, 0)
        self.assertEqual(again, 0)
        self.assertEqual(len(sent), 1)
        subject, body, recipients = sent[0]
        self.assertEqual(subject, EMAIL_SUBJECT)
        self.assertEqual(recipients, ("ops@example.com",))
        self.assertIn("comment_permalink: https://www.facebook.com/posts/1?comment_id=fan", body)
        self.assertIn("keyword: Календар", body)
        self.assertIn(f"reply:\n{REPLY}", body)
        self.assertIn(f"dm:\n{DM}", body)
        self.assertTrue(saved["comments"]["fan"]["emailed"])
        saved["comments"]["fan"]["reply"] = True
        saved["comments"]["fan"]["dm"] = True
        self.assertEqual(saved["comments"]["fan"]["permalink"], "https://www.facebook.com/posts/1?comment_id=fan")

    def test_dry_run_does_not_record_the_email(self) -> None:
        client = MagicMock()
        client.list_facebook_comments.return_value = [{"id": "fan", "message": "календар"}]
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            _state(state_path)
            code = run_facebook_comment_responses(
                client,
                config=_config(),
                state_path=state_path,
                apply=False,
                send=lambda *_args: self.fail("dry run sent mail"),
            )
            saved = load_facebook_response_state(state_path)
        self.assertEqual(code, 0)
        self.assertEqual(saved["comments"], {})

    def test_a_finished_reply_is_not_emailed(self) -> None:
        client = MagicMock()
        client.list_facebook_comments.return_value = [{"id": "fan", "message": "Календар"}]
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            _state(state_path)
            saved = load_facebook_response_state(state_path)
            saved["comments"]["fan"] = {"reply": True, "dm": True}
            state_path.write_text(json.dumps(saved), encoding="utf-8")
            code = run_facebook_comment_responses(
                client,
                config=_config(),
                state_path=state_path,
                apply=True,
                send=lambda *_args: self.fail("finished comment was emailed"),
            )
        self.assertEqual(code, 0)

    def test_send_failure_stays_unemailed(self) -> None:
        client = MagicMock()
        client.list_facebook_comments.return_value = [
            {"id": "fan", "message": "Календар", "permalink_url": "https://fb.example/fan"}
        ]

        def send(*_args: object) -> None:
            raise RuntimeError("smtp down")

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            _state(state_path)
            with unittest.mock.patch.dict("os.environ", {"NOTIFY_EMAIL": "ops@example.com"}):
                code = run_facebook_comment_responses(
                    client,
                    config=_config(),
                    state_path=state_path,
                    apply=True,
                    send=send,
                )
            saved = load_facebook_response_state(state_path)
        self.assertEqual(code, 1)
        self.assertEqual(saved["comments"], {})

    def test_a_hidden_scheduled_post_waits_for_the_next_run(self) -> None:
        client = MagicMock()
        client.list_facebook_comments.side_effect = MetaError(
            "not visible",
            http_status=400,
            payload={"code": 100, "error_subcode": 33},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            remember_facebook_prompt_post(
                state_path,
                post_id="video1",
                end_cards=["ekadashi"],
                comments=[PROMPT],
                permalink="https://www.facebook.com/watch/?v=video1",
                title="Ekadashi",
            )
            code = run_facebook_comment_responses(
                client,
                config=_config(),
                state_path=state_path,
                apply=True,
                send=lambda *_args: self.fail("hidden post was emailed"),
            )
            saved = load_facebook_response_state(state_path)
        self.assertEqual(code, 0)
        self.assertNotIn("needs_comment", saved["posts"]["video1"])
        self.assertEqual(saved["posts"]["video1"]["comments"], [PROMPT])
        self.assertEqual(saved["comments"], {})

    def test_a_transient_graph_error_waits_for_the_next_run(self) -> None:
        client = MagicMock()
        client.list_facebook_comments.side_effect = MetaError(
            "unknown",
            http_status=500,
            payload={
                "code": 1,
                "error_subcode": 99,
                "message": "An unknown error occurred",
            },
        )
        logs: list[str] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            remember_facebook_prompt_post(
                state_path,
                post_id="1862791591802783",
                end_cards=["ekadashi"],
                comments=[PROMPT],
                permalink="https://www.facebook.com/reel/1862791591802783/",
                title="Wellbeing",
            )
            code = run_facebook_comment_responses(
                client,
                config=_config(),
                state_path=state_path,
                apply=True,
                log=logs.append,
                send=lambda *_args: self.fail("transient post was emailed"),
            )
        self.assertEqual(code, 0)
        self.assertTrue(any("comments skipped post=1862791591802783 transient" in line for line in logs))
        self.assertTrue(any("http=500" in line and "code=1" in line and "subcode=99" in line for line in logs))

    def test_a_permanent_graph_error_fails_the_run(self) -> None:
        client = MagicMock()
        client.list_facebook_comments.side_effect = MetaError(
            "token",
            http_status=400,
            payload={"code": 190, "message": "Invalid OAuth access token"},
        )
        logs: list[str] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            _state(state_path)
            code = run_facebook_comment_responses(
                client,
                config=_config(),
                state_path=state_path,
                apply=True,
                log=logs.append,
                send=lambda *_args: self.fail("permanent error was emailed"),
            )
        self.assertEqual(code, 1)
        self.assertTrue(any("comments failed post=post1 http=400 code=190" in line for line in logs))

    def test_playlist_publish_records_the_prompt(self) -> None:
        logs: list[str] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            remember_facebook_playlist_post(
                "page_post",
                {"Playlists": ["Вътрешно Изграждане", "Съзнателна Планета"]},
                permalink="https://www.facebook.com/watch/?v=page_post",
                title="Demo",
                state_path=state_path,
                log=logs.append,
            )
            saved = load_facebook_response_state(state_path)
        post = saved["posts"]["page_post"]
        self.assertEqual(post["end_cards"], ["inner_engineering"])
        self.assertNotIn("needs_comment", post)
        self.assertIn("Вътрешно изграждане", post["comments"][0])
        self.assertEqual(post["title"], "Demo")
        self.assertEqual(logs, ["Facebook watch list: page_post end_cards=inner_engineering"])


if __name__ == "__main__":
    unittest.main()
