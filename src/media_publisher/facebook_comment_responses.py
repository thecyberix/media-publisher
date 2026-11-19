"""Email Grok Bot when a watched Facebook post gets a keyword comment.

Publishing adds a post that needs a prompt to
``data/facebook_comment_responses.json``. The entry key is the Graph post id.
The value stores ``permalink``, ``title``, ``end_cards``, and ``comments``.
Grok Bot reads that file, types ``comments`` in the Business Suite comment
box when it resubmits the post, and replaces the key and ``permalink`` with
the public post. This scan reads comments on the stored id and mails
``NOTIFY_EMAIL`` once per new keyword comment, then sets ``emailed``. A post
Graph cannot read yet is left for the next run. Grok Bot posts the public
reply and sends the DM from that mail.

The mail subject is ``Facebook keyword comment``. The body is::

    post_id: ...
    post_permalink: ...
    comment_id: ...
    comment_permalink: ...
    end_card: ...
    keyword: ...

    comment:
    ...

    reply:
    ...

    dm:
    ...
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from catalog_parser.playlists import PlaylistConfig, PlaylistRule, comment_response_rules
from media_publisher.instagram_comment_responses import (
    comment_matches_keyword,
    normalize_comment_text,
)
from media_publisher.publishers.meta import (
    MetaClient,
    MetaError,
    is_facebook_object_not_visible,
)

DEFAULT_STATE_RELATIVE_PATH = Path("data") / "facebook_comment_responses.json"
EMAIL_SUBJECT = "Facebook keyword comment"


def load_facebook_response_state(path: Path) -> dict[str, Any]:
    empty = {"posts": {}, "comments": {}}
    if not path.is_file():
        return empty
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return empty
    posts = payload.get("posts")
    comments = payload.get("comments")
    return {
        "posts": posts if isinstance(posts, dict) else {},
        "comments": comments if isinstance(comments, dict) else {},
    }


def remember_facebook_prompt_post(
    state_path: Path,
    *,
    post_id: str,
    end_cards: Sequence[str],
    comments: Sequence[str],
    permalink: str = "",
    title: str = "",
) -> None:
    """Remember a Facebook post whose prompt Grok Bot reads and types.

    New end cards and prompt texts are added. Grok Bot later replaces this
    post id with the public post id.
    """
    target = post_id.strip()
    cards = [card.strip() for card in end_cards if isinstance(card, str) and card.strip()]
    prompts = [text.strip() for text in comments if isinstance(text, str) and text.strip()]
    if not target or not cards or not prompts:
        return
    state = load_facebook_response_state(state_path)
    posts: dict[str, Any] = state["posts"]
    current = posts.get(target)
    if not isinstance(current, dict):
        current = {}
    merged_cards: list[str] = []
    for card in list(current.get("end_cards") or []) + cards:
        if isinstance(card, str) and card not in merged_cards:
            merged_cards.append(card)
    merged_comments: list[str] = []
    for text in list(current.get("comments") or []) + prompts:
        if isinstance(text, str) and text not in merged_comments:
            merged_comments.append(text)
    stored_permalink = current.get("permalink")
    stored_title = current.get("title")
    posts[target] = {
        "permalink": permalink.strip()
        or (stored_permalink.strip() if isinstance(stored_permalink, str) else ""),
        "title": title.strip() or (stored_title.strip() if isinstance(stored_title, str) else ""),
        "end_cards": merged_cards,
        "comments": merged_comments,
    }
    save_facebook_response_state(state_path, state)


def save_facebook_response_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def facebook_keyword_email(
    *,
    post_id: str,
    post_permalink: str,
    comment_id: str,
    comment_permalink: str,
    end_card: str,
    keyword: str,
    comment: str,
    reply: str,
    dm: str,
) -> tuple[str, str]:
    """Subject and body for one keyword comment."""
    body = "\n".join(
        [
            f"post_id: {post_id}",
            f"post_permalink: {post_permalink}",
            f"comment_id: {comment_id}",
            f"comment_permalink: {comment_permalink}",
            f"end_card: {end_card}",
            f"keyword: {keyword}",
            "",
            "comment:",
            comment,
            "",
            "reply:",
            reply,
            "",
            "dm:",
            dm,
            "",
        ]
    )
    return EMAIL_SUBJECT, body


def _is_prompt_or_reply(text: str, rule: PlaylistRule) -> bool:
    normalized = normalize_comment_text(text)
    for sample in (rule.comment, rule.reply):
        if sample and normalized == normalize_comment_text(sample):
            return True
    return False


def _already_handled(record: dict[str, Any] | None) -> bool:
    stored = record or {}
    return bool(stored.get("emailed") or stored.get("reply"))


def _comment_permalink(client: MetaClient, raw: dict[str, Any]) -> str:
    permalink = raw.get("permalink_url")
    if isinstance(permalink, str) and permalink.strip():
        return permalink.strip()
    comment_id = raw.get("id")
    if not isinstance(comment_id, str) or not comment_id.strip():
        return ""
    try:
        detail = client._request(
            "GET",
            comment_id.strip(),
            query={"fields": "permalink_url"},
        )
    except MetaError:
        return ""
    if not isinstance(detail, dict):
        return ""
    found = detail.get("permalink_url")
    return found.strip() if isinstance(found, str) else ""


def _notify_recipients() -> list[str]:
    recipients: list[str] = []
    for part in os.getenv("NOTIFY_EMAIL", "").replace(";", ",").split(","):
        text = part.strip()
        if text and text not in recipients:
            recipients.append(text)
    return recipients


def _default_send(subject: str, body: str, recipients: Sequence[str]) -> None:
    from pathlib import Path
    import sys

    scripts_dir = Path(__file__).resolve().parents[2] / "scripts" / "catalog"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from send_notification_email import send_email

    smtp_user = os.getenv("GMAIL_SMTP_USER", "").strip()
    smtp_password = os.getenv("GMAIL_SMTP_APP_PASSWORD", "").strip()
    if not smtp_user or not smtp_password:
        raise RuntimeError("GMAIL_SMTP_USER and GMAIL_SMTP_APP_PASSWORD are required")
    for recipient in recipients:
        send_email(
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            to_address=recipient,
            subject=subject,
            body=body,
        )


def run_facebook_comment_responses(
    client: MetaClient,
    *,
    config: PlaylistConfig,
    state_path: Path,
    apply: bool,
    limit: int = 0,
    log: Callable[[str], None] = print,
    send: Callable[[str, str, Sequence[str]], None] | None = None,
) -> int:
    """Mail new keyword comments. Returns 1 when a read or send fails."""
    rules = comment_response_rules(config)
    if not rules:
        log("No playlist has a keyword reply or DM.")
        return 0

    state = load_facebook_response_state(state_path)
    posts = list(state["posts"].items())
    if limit > 0:
        posts = posts[:limit]
    comments: dict[str, Any] = state["comments"]
    recipients = _notify_recipients()
    sender = send or _default_send

    def safe_log(message: str) -> None:
        log(message.encode("ascii", "backslashreplace").decode("ascii"))

    planned = 0
    sent = 0
    failures = 0
    safe_log(f"{'apply' if apply else 'dry-run'}: {len(posts)} posts")

    for post_id, post in posts:
        if not isinstance(post_id, str) or not post_id or not isinstance(post, dict):
            continue
        end_cards = post.get("end_cards")
        if not isinstance(end_cards, list):
            continue
        matched = [rule for rule in rules if rule.end_card in end_cards]
        if not matched:
            continue
        post_permalink = post.get("permalink")
        post_link = post_permalink.strip() if isinstance(post_permalink, str) else ""
        try:
            raw_comments = client.list_facebook_comments(post_id)
        except MetaError as exc:
            if is_facebook_object_not_visible(exc):
                safe_log(f"comments not visible yet post={post_id}")
                continue
            safe_log(f"comments failed post={post_id} {exc.__class__.__name__}")
            failures += 1
            continue
        for raw in raw_comments:
            message = raw.get("message")
            comment_id = raw.get("id")
            if not isinstance(message, str) or not message.strip():
                continue
            if not isinstance(comment_id, str) or not comment_id.strip():
                continue
            rule = next(
                (
                    item
                    for item in matched
                    if item.keyword
                    and comment_matches_keyword(message, item.keyword)
                    and not _is_prompt_or_reply(message, item)
                ),
                None,
            )
            if rule is None:
                continue
            existing = comments.get(comment_id)
            if isinstance(existing, dict) and _already_handled(existing):
                continue
            planned += 1
            permalink = _comment_permalink(client, raw)
            subject, body = facebook_keyword_email(
                post_id=post_id,
                post_permalink=post_link,
                comment_id=comment_id,
                comment_permalink=permalink,
                end_card=rule.end_card or rule.name,
                keyword=rule.keyword or "",
                comment=message.strip(),
                reply=rule.reply or "",
                dm=rule.dm or "",
            )
            safe_log(f"comment={comment_id} post={post_id} end_card={rule.end_card}")
            if not apply:
                continue
            if not recipients:
                safe_log("NOTIFY_EMAIL is unset")
                failures += 1
                continue
            try:
                sender(subject, body, recipients)
            except Exception as exc:  # noqa: BLE001 — keep scanning after a send failure
                safe_log(f"email failed comment={comment_id} {exc.__class__.__name__}")
                failures += 1
                continue
            record = dict(existing) if isinstance(existing, dict) else {}
            record.update(
                {
                    "post_id": post_id,
                    "end_card": rule.end_card or rule.name,
                    "permalink": permalink,
                    "message": message.strip(),
                    "emailed": True,
                }
            )
            comments[comment_id] = record
            sent += 1
            save_facebook_response_state(state_path, state)
    safe_log(f"done planned={planned} sent={sent} failures={failures}")
    return 1 if failures else 0
