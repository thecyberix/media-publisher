"""Reply and DM when someone comments the keyword on an IE or MoM video.

Only videos already on the watch list are checked. That list is the videos
whose playlist comment was posted (Inner Engineering or Miracle of Mind).
Each publish that posts one of those comments adds the video. Keyword
comments get ``reply`` (public) and ``dm`` (private reply), including when
the keyword is written as a reply under another comment. The account's own
comments are skipped. State is ``data/instagram_comment_responses.json``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from catalog_parser.playlists import PlaylistConfig, PlaylistRule, comment_response_rules
from media_publisher.publishers.meta import MetaClient, MetaError

DEFAULT_STATE_RELATIVE_PATH = Path("data") / "instagram_comment_responses.json"
DEFAULT_COMMENT_REPORT = Path("output") / "instagram-end-card-comments.json"
DM_WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class InstagramComment:
    comment_id: str
    text: str
    username: str = ""
    timestamp: datetime | None = None


@dataclass(frozen=True)
class CommentResponse:
    comment: InstagramComment
    rule: PlaylistRule
    send_reply: bool
    send_dm: bool
    skip_dm_window: bool = False


def normalize_comment_text(text: str) -> str:
    return " ".join(text.casefold().split())


def comment_matches_keyword(text: str, keyword: str) -> bool:
    needle = normalize_comment_text(keyword)
    if not needle:
        return False
    return needle in normalize_comment_text(text)


def parse_instagram_comment(raw: dict[str, Any]) -> InstagramComment | None:
    comment_id = raw.get("id")
    text = raw.get("text")
    if not isinstance(comment_id, str) or not comment_id.strip():
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    username = raw.get("username")
    return InstagramComment(
        comment_id=comment_id.strip(),
        text=text.strip(),
        username=username.strip() if isinstance(username, str) else "",
        timestamp=_parse_timestamp(raw.get("timestamp")),
    )


def response_is_complete(record: dict[str, Any] | None, rule: PlaylistRule) -> bool:
    stored = record or {}
    if rule.reply and not _filled(stored.get("reply_id")):
        return False
    if rule.dm and not _filled(stored.get("dm_id")) and not stored.get("dm_skipped"):
        return False
    return bool(rule.reply or rule.dm)


def pending_responses(
    comments: Sequence[InstagramComment],
    rules: Sequence[PlaylistRule],
    *,
    own_username: str,
    handled: dict[str, dict[str, Any]],
    now: datetime | None = None,
) -> list[CommentResponse]:
    """Keyword comments that still need a reply, a DM, or a closed-window mark."""
    moment = now or datetime.now(timezone.utc)
    own = own_username.strip().lstrip("@").casefold()
    pending: list[CommentResponse] = []
    for rule in rules:
        if not rule.keyword or not (rule.reply or rule.dm):
            continue
        keyword = rule.keyword
        cta = normalize_comment_text(rule.comment or "")
        for comment in comments:
            if not comment_matches_keyword(comment.text, keyword):
                continue
            if cta and normalize_comment_text(comment.text) == cta:
                continue
            author = comment.username.strip().lstrip("@").casefold()
            if own and author == own:
                continue
            record = handled.get(comment.comment_id) or {}
            if response_is_complete(record, rule):
                continue
            send_reply = bool(rule.reply) and not _filled(record.get("reply_id"))
            dm_open = _within_dm_window(comment, now=moment)
            send_dm = (
                bool(rule.dm)
                and dm_open
                and not _filled(record.get("dm_id"))
                and not record.get("dm_skipped")
            )
            skip_dm_window = (
                bool(rule.dm)
                and not dm_open
                and not _filled(record.get("dm_id"))
                and not record.get("dm_skipped")
            )
            if not send_reply and not send_dm and not skip_dm_window:
                continue
            pending.append(
                CommentResponse(
                    comment=comment,
                    rule=rule,
                    send_reply=send_reply,
                    send_dm=send_dm,
                    skip_dm_window=skip_dm_window,
                )
            )
    return pending


@dataclass(frozen=True)
class CommentThread:
    parent: InstagramComment
    replies: tuple[InstagramComment, ...]


@dataclass(frozen=True)
class SentDirectMessage:
    message_id: str
    username: str
    text: str
    timestamp: datetime | None = None


def comment_thread_from_api(raw: dict[str, Any]) -> CommentThread | None:
    parent = parse_instagram_comment(raw)
    if parent is None:
        return None
    replies: list[InstagramComment] = []
    nested = raw.get("replies")
    if isinstance(nested, list):
        for item in nested:
            if not isinstance(item, dict):
                continue
            parsed = parse_instagram_comment(item)
            if parsed is not None:
                replies.append(parsed)
    return CommentThread(parent=parent, replies=tuple(replies))


def sent_message_from_api(raw: dict[str, Any]) -> SentDirectMessage | None:
    message_id = raw.get("id")
    text = raw.get("text")
    username = raw.get("username")
    if not isinstance(message_id, str) or not message_id.strip():
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    if not isinstance(username, str) or not username.strip():
        return None
    timestamp = raw.get("timestamp")
    parsed = timestamp if isinstance(timestamp, datetime) else _parse_timestamp(timestamp)
    return SentDirectMessage(
        message_id=message_id.strip(),
        username=username.strip(),
        text=text,
        timestamp=parsed,
    )


def public_reply_ids(
    thread: CommentThread,
    rules: Sequence[PlaylistRule],
    *,
    own_username: str,
) -> dict[str, tuple[PlaylistRule, str]]:
    """Keyword comments in one thread that already have this account's reply.

    Instagram keeps replies on the top-level comment, so a reply to a keyword
    comment and the keyword comment itself are siblings. Each keyword comment
    is paired with the earliest later reply that uses that rule's reply text.
    """
    found: dict[str, tuple[PlaylistRule, str]] = {}
    own = _account_name(own_username)
    for rule in rules:
        if not rule.keyword or not rule.reply:
            continue
        dated = [
            item
            for item in (thread.parent, *thread.replies)
            if item.timestamp is not None
        ]
        dated.sort(key=lambda item: (item.timestamp, item.comment_id))
        keywords = [item for item in dated if _is_foreign_keyword(item, rule, own)]
        replies = [item for item in dated if _is_our_reply(item, rule, own)]
        reply_index = 0
        for comment in keywords:
            while (
                reply_index < len(replies)
                and replies[reply_index].timestamp < comment.timestamp
            ):
                reply_index += 1
            if reply_index >= len(replies):
                break
            found.setdefault(comment.comment_id, (rule, replies[reply_index].comment_id))
            reply_index += 1
    return found


def direct_message_ids(
    comments: Sequence[tuple[InstagramComment, PlaylistRule]],
    messages: Sequence[SentDirectMessage],
) -> dict[str, str]:
    """Pair each keyword comment with the earliest later DM of that rule's text."""
    buckets: dict[tuple[str, str], list[SentDirectMessage]] = {}
    for message in messages:
        if message.timestamp is None or not message.message_id.strip():
            continue
        text = normalize_comment_text(message.text)
        username = _account_name(message.username)
        if not text or not username:
            continue
        buckets.setdefault((username, text), []).append(message)
    for bucket in buckets.values():
        bucket.sort(key=lambda item: (item.timestamp, item.message_id))

    pending = [
        item
        for item in comments
        if item[1].dm and item[0].timestamp is not None and item[0].username.strip()
    ]
    pending.sort(key=lambda item: (item[0].timestamp, item[0].comment_id))
    found: dict[str, str] = {}
    used: set[str] = set()
    for comment, rule in pending:
        if not rule.dm:
            continue
        key = (_account_name(comment.username), normalize_comment_text(rule.dm))
        for message in buckets.get(key, ()):
            if message.message_id in used or message.timestamp is None:
                continue
            if message.timestamp < comment.timestamp:
                continue
            found[comment.comment_id] = message.message_id
            used.add(message.message_id)
            break
    return found


