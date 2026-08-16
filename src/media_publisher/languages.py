from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


class LanguageConfigError(RuntimeError):
    pass


DEFAULT_LANGUAGES_PATH = "config/languages.json"


@dataclass(frozen=True)
class EventLanguageConfig:
    program_word: str
    benefits_headings: tuple[str, ...]
    quote_attributions: tuple[str, ...]
    learn_more_prefixes: tuple[str, ...]
    city_preposition: str
    city_preposition_before: tuple[tuple[str, str], ...]
    registration_cta: str
    html_registration_label: str
    closing_lines: tuple[str, ...]
    page_heading: str
    empty_state: str
    profile_alt: str


@dataclass(frozen=True)
class IngestLanguageConfig:
    smartcat_language_id: int
    aliases: tuple[str, ...]
    quote_open: str
    quote_close: str
    letter_pattern: str
    title_case_small_words: tuple[str, ...]


@dataclass(frozen=True)
class PublishLanguageConfig:
    display_name: str
    hashtag: str
    youtube_title_pipe_suffix: str
    shorts_description_hashtags: str
    quote_youtube_description_hashtag: str
    learn_more_label: str
    youtube_tags: tuple[str, ...]


@dataclass(frozen=True)
class LanguageDefinition:
    name: str
    alias: str
    country: str
    months: tuple[str, ...]
    date_year_suffix: str = ""
    events: EventLanguageConfig | None = None
    ingest: IngestLanguageConfig | None = None
    publish: PublishLanguageConfig | None = None

    def month_name(self, month: int) -> str:
        if month < 1 or month > len(self.months):
            raise LanguageConfigError(
                f"Unsupported month number {month} for language {self.name!r}"
            )
        return self.months[month - 1]

    def month_number(self, name: str) -> int | None:
        target = name.strip().casefold()
        if not target:
            return None
        for index, month_name in enumerate(self.months, start=1):
            if month_name.casefold() == target:
                return index
        return None

    def require_events(self) -> EventLanguageConfig:
        if self.events is None:
            raise LanguageConfigError(
                f"Language {self.name!r} is missing an 'events' section in "
                f"{DEFAULT_LANGUAGES_PATH}"
            )
        return self.events

    def require_ingest(self) -> IngestLanguageConfig:
        if self.ingest is None:
            raise LanguageConfigError(
                f"Language {self.name!r} is missing an 'ingest' section in "
                f"{DEFAULT_LANGUAGES_PATH}"
            )
        return self.ingest

    def require_publish(self) -> PublishLanguageConfig:
        if self.publish is None:
            raise LanguageConfigError(
                f"Language {self.name!r} is missing a 'publish' section in "
                f"{DEFAULT_LANGUAGES_PATH}"
            )
        return self.publish


def languages_config_path(project_root: Path | None = None) -> Path:
    root = project_root or Path(__file__).resolve().parents[2]
    return root / DEFAULT_LANGUAGES_PATH


def _required_str(
    raw: dict[str, Any], key: str, *, language: str, section: str = "events"
) -> str:
    value = str(raw.get(key) or "").strip()
    if not value:
        raise LanguageConfigError(
            f"Language {language!r} {section} config is missing {key!r}"
        )
    return value


def _str_tuple(
    raw: dict[str, Any],
    key: str,
    *,
    language: str,
    section: str = "events",
    allow_empty: bool = False,
) -> tuple[str, ...]:
    value = raw.get(key)
    if value is None and allow_empty:
        return ()
    if not isinstance(value, list) or (not value and not allow_empty):
        raise LanguageConfigError(
            f"Language {language!r} {section} config must define {key!r} as a list"
        )
    items = tuple(str(item).strip() for item in value)
    if any(not item for item in items):
        raise LanguageConfigError(
            f"Language {language!r} {section} config has an empty {key!r} entry"
        )
    return items


def _parse_events_config(language: str, raw: Any) -> EventLanguageConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LanguageConfigError(
            f"Language {language!r} events config must be an object"
        )
    before_raw = raw.get("city_preposition_before") or {}
    if not isinstance(before_raw, dict):
        raise LanguageConfigError(
            f"Language {language!r} events.city_preposition_before must be an object"
        )
    before = tuple(
        (str(prefix).strip(), str(preposition).strip())
        for prefix, preposition in before_raw.items()
        if str(prefix).strip() and str(preposition).strip()
    )
    return EventLanguageConfig(
        program_word=_required_str(raw, "program_word", language=language),
        benefits_headings=_str_tuple(raw, "benefits_headings", language=language),
        quote_attributions=_str_tuple(raw, "quote_attributions", language=language),
        learn_more_prefixes=_str_tuple(raw, "learn_more_prefixes", language=language),
        city_preposition=_required_str(raw, "city_preposition", language=language),
        city_preposition_before=before,
        registration_cta=_required_str(raw, "registration_cta", language=language),
        html_registration_label=_required_str(
            raw, "html_registration_label", language=language
        ),
        closing_lines=_str_tuple(raw, "closing_lines", language=language),
        page_heading=_required_str(raw, "page_heading", language=language),
        empty_state=_required_str(raw, "empty_state", language=language),
        profile_alt=_required_str(raw, "profile_alt", language=language),
    )


