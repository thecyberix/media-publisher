from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Protocol

DEFAULT_UI_BASE = "https://ea.smartcat.com"
DEFAULT_TARGET_LANGUAGE = "bg"

PKG_SM_LINK_PATTERN = re.compile(
    r"https?://[^/]+/projects/(?P<project_id>[a-f0-9-]+)/files",
    re.IGNORECASE,
)
BULGARIAN_LANGUAGE_ALIASES = frozenset({"bg", "bul", "bulgarian"})
BULGARIAN_LANGUAGE_ID = 1026
LANGUAGE_ID_BY_CODE = {
    "bg": BULGARIAN_LANGUAGE_ID,
    "bul": BULGARIAN_LANGUAGE_ID,
    "bulgarian": BULGARIAN_LANGUAGE_ID,
}
SRT_NAME_PATTERN = re.compile(r"\.srt$", re.IGNORECASE)
BULGARIAN_NAME_PATTERN = re.compile(
    r"(^|[._-])(bg|bul)([._-]|$)|bulgarian",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedPkgSmLink:
    project_id: str
    search: str | None


class SmartcatError(RuntimeError):
    pass


class BulgarianSrtResolver(Protocol):
    def resolve_bulgarian_srt_link(
        self,
        pkg_sm_link: str,
        *,
        title: str | None = None,
        language: str = DEFAULT_TARGET_LANGUAGE,
    ) -> str | None: ...


def parse_pkg_sm_link(value: str) -> ParsedPkgSmLink | None:
    value = value.strip()
    if not value:
        return None

    match = PKG_SM_LINK_PATTERN.search(value)
    if not match:
        return None

    query = urllib.parse.parse_qs(urllib.parse.urlparse(value).query)
    search_values = query.get("search")
    search = urllib.parse.unquote_plus(search_values[0]) if search_values else None

    return ParsedPkgSmLink(
        project_id=match.group("project_id"),
        search=search,
    )


def build_smartcat_editor_link(
    ui_base: str,
    document_id: str,
    *,
    language_id: int,
    pkg_sm_link: str,
) -> str:
    parsed = urllib.parse.urlparse(pkg_sm_link.strip())
    back_url = parsed.path
    if parsed.query:
        back_url = f"{back_url}?{parsed.query}"

    params = urllib.parse.urlencode(
        {
            "targetLanguageId": language_id,
            "backUrl": back_url,
        }
    )
    return f"{ui_base.rstrip('/')}/open-editor/{document_id}?{params}"


def _normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.strip().lower().split())


def _document_name(document: dict[str, Any]) -> str:
    return str(document.get("name") or document.get("fileName") or "")


def _score_document_match(document: dict[str, Any], search: str | None, title: str | None) -> int:
    name = _normalize_text(_document_name(document))
    if not name:
        return -1

    search_norm = _normalize_text(search)
    title_norm = _normalize_text(title)

    if search_norm and name == search_norm:
        return 100
    if title_norm and name == title_norm:
        return 95
    if search_norm and name.startswith(search_norm):
        return 90
    if title_norm and name.startswith(title_norm):
        return 85
    if search_norm and search_norm in name:
        return 80
    if title_norm and title_norm in name:
        return 75
    if search_norm and name.startswith(search_norm[: min(len(search_norm), 80)]):
        return 70
    if title_norm and name.startswith(title_norm[: min(len(title_norm), 80)]):
        return 65
    return -1


def find_matching_document(
    documents: list[dict[str, Any]],
    *,
    search: str | None,
    title: str | None,
) -> dict[str, Any] | None:
    best_document: dict[str, Any] | None = None
    best_score = -1

    for document in documents:
        score = _score_document_match(document, search, title)
        if score > best_score:
            best_score = score
            best_document = document

    return best_document


def _is_bulgarian_srt_name(name: str) -> bool:
    if not SRT_NAME_PATTERN.search(name):
        return False
    return bool(BULGARIAN_NAME_PATTERN.search(name))