def dm_window_closed(exc: BaseException) -> bool:
    detail = str(exc).casefold()
    return any(
        phrase in detail
        for phrase in (
            "private reply",
            "outside of allowed window",
            "outside the allowed window",
            "comment is too old",
            "7 day",
            "7-day",
        )
    )


def rate_limited(exc: BaseException) -> bool:
    detail = str(exc).casefold()
    if "rate limit" in detail or "too many calls" in detail or "request limit" in detail:
        return True
    if isinstance(exc, MetaError):
        code = (exc.payload or {}).get("code")
        return code in {4, 17, 32, 613}
    return False


def load_response_state(path: Path) -> dict[str, Any]:
    empty = {"videos": {}, "comments": {}}
    if not path.is_file():
        return empty
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return empty
    comments = payload.get("comments")
    return {
        "videos": _videos_from_state(payload.get("videos")),
        "comments": comments if isinstance(comments, dict) else {},
    }


def commented_videos_from_report(
    payload: dict[str, Any],
    rules: Sequence[PlaylistRule],
) -> list[dict[str, Any]]:
    """Videos in an end-card comment report that already have an IE or MoM comment.

    ``posted`` and ``already`` are the end-card keys whose comment is on the
    video. ``pending`` means the comment was not posted, so those are left out.
    """
    wanted = {rule.end_card for rule in rules if rule.end_card and rule.comment}
    rows = payload.get("videos")
    if not isinstance(rows, list):
        return []
    found: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        media_id = row.get("media_id")
        if not isinstance(media_id, str) or not media_id.strip():
            continue
        cards: list[str] = []
        for key in ("posted", "already"):
            values = row.get(key)
            if not isinstance(values, list):
                continue
            for value in values:
                if isinstance(value, str) and value in wanted and value not in cards:
                    cards.append(value)
        if not cards:
            continue
        permalink = row.get("permalink")
        found.append(
            {
                "media_id": media_id.strip(),
                "permalink": permalink.strip() if isinstance(permalink, str) else "",
                "end_cards": cards,
            }
        )
    return found