def _parse_ingest_config(language: str, raw: Any) -> IngestLanguageConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LanguageConfigError(
            f"Language {language!r} ingest config must be an object"
        )
    language_id_raw = raw.get("smartcat_language_id")
    try:
        language_id = int(language_id_raw)
    except (TypeError, ValueError) as exc:
        raise LanguageConfigError(
            f"Language {language!r} ingest.smartcat_language_id must be an integer"
        ) from exc
    letter_pattern = _required_str(
        raw, "letter_pattern", language=language, section="ingest"
    )
    try:
        re.compile(letter_pattern)
    except re.error as exc:
        raise LanguageConfigError(
            f"Language {language!r} ingest.letter_pattern is not a valid regex"
        ) from exc
    return IngestLanguageConfig(
        smartcat_language_id=language_id,
        aliases=_str_tuple(raw, "aliases", language=language, section="ingest"),
        quote_open=_required_str(
            raw, "quote_open", language=language, section="ingest"
        ),
        quote_close=_required_str(
            raw, "quote_close", language=language, section="ingest"
        ),
        letter_pattern=letter_pattern,
        title_case_small_words=_str_tuple(
            raw,
            "title_case_small_words",
            language=language,
            section="ingest",
            allow_empty=True,
        ),
    )


def _parse_publish_config(language: str, raw: Any) -> PublishLanguageConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LanguageConfigError(
            f"Language {language!r} publish config must be an object"
        )
    return PublishLanguageConfig(
        display_name=_required_str(
            raw, "display_name", language=language, section="publish"
        ),
        hashtag=_required_str(raw, "hashtag", language=language, section="publish"),
        youtube_title_pipe_suffix=_required_str(
            raw, "youtube_title_pipe_suffix", language=language, section="publish"
        ),
        shorts_description_hashtags=_required_str(
            raw, "shorts_description_hashtags", language=language, section="publish"
        ),
        quote_youtube_description_hashtag=_required_str(
            raw,
            "quote_youtube_description_hashtag",
            language=language,
            section="publish",
        ),
        learn_more_label=_required_str(
            raw, "learn_more_label", language=language, section="publish"
        ),
        youtube_tags=_str_tuple(
            raw, "youtube_tags", language=language, section="publish"
        ),
    )


@lru_cache(maxsize=4)
def load_languages(path: str | None = None) -> dict[str, LanguageDefinition]:
    config_path = Path(path) if path else languages_config_path()
    if not config_path.is_file():
        raise LanguageConfigError(f"Languages config not found: {config_path}")
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload:
        raise LanguageConfigError(f"Languages config is empty: {config_path}")

    languages: dict[str, LanguageDefinition] = {}
    for name, raw in payload.items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            raise LanguageConfigError(
                f"Invalid language entry {name!r} in {config_path}"
            )
        alias = str(raw.get("alias") or "").strip().lower()
        if not alias:
            raise LanguageConfigError(f"Language {name!r} is missing alias")
        country = str(raw.get("country") or "").strip()
        if not country:
            raise LanguageConfigError(f"Language {name!r} is missing country")
        months = raw.get("months")
        if not isinstance(months, list) or len(months) != 12:
            raise LanguageConfigError(
                f"Language {name!r} must define 12 month names"
            )
        month_names = tuple(str(item).strip() for item in months)
        if any(not item for item in month_names):
            raise LanguageConfigError(f"Language {name!r} has an empty month name")
        definition = LanguageDefinition(
            name=name.strip(),
            alias=alias,
            country=country,
            months=month_names,
            date_year_suffix=str(raw.get("date_year_suffix") or ""),
            events=_parse_events_config(name.strip(), raw.get("events")),
            ingest=_parse_ingest_config(name.strip(), raw.get("ingest")),
            publish=_parse_publish_config(name.strip(), raw.get("publish")),
        )
        languages[definition.name.casefold()] = definition
        languages[definition.alias] = definition
    return languages


def get_language(language: str, *, path: str | None = None) -> LanguageDefinition | None:
    key = language.strip().casefold()
    if not key:
        return None
    return load_languages(path).get(key)


def selected_language_name() -> str:
    name = os.getenv("TARGET_LANGUAGE", "").strip()
    if not name:
        raise LanguageConfigError(
            "TARGET_LANGUAGE is required (key in config/languages.json)"
        )
    return name


def selected_language(*, path: str | None = None) -> LanguageDefinition:
    name = selected_language_name()
    language = get_language(name, path=path)
    if language is None:
        raise LanguageConfigError(
            f"Unknown language {name!r}. Add it to {DEFAULT_LANGUAGES_PATH}."
        )
    return language