def find_bulgarian_srt_document(
    documents: list[dict[str, Any]],
    *,
    search: str | None,
    title: str | None,
) -> dict[str, Any] | None:
    parent = find_matching_document(documents, search=search, title=title)
    if parent is None:
        return None

    parent_name = _normalize_text(_document_name(parent))
    best_document: dict[str, Any] | None = None
    best_score = -1

    for document in documents:
        name = _document_name(document)
        if not _is_bulgarian_srt_name(name):
            continue

        name_norm = _normalize_text(name)
        score = 0
        if parent_name and parent_name in name_norm:
            score += 50
        if search and _normalize_text(search) in name_norm:
            score += 30
        if title and _normalize_text(title) in name_norm:
            score += 20
        score += _score_document_match(document, search, title)

        if score > best_score:
            best_score = score
            best_document = document

    return best_document


def resolve_language_id(language: str) -> int:
    normalized = language.strip().lower()
    if normalized.isdigit():
        return int(normalized)
    if normalized in LANGUAGE_ID_BY_CODE:
        return LANGUAGE_ID_BY_CODE[normalized]
    raise SmartcatError(f"Unsupported Smartcat language code: {language!r}")


def document_has_language_target(document: dict[str, Any], language_id: int) -> bool:
    return get_language_target(document, language_id) is not None


def get_language_target(
    document: dict[str, Any],
    language_id: int,
) -> dict[str, Any] | None:
    targets = document.get("targets")
    if not isinstance(targets, list):
        return None
    for target in targets:
        if isinstance(target, dict) and target.get("languageId") == language_id:
            return target
    return None


def get_translation_stage(target: dict[str, Any]) -> dict[str, Any] | None:
    workflow_stages = target.get("workflowStages")
    if not isinstance(workflow_stages, list):
        return None

    for stage in workflow_stages:
        if isinstance(stage, dict) and stage.get("type") == 1:
            return stage

    if workflow_stages and isinstance(workflow_stages[0], dict):
        return workflow_stages[0]
    return None


def bulgarian_target_needs_translation(target: dict[str, Any]) -> bool:
    """Return True when the Bulgarian target has no translated subtitle content yet."""
    stage = get_translation_stage(target)
    if stage is None:
        return True

    if stage.get("translatedCharsWithoutSpaces", 0) > 0:
        return False
    if stage.get("wordsTranslated", 0) > 0:
        return False
    if stage.get("progress", 0) > 0:
        return False
    return True


def language_matches(value: str | None, language: str) -> bool:
    if not value:
        return False

    normalized = _normalize_text(value)
    aliases = {language.strip().lower()}
    if language.strip().lower() in BULGARIAN_LANGUAGE_ALIASES:
        aliases |= {code.lower() for code in BULGARIAN_LANGUAGE_ALIASES}

    if normalized in aliases:
        return True
    if any(alias in normalized for alias in aliases):
        return True
    return "bulgarian" in normalized and language.strip().lower() in BULGARIAN_LANGUAGE_ALIASES


def pick_document_download_url(document: dict[str, Any]) -> str | None:
    for key in ("downloadUrl", "url", "href", "link", "fileUrl"):
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def enrich_records_with_bulgarian_srt_links(
    records: list[dict[str, Any]],
    resolver: BulgarianSrtResolver,
    *,
    language: str = DEFAULT_TARGET_LANGUAGE,
    link_field: str = "pkgBgSrtLk",
    source_link_field: str = "pkgSmLk",
    title_field: str = "ctTitle",
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for record in records:
        updated = dict(record)
        pkg_sm_link = updated.get(source_link_field)
        if not isinstance(pkg_sm_link, str) or not pkg_sm_link.strip():
            enriched.append(updated)
            continue

        title = updated.get(title_field)
        title_value = title if isinstance(title, str) else None
        try:
            link = resolver.resolve_bulgarian_srt_link(
                pkg_sm_link,
                title=title_value,
                language=language,
            )
            if link is None:
                updated[link_field] = None
                updated[f"{link_field}SkipReason"] = (
                    "Bulgarian subtitles already completed in Smartcat"
                )
            else:
                updated[link_field] = link
                updated.pop(f"{link_field}SkipReason", None)
            updated.pop(f"{link_field}Error", None)
        except SmartcatError as exc:
            updated[link_field] = None
            updated[f"{link_field}Error"] = str(exc)
        except Exception as exc:
            updated[link_field] = None
            updated[f"{link_field}Error"] = str(exc)
        enriched.append(updated)
    return enriched
