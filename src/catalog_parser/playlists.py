"""YouTube playlist names stored on the Airtable Playlists field.

Catalog publish adds the uploaded video to the names stored on that record.
Names and when they apply live in ``config/playlists.json``. A rule with no
``end_card`` is added to every video. A rule with ``end_card`` is added when
that ending is found on the reference video. Either kind can be omitted, and
the ``video_playlists`` array itself can be empty. ``comment`` is the Instagram
comment posted on that video. ``keyword``, ``reply``, and ``dm`` answer a
later comment that contains the keyword. ``quote_playlist`` is the title
of the playlist that receives quote videos only. ``ekadashi_playlist`` is the
title of the playlist that receives Ekadashi videos only. Omit it and the
Ekadashi video is published without a playlist. ``publish_priority``
is an ordered list of playlist names to schedule first. A name matches the
Airtable Playlists field; it does not have to be one of the rules above.
Airtable status
still decides which pool is used; names earlier in the list are scheduled
sooner inside that pool.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from catalog_parser.airtable import FIELD_COMBINED_MEDIA_FILE, FIELD_PLAYLISTS

DEFAULT_PLAYLISTS_PATH = Path("config") / "playlists.json"
END_CARD_INNER_ENGINEERING = "inner_engineering"
END_CARD_SAVE_SOIL = "save_soil"
END_CARD_MIRACLE_OF_MIND = "miracle_of_mind"
KNOWN_END_CARDS = frozenset(
    {
        END_CARD_INNER_ENGINEERING,
        END_CARD_SAVE_SOIL,
        END_CARD_MIRACLE_OF_MIND,
    }
)


@dataclass(frozen=True)
class PlaylistRule:
    name: str
    end_card: str | None = None
    comment: str | None = None
    keyword: str | None = None
    reply: str | None = None
    dm: str | None = None


@dataclass(frozen=True)
class PlaylistConfig:
    rules: tuple[PlaylistRule, ...]
    quote_playlist: str | None = None
    ekadashi_playlist: str | None = None
    publish_priority: tuple[str, ...] = ()


def playlists_config_path(project_root: Path | None = None) -> Path:
    root = project_root or Path(__file__).resolve().parents[2]
    return root / DEFAULT_PLAYLISTS_PATH


def load_playlist_config(path: Path | None = None) -> PlaylistConfig:
    config_path = path or playlists_config_path()
    if not config_path.is_file():
        raise RuntimeError(f"{config_path.as_posix()} is required")
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{config_path.as_posix()} must contain a JSON object")
    raw_rules = payload.get("video_playlists", [])
    if raw_rules is None:
        raw_rules = []
    if not isinstance(raw_rules, list):
        raise RuntimeError(f"{config_path.as_posix()} video_playlists must be an array")

    rules: list[PlaylistRule] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_rules, start=1):
        rules.append(_parse_rule(item, index=index, source=config_path.as_posix()))
        if rules[-1].name in seen:
            raise RuntimeError(
                f"{config_path.as_posix()} video_playlists[{index}] duplicates {rules[-1].name!r}"
            )
        seen.add(rules[-1].name)
    return PlaylistConfig(
        rules=tuple(rules),
        quote_playlist=_parse_optional_playlist_title(
            payload, "quote_playlist", source=config_path.as_posix()
        ),
        ekadashi_playlist=_parse_optional_playlist_title(
            payload, "ekadashi_playlist", source=config_path.as_posix()
        ),
        publish_priority=_parse_publish_priority(
            payload,
            source=config_path.as_posix(),
        ),
    )


def youtube_playlist_ids_for_end_cards(
    rules: Sequence[PlaylistRule],
    titled_playlists: Sequence[tuple[str, str]],
) -> dict[str, str]:
    """Map an end-card key to a YouTube playlist id by exact playlist title.

    ``titled_playlists`` is ``(playlist_id, title)``. Rules with no end card are
    skipped. A missing title raises so a video is not added to the wrong list.
    """
    by_title: dict[str, str] = {}
    for playlist_id, title in titled_playlists:
        by_title.setdefault(title, playlist_id)
    found: dict[str, str] = {}
    missing: list[str] = []
    for rule in rules:
        if not rule.end_card:
            continue
        playlist_id = by_title.get(rule.name)
        if not playlist_id:
            missing.append(rule.name)
            continue
        found[rule.end_card] = playlist_id
    if missing:
        known = ", ".join(sorted(by_title)) or "(none)"
        raise RuntimeError(
            "YouTube playlist title not found for "
            + ", ".join(missing)
            + f". Channel playlists: {known}"
        )
    return found


def playlist_publish_rank(assigned: Sequence[str], priority: Sequence[str]) -> int:
    """Lower ranks are scheduled sooner. Unlisted videos sort after listed ones."""
    assigned_names = {
        name.strip() for name in assigned if isinstance(name, str) and name.strip()
    }
    for index, name in enumerate(priority):
        if name in assigned_names:
            return index
    return len(priority)


def youtube_playlists(
    detected_end_cards: Collection[str] = (),
    *,
    config: PlaylistConfig | None = None,
) -> list[str]:
    """Playlist names for one video, in config order."""
    selected = config or load_playlist_config()
    detected = set(detected_end_cards)
    return [
        rule.name
        for rule in selected.rules
        if rule.end_card is None or rule.end_card in detected
    ]


def instagram_comments_for_playlists(
    playlist_names: Sequence[str],
    *,
    config: PlaylistConfig | None = None,
) -> list[str]:
    """Instagram comments for the playlists assigned to one video, in config order."""
    selected = config or load_playlist_config()
    comments_by_name = {
        rule.name: rule.comment for rule in selected.rules if rule.comment
    }
    comments: list[str] = []
    seen: set[str] = set()
    for name in playlist_names:
        if not isinstance(name, str):
            continue
        comment = comments_by_name.get(name.strip())
        if not comment or comment in seen:
            continue
        seen.add(comment)
        comments.append(comment)
    return comments


def comment_response_rules(
    config: PlaylistConfig | None = None,
) -> list[PlaylistRule]:
    """Playlists that reply to a keyword comment and/or send a DM."""
    selected = config or load_playlist_config()
    return [
        rule
        for rule in selected.rules
        if rule.keyword and (rule.reply or rule.dm)
    ]


def combined_media_airtable_fields(
    drive_url: str,
    playlists: Sequence[str],
) -> dict[str, Any]:
    """Combined Media File and Playlists for a single Airtable update."""
    fields: dict[str, Any] = {FIELD_COMBINED_MEDIA_FILE: drive_url}
    if playlists:
        fields[FIELD_PLAYLISTS] = list(playlists)
    return fields


def _parse_publish_priority(
    payload: dict[str, Any],
    *,
    source: str,
) -> tuple[str, ...]:
    if "publish_priority" not in payload:
        return ()
    raw = payload.get("publish_priority")
    if not isinstance(raw, list):
        raise RuntimeError(f"{source} publish_priority must be an array of playlist names")
    names: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, str) or not item.strip():
            raise RuntimeError(f"{source} publish_priority[{index}] must be a playlist name")
        name = item.strip()
        if name in seen:
            raise RuntimeError(f"{source} publish_priority duplicates {name!r}")
        seen.add(name)
        names.append(name)
    return tuple(names)


def _parse_optional_playlist_title(
    payload: dict[str, Any],
    key: str,
    *,
    source: str,
) -> str | None:
    """Optional playlist title that is not one of the video rules above."""
    if key not in payload or payload.get(key) is None:
        return None
    value = payload.get(key)
    if not isinstance(value, str):
        raise RuntimeError(f"{source} {key} must be a string")
    return value.strip() or None


def _parse_rule(item: object, *, index: int, source: str) -> PlaylistRule:
    label = f"{source} video_playlists[{index}]"
    if not isinstance(item, dict):
        raise RuntimeError(f"{label} must be an object")
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RuntimeError(f"{label} name is required")
    end_card = _parse_end_card(item.get("end_card"), label=label)
    comment = _parse_optional_text(
        item.get("comment"), label=label, field="comment", provided="comment" in item
    )
    keyword = _parse_optional_text(
        item.get("keyword"), label=label, field="keyword", provided="keyword" in item
    )
    reply = _parse_optional_text(
        item.get("reply"), label=label, field="reply", provided="reply" in item
    )
    dm = _parse_optional_text(item.get("dm"), label=label, field="dm", provided="dm" in item)
    if (reply or dm) and not keyword:
        raise RuntimeError(f"{label} keyword is required when reply or dm is set")
    if keyword and not (reply or dm):
        raise RuntimeError(f"{label} reply or dm is required when keyword is set")
    return PlaylistRule(
        name=name.strip(),
        end_card=end_card,
        comment=comment,
        keyword=keyword,
        reply=reply,
        dm=dm,
    )


def _parse_end_card(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label} end_card must be one of {sorted(KNOWN_END_CARDS)}")
    end_card = value.strip()
    if end_card not in KNOWN_END_CARDS:
        raise RuntimeError(
            f"{label} end_card {end_card!r} must be one of {sorted(KNOWN_END_CARDS)}"
        )
    return end_card


def _parse_optional_text(
    value: object, *, label: str, field: str, provided: bool
) -> str | None:
    if not provided or value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label} {field} must be a non-empty string")
    return value.strip()