def remember_commented_video(
    state_path: Path,
    *,
    media_id: str,
    end_cards: Sequence[str],
    permalink: str = "",
) -> None:
    """Add a video that received an IE or MoM comment, or add another end card."""
    video_id = media_id.strip()
    cards = [card for card in end_cards if isinstance(card, str) and card.strip()]
    if not video_id or not cards:
        return
    state = load_response_state(state_path)
    _merge_video(
        state["videos"],
        media_id=video_id,
        end_cards=cards,
        permalink=permalink,
    )
    save_response_state(state_path, state)


def absorb_comment_report(
    state: dict[str, Any],
    report_path: Path,
    rules: Sequence[PlaylistRule],
) -> int:
    """Merge a local end-card comment report into ``state``. Returns new videos."""
    if not report_path.is_file():
        return 0
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return 0
    added = 0
    videos = state["videos"]
    for video in commented_videos_from_report(payload, rules):
        if video["media_id"] not in videos:
            added += 1
        _merge_video(
            videos,
            media_id=video["media_id"],
            end_cards=video["end_cards"],
            permalink=video["permalink"],
        )
    return added


def save_response_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def run_instagram_comment_responses(
    client: MetaClient,
    *,
    page_id: str,
    own_username: str,
    config: PlaylistConfig,
    state_path: Path,
    apply: bool,
    limit: int = 0,
    report_path: Path | None = None,
    now: datetime | None = None,
    log: Callable[[str], None] = print,
) -> int:
    """Check listed IE/MoM videos and answer keyword comments.

    Returns 0 when the scan finishes. Returns 1 when Instagram rate-limits the
    run or a send fails for another reason. Already-sent replies and DMs stay
    recorded so the next run continues.
    """
    rules = comment_response_rules(config)
    if not rules:
        log("No playlist has a keyword reply or DM.")
        return 0

    moment = now or datetime.now(timezone.utc)
    state = load_response_state(state_path)
    if report_path is not None:
        absorb_comment_report(state, report_path, rules)
    videos = list(state["videos"].items())
    if limit > 0:
        videos = videos[:limit]
    handled: dict[str, Any] = state["comments"]

    planned = 0
    sent = 0
    failures = 0
    log(f"{'apply' if apply else 'dry-run'}: {len(videos)} videos")
    for media_id, video in videos:
        if not isinstance(media_id, str) or not media_id:
            continue
        if not isinstance(video, dict):
            continue
        end_cards = video.get("end_cards")
        if not isinstance(end_cards, list):
            continue
        matched = [rule for rule in rules if rule.end_card in end_cards]
        if not matched:
            continue
        try:
            raw_comments = client.list_instagram_comments(media_id)
        except MetaError as exc:
            if rate_limited(exc):
                if apply:
                    _store(state_path, state, handled)
                log("Stopped: Instagram rate limit reached. Re-run after it resets.")
                return 1
            log(f"comments failed media={media_id}")
            failures += 1
            continue
        comments = [
            parsed
            for raw in raw_comments
            if (parsed := parse_instagram_comment(raw)) is not None
        ]
        actions = pending_responses(
            comments,
            matched,
            own_username=own_username,
            handled=handled,
            now=moment,
        )
        for action in actions:
            end_card = action.rule.end_card or action.rule.name
            comment_id = action.comment.comment_id
            record = dict(handled.get(comment_id) or {})
            record["media_id"] = media_id
            record["end_card"] = end_card
            if action.skip_dm_window:
                record["dm_skipped"] = "window"
                log(f"dm skipped comment={comment_id} reason=window")
            if not action.send_reply and not action.send_dm:
                handled[comment_id] = record
                if apply:
                    _store(state_path, state, handled)
                continue
            planned += 1
            log(
                f"comment={comment_id} media={media_id} end_card={end_card} "
                f"reply={'yes' if action.send_reply else 'no'} "
                f"dm={'yes' if action.send_dm else 'no'}"
            )
            if not apply:
                continue
            try:
                if action.send_reply and action.rule.reply:
                    record["reply_id"] = client.reply_to_instagram_comment(
                        comment_id=comment_id,
                        message=action.rule.reply,
                    )
                    sent += 1
                if action.send_dm and action.rule.dm:
                    try:
                        record["dm_id"] = client.send_instagram_comment_dm(
                            page_id=page_id,
                            comment_id=comment_id,
                            message=action.rule.dm,
                        )
                        sent += 1
                    except MetaError as exc:
                        if dm_window_closed(exc):
                            record["dm_skipped"] = "window"
                            log(f"dm skipped comment={comment_id} reason=window")
                        else:
                            raise
                handled[comment_id] = record
                _store(state_path, state, handled)
            except MetaError as exc:
                handled[comment_id] = record
                _store(state_path, state, handled)
                if rate_limited(exc):
                    log("Stopped: Instagram rate limit reached. Re-run after it resets.")
                    return 1
                code = (exc.payload or {}).get("code")
                detail = str(exc).encode("ascii", "backslashreplace").decode("ascii")
                log(f"send failed comment={comment_id} code={code} {detail[:300]}")
                failures += 1
    if apply:
        _store(state_path, state, handled)
    log(f"done planned={planned} sent={sent} failures={failures}")
    return 1 if failures else 0


