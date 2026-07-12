from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from catalog_parser.airtable import (
    YT_DESCRIPTION_COMMENT_PREFIX,
    YT_TITLE_COMMENT_PREFIX,
    resolve_original_video_name,
)

EDITED_TITLE_PREFIX = "Редактирано заглавие:"
TRANSLATED_TITLE_PREFIX = "Преведено заглавие:"
EDITED_DESCRIPTION_PREFIX = "Редактирано описание:"
TRANSLATED_DESCRIPTION_PREFIX = "Преведено описание:"

CONTENT_PREFIXES = (
    YT_TITLE_COMMENT_PREFIX.casefold(),
    "съкратено заглавие:",
    YT_DESCRIPTION_COMMENT_PREFIX.casefold(),
    EDITED_TITLE_PREFIX.casefold(),
    TRANSLATED_TITLE_PREFIX.casefold(),
    EDITED_DESCRIPTION_PREFIX.casefold(),
    TRANSLATED_DESCRIPTION_PREFIX.casefold(),
    "редакция на заглавието:",
    "редакция на загланието:",
)

TRANSLATOR_PATTERNS = (
    r"^преводът е готов\.?$",
    r"^translation ready\.?$",
    r"^translated\.?$",
    r"^готово\.?$",
    r"^готов\.?$",
    r"^преведено\.?$",
    r"^преведен\.?$",
    r"^готова\.?$",
    r"^готови\.?$",
)

EDITOR_PATTERNS = (
    r"^редактирано е\.?$",
    r"^редактирано\.?$",
    r"^редактиран\.?$",
    r"^редактирана\.?$",
    r"^editing done\.?$",
    r"^edited\.?$",
)


@dataclass(frozen=True)
class ReadinessState:
    translator_ready: bool
    editor_ready: bool
    translator_comment: str | None = None
    editor_comment: str | None = None


@dataclass(frozen=True)
class TranslatedContentFromComments:
    video_name_translated: str | None = None
    video_description_translated: str | None = None


def normalize_comment(text: str) -> str:
    return " ".join(text.strip().split())


def is_content_note(text: str) -> bool:
    lowered = text.strip().casefold()
    return any(lowered.startswith(prefix) for prefix in CONTENT_PREFIXES)


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    lowered = normalize_comment(text).casefold()
    return any(re.fullmatch(pattern, lowered) for pattern in patterns)


def comment_indicates_translator_ready(text: str) -> bool:
    if is_content_note(text):
        return False
    if _matches_any(text, TRANSLATOR_PATTERNS):
        return True
    lowered = normalize_comment(text).casefold()
    if len(lowered) > 60 or "\n" in lowered:
        return False
    return bool(
        re.search(r"\b(преведен|преведено|готов|готово|готова|готови)\b", lowered)
        and not re.search(r"\b(редактиран|редактирано|редактирана)\b", lowered)
    )


def comment_indicates_editor_ready(text: str) -> bool:
    if is_content_note(text):
        return False
    if _matches_any(text, EDITOR_PATTERNS):
        return True
    lowered = normalize_comment(text).casefold()
    if len(lowered) > 60 or "\n" in lowered:
        return False
    return bool(re.search(r"\b(редактиран|редактирано|редактирана)\b", lowered))


def comment_has_description(text: str) -> bool:
    return text.strip().startswith(YT_DESCRIPTION_COMMENT_PREFIX)


def extract_value_after_prefix(text: str, prefix: str) -> str | None:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return None

    lines = normalized.split("\n")
    first_line = lines[0].strip()
    if not first_line.casefold().startswith(prefix.casefold()):
        return None

    inline_value = first_line[len(prefix) :].strip()
    following_lines = [line.rstrip() for line in lines[1:]]
    if inline_value:
        value_lines = [inline_value, *following_lines]
    else:
        value_lines = following_lines

    value = "\n".join(line for line in value_lines if line.strip()).strip()
    return value or None


def _latest_comment_value_for_prefix(
    comments: list[dict[str, Any]],
    prefix: str,
) -> str | None:
    latest: str | None = None
    ordered = sorted(comments, key=lambda comment: str(comment.get("createdTime", "")))
    for comment in ordered:
        text = comment.get("text")
        if not isinstance(text, str):
            continue
        extracted = extract_value_after_prefix(text, prefix)
        if extracted:
            latest = extracted
    return latest


def _value_from_comment_prefixes(
    comments: list[dict[str, Any]],
    *,
    primary_prefix: str,
    fallback_prefix: str,
) -> str | None:
    value = _latest_comment_value_for_prefix(comments, primary_prefix)
    if value:
        return value
    return _latest_comment_value_for_prefix(comments, fallback_prefix)


@dataclass(frozen=True)
class OriginalContentFromComments:
    original_video_name: str | None = None
    original_video_description: str | None = None


def extract_original_content_from_comments(
    comments: list[dict[str, Any]],
    *,
    title_fallback: Any = None,
) -> OriginalContentFromComments:
    return OriginalContentFromComments(
        original_video_name=resolve_original_video_name(
            yt_title=_latest_comment_value_for_prefix(comments, YT_TITLE_COMMENT_PREFIX),
            title=title_fallback,
        ),
        original_video_description=_latest_comment_value_for_prefix(
            comments,
            YT_DESCRIPTION_COMMENT_PREFIX,
        ),
    )


def extract_translated_content_from_comments(
    comments: list[dict[str, Any]],
) -> TranslatedContentFromComments:
    return TranslatedContentFromComments(
        video_name_translated=_value_from_comment_prefixes(
            comments,
            primary_prefix=EDITED_TITLE_PREFIX,
            fallback_prefix=TRANSLATED_TITLE_PREFIX,
        ),
        video_description_translated=_value_from_comment_prefixes(
            comments,
            primary_prefix=EDITED_DESCRIPTION_PREFIX,
            fallback_prefix=TRANSLATED_DESCRIPTION_PREFIX,
        ),
    )


def analyze_readiness(comments: list[dict[str, Any]]) -> ReadinessState:
    translator_ready = False
    editor_ready = False
    translator_comment: str | None = None
    editor_comment: str | None = None

    for comment in comments:
        text = comment.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        if comment_indicates_translator_ready(text):
            translator_ready = True
            translator_comment = text.strip()
        if comment_indicates_editor_ready(text):
            editor_ready = True
            editor_comment = text.strip()

    return ReadinessState(
        translator_ready=translator_ready,
        editor_ready=editor_ready,
        translator_comment=translator_comment,
        editor_comment=editor_comment,
    )


def comments_have_description(comments: list[dict[str, Any]]) -> bool:
    for comment in comments:
        text = comment.get("text")
        if isinstance(text, str) and comment_has_description(text):
            return True
    return False
