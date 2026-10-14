"""YouTube playlist names stored on the Airtable Playlists field.

Names and when they apply live in ``config/playlists.json``. A rule with no
``end_card`` is added to every video. A rule with ``end_card`` is added when
that ending is found on the reference video. ``quote_playlist`` is the title
of the playlist that receives quote videos only.
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


@dataclass(frozen=True)
class PlaylistConfig:
    rules: tuple[PlaylistRule, ...]
    quote_playlist: str | None = None


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
    raw_rules = payload.get("playlists")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise RuntimeError(f"{config_path.as_posix()} playlists must be a non-empty array")

    rules: list[PlaylistRule] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_rules, start=1):
        rules.append(_parse_rule(item, index=index, source=config_path.as_posix()))
        if rules[-1].name in seen:
            raise RuntimeError(
                f"{config_path.as_posix()} playlists[{index}] duplicates {rules[-1].name!r}"
            )
        seen.add(rules[-1].name)
    if not any(rule.end_card is None for rule in rules):
        raise RuntimeError(
            f"{config_path.as_posix()} needs a playlist with no end_card "
            "so every video is assigned one"
        )
    return PlaylistConfig(
        rules=tuple(rules),
        quote_playlist=_parse_quote_playlist(payload, source=config_path.as_posix()),
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


def combined_media_airtable_fields(
    drive_url: str,
    playlists: Sequence[str],
) -> dict[str, Any]:
    """Combined Media File and Playlists for a single Airtable update."""
    fields: dict[str, Any] = {FIELD_COMBINED_MEDIA_FILE: drive_url}
    if playlists:
        fields[FIELD_PLAYLISTS] = list(playlists)
    return fields


def _parse_quote_playlist(payload: dict[str, Any], *, source: str) -> str | None:
    """Title of the playlist that receives quote videos only.

    Omitted means quotes are not added to a quote playlist. A present value
    must be a non-empty title. It is not one of the video rules above.
    """
    if "quote_playlist" not in payload:
        return None
    value = payload.get("quote_playlist")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{source} quote_playlist must be a non-empty string")
    return value.strip()


def _parse_rule(item: object, *, index: int, source: str) -> PlaylistRule:
    label = f"{source} playlists[{index}]"
    if not isinstance(item, dict):
        raise RuntimeError(f"{label} must be an object")
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RuntimeError(f"{label} name is required")
    end_card = _parse_end_card(item.get("end_card"), label=label)
    comment = _parse_comment(item.get("comment"), label=label, provided="comment" in item)
    return PlaylistRule(name=name.strip(), end_card=end_card, comment=comment)


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


def _parse_comment(value: object, *, label: str, provided: bool) -> str | None:
    if not provided or value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label} comment must be a non-empty string")
    return value.strip()