def backfill_instagram_comment_responses(
    client: MetaClient,
    *,
    page_id: str,
    own_username: str,
    config: PlaylistConfig,
    state_path: Path,
    apply: bool,
    limit: int = 0,
    report_path: Path | None = None,
    now: datetime | None = None,
    log: Callable[[str], None] = print,
) -> int:
    """Record replies and DMs already present on Instagram.

    A public reply counts when this account already posted the configured reply
    text on that keyword comment's thread. A DM counts when the configured DM
    text is already in that person's Instagram conversation. Comments older
    than the DM window that already have a public reply are marked skipped.
    Nothing is sent. Returns 1 when Instagram rate-limits the run or the inbox
    cannot be read.
    """
    rules = comment_response_rules(config)
    if not rules:
        log("No playlist has a keyword reply or DM.")
        return 0

    moment = now or datetime.now(timezone.utc)
    state = load_response_state(state_path)
    if report_path is not None:
        absorb_comment_report(state, report_path, rules)
    videos = list(state["videos"].items())
    if limit > 0:
        videos = videos[:limit]
    recorded = {
        key: dict(value)
        for key, value in state["comments"].items()
        if isinstance(key, str) and isinstance(value, dict)
    }

    observed: dict[str, tuple[InstagramComment, PlaylistRule, str]] = {}
    public: dict[str, tuple[PlaylistRule, str]] = {}
    scanned = 0
    stopped = False
    log(f"{'apply' if apply else 'dry-run'}: {len(videos)} videos")
    for media_id, video in videos:
        if not isinstance(media_id, str) or not media_id or not isinstance(video, dict):
            continue
        end_cards = video.get("end_cards")
        if not isinstance(end_cards, list):
            continue
        matched = [rule for rule in rules if rule.end_card in end_cards]
        if not matched:
            continue
        try:
            raw_threads = client.list_instagram_comment_threads(media_id)
        except MetaError as exc:
            if rate_limited(exc):
                stopped = True
                log("Stopped: Instagram rate limit reached. Re-run after it resets.")
                break
            log(f"comments failed media={media_id}")
            continue
        scanned += 1
        for raw in raw_threads:
            if not isinstance(raw, dict):
                continue
            thread = comment_thread_from_api(raw)
            if thread is None:
                continue
            for comment, rule in _keyword_comments(thread, matched, own_username):
                observed.setdefault(comment.comment_id, (comment, rule, media_id))
            for comment_id, match in public_reply_ids(
                thread,
                matched,
                own_username=own_username,
            ).items():
                public.setdefault(comment_id, match)
                current = observed.get(comment_id)
                if current is not None and current[1] != match[0]:
                    observed[comment_id] = (current[0], match[0], media_id)
        if scanned == 1 or scanned % 25 == 0:
            log(f"scanned {scanned}/{len(videos)} videos")

    replies_added = 0
    for comment_id, (comment, rule, media_id) in observed.items():
        match = public.get(comment_id)
        if match is None:
            continue
        matched_rule, reply_id = match
        if _fill_response_record(
            recorded,
            comment_id=comment_id,
            media_id=media_id,
            end_card=matched_rule.end_card or matched_rule.name,
            reply_id=reply_id,
        ):
            replies_added += 1
            log(f"reply found comment={comment_id} media={media_id} reply={reply_id}")
        rule = matched_rule
        observed[comment_id] = (comment, rule, media_id)

    needs_dm = [
        (comment, rule)
        for comment, rule, _media_id in observed.values()
        if _dm_still_open(recorded.get(comment.comment_id), rule, comment, moment)
    ]
    dms_added = 0
    inbox_failed = False
    if needs_dm and not stopped:
        dated = [comment.timestamp for comment, _rule in needs_dm if comment.timestamp]
        since = min(dated) if dated else moment - DM_WINDOW
        log(f"checking inbox for {len(needs_dm)} comments")
        try:
            raw_messages = client.list_instagram_outbound_messages(
                page_id,
                own_username=own_username,
                since=since,
            )
        except MetaError as exc:
            if rate_limited(exc):
                stopped = True
                log("Stopped: Instagram rate limit reached. Re-run after it resets.")
            else:
                inbox_failed = True
                detail = str(exc).encode("ascii", "backslashreplace").decode("ascii")
                log(f"DM lookup failed: {detail[:300]}")
        else:
            messages = [
                parsed
                for raw in raw_messages
                if isinstance(raw, dict) and (parsed := sent_message_from_api(raw)) is not None
            ]
            found = direct_message_ids(needs_dm, messages)
            for comment, rule, media_id in observed.values():
                message_id = found.get(comment.comment_id)
                if not message_id:
                    continue
                if _fill_response_record(
                    recorded,
                    comment_id=comment.comment_id,
                    media_id=media_id,
                    end_card=rule.end_card or rule.name,
                    dm_id=message_id,
                ):
                    dms_added += 1
                    log(f"dm found comment={comment.comment_id} message={message_id}")

    windows = 0
    for comment, rule, media_id in observed.values():
        record = recorded.get(comment.comment_id)
        if not isinstance(record, dict) or not _filled(record.get("reply_id")):
            continue
        if _within_dm_window(comment, now=moment):
            continue
        if _fill_response_record(
            recorded,
            comment_id=comment.comment_id,
            media_id=media_id,
            end_card=rule.end_card or rule.name,
            dm_skipped="window",
        ):
            windows += 1
            log(f"dm skipped comment={comment.comment_id} reason=window")

    still_open = 0
    unanswered = 0
    for comment, rule, _media_id in observed.values():
        record = recorded.get(comment.comment_id)
        stored = record if isinstance(record, dict) else {}
        if not _filled(stored.get("reply_id")):
            unanswered += 1
            log(
                f"unanswered comment={comment.comment_id} "
                f"media={_media_id} user={comment.username}"
            )
            continue
        if rule.dm and not _filled(stored.get("dm_id")) and not stored.get("dm_skipped"):
            still_open += 1
            log(
                f"dm not found comment={comment.comment_id} "
                f"user={comment.username}"
            )
    if apply:
        state["comments"] = recorded
        _store(state_path, state, recorded)
    log(
        f"done scanned={scanned} replies={replies_added} dms={dms_added} "
        f"dm_window_closed={windows} dm_not_found={still_open} unanswered={unanswered}"
    )
    return 1 if stopped or inbox_failed else 0


def _account_name(username: str) -> str:
    return username.strip().lstrip("@").casefold()


def _is_foreign_keyword(comment: InstagramComment, rule: PlaylistRule, own: str) -> bool:
    if not rule.keyword or not comment_matches_keyword(comment.text, rule.keyword):
        return False
    prompt = normalize_comment_text(rule.comment or "")
    if prompt and normalize_comment_text(comment.text) == prompt:
        return False
    if own and _account_name(comment.username) == own:
        return False
    return True


def _same_message_text(left: str, right: str) -> bool:
    """True when two messages match, allowing one short word to differ.

    Metricool's Miracle of Mind reply says "в лично съобщение" where the
    configured reply says "на лично съобщение".
    """
    a = normalize_comment_text(left).split()
    b = normalize_comment_text(right).split()
    if a == b:
        return True
    if len(a) != len(b):
        return False
    diffs = [(left_word, right_word) for left_word, right_word in zip(a, b) if left_word != right_word]
    return len(diffs) == 1 and len(diffs[0][0]) <= 3 and len(diffs[0][1]) <= 3


def _is_our_reply(comment: InstagramComment, rule: PlaylistRule, own: str) -> bool:
    if not own or not rule.reply:
        return False
    if _account_name(comment.username) != own:
        return False
    return _same_message_text(comment.text, rule.reply)


def _keyword_comments(
    thread: CommentThread,
    rules: Sequence[PlaylistRule],
    own_username: str,
) -> list[tuple[InstagramComment, PlaylistRule]]:
    own = _account_name(own_username)
    found: list[tuple[InstagramComment, PlaylistRule]] = []
    seen: set[str] = set()
    for rule in rules:
        for comment in (thread.parent, *thread.replies):
            if comment.comment_id in seen:
                continue
            if not _is_foreign_keyword(comment, rule, own):
                continue
            seen.add(comment.comment_id)
            found.append((comment, rule))
    return found


def _dm_still_open(
    record: dict[str, Any] | None,
    rule: PlaylistRule,
    comment: InstagramComment,
    now: datetime,
) -> bool:
    if not rule.dm:
        return False
    stored = record or {}
    if _filled(stored.get("dm_id")) or stored.get("dm_skipped"):
        return False
    return _within_dm_window(comment, now=now)


def _fill_response_record(
    handled: dict[str, Any],
    *,
    comment_id: str,
    media_id: str,
    end_card: str,
    reply_id: str = "",
    dm_id: str = "",
    dm_skipped: str = "",
) -> bool:
    current = handled.get(comment_id)
    record = dict(current) if isinstance(current, dict) else {}
    if dm_skipped and (not _filled(record.get("reply_id")) or _filled(record.get("dm_id"))):
        dm_skipped = ""
    changed = False
    for key, value in (
        ("reply_id", reply_id),
        ("dm_id", dm_id),
        ("dm_skipped", dm_skipped),
    ):
        if not value or (key != "dm_skipped" and not _filled(value)):
            continue
        if key == "dm_skipped":
            if record.get("dm_skipped") or _filled(record.get("dm_id")):
                continue
        elif _filled(record.get(key)):
            continue
        record[key] = value
        changed = True
    if not changed:
        return False
    if not _filled(record.get("reply_id")) and not _filled(record.get("dm_id")) and not record.get(
        "dm_skipped"
    ):
        return False
    if media_id and not _filled(record.get("media_id")):
        record["media_id"] = media_id
    if end_card and not _filled(record.get("end_card")):
        record["end_card"] = end_card
    handled[comment_id] = record
    return True


def _filled(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _within_dm_window(comment: InstagramComment, *, now: datetime) -> bool:
    if comment.timestamp is None:
        return True
    return now - comment.timestamp <= DM_WINDOW


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _videos_from_state(raw: object) -> dict[str, dict[str, Any]]:
    if isinstance(raw, dict):
        return {
            key: value
            for key, value in raw.items()
            if isinstance(key, str) and isinstance(value, dict)
        }
    if not isinstance(raw, list):
        return {}
    videos: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        media_id = item.get("media_id")
        if isinstance(media_id, str) and media_id.strip():
            videos[media_id.strip()] = item
    return videos


def _merge_video(
    videos: dict[str, Any],
    *,
    media_id: str,
    end_cards: Sequence[str],
    permalink: str,
) -> None:
    current = videos.get(media_id)
    if not isinstance(current, dict):
        current = {"permalink": "", "end_cards": []}
    cards = [
        card
        for card in current.get("end_cards") or []
        if isinstance(card, str) and card.strip()
    ]
    for card in end_cards:
        cleaned = card.strip()
        if cleaned and cleaned not in cards:
            cards.append(cleaned)
    current["end_cards"] = cards
    if permalink.strip():
        current["permalink"] = permalink.strip()
    elif not isinstance(current.get("permalink"), str):
        current["permalink"] = ""
    videos[media_id] = current


def _store(path: Path, state: dict[str, Any], handled: dict[str, Any]) -> None:
    state["comments"] = handled
    save_response_state(path, state)
