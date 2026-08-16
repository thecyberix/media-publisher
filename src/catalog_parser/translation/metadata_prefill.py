"""Ingest-time AI translation of Bulgarian title/description from English Drive fields."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from catalog_parser.translation.index import (
    DEFAULT_HOLDOUT_PATH,
    DEFAULT_METADATA_DESCRIPTION_INDEX_PATH,
    DEFAULT_METADATA_PAIRS_PATH,
    DEFAULT_METADATA_TITLE_INDEX_PATH,
    Bm25Index,
    MetadataKind,
    load_or_build_metadata_index,
)
from catalog_parser.translation.prefill import ai_prefill_enabled
from catalog_parser.translation.rag_translate import (
    DEFAULT_METADATA_TOP_K,
    ChatConfig,
    chat_config_from_env,
    translate_metadata_field,
)

_INDEX_CACHE: dict[tuple[str, str, str, str], Bm25Index] = {}


@dataclass
class MetadataTranslateResult:
    ok: bool
    title_translated: bool = False
    description_translated: bool = False
    skipped: bool = False
    errors: list[str] = field(default_factory=list)


def _text_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def resolve_english_title(record: dict[str, Any]) -> str | None:
    return _text_or_none(record.get("ytTitle")) or _text_or_none(record.get("ctTitle"))


def resolve_english_description(record: dict[str, Any]) -> str | None:
    return _text_or_none(record.get("ytDescription"))


def _resolve_path(project_root: Path | None, path: Path) -> Path:
    if path.is_absolute():
        return path
    root = project_root or Path.cwd()
    return root / path


def get_metadata_index(
    kind: MetadataKind,
    *,
    project_root: Path | None = None,
    pairs_path: Path = DEFAULT_METADATA_PAIRS_PATH,
    holdout_path: Path = DEFAULT_HOLDOUT_PATH,
    title_index_path: Path = DEFAULT_METADATA_TITLE_INDEX_PATH,
    description_index_path: Path = DEFAULT_METADATA_DESCRIPTION_INDEX_PATH,
) -> Bm25Index:
    index_path = (
        title_index_path if kind == "title" else description_index_path
    )
    resolved_pairs = _resolve_path(project_root, pairs_path)
    resolved_holdout = _resolve_path(project_root, holdout_path)
    resolved_index = _resolve_path(project_root, index_path)
    cache_key = (
        kind,
        str(resolved_pairs),
        str(resolved_holdout),
        str(resolved_index),
    )
    cached = _INDEX_CACHE.get(cache_key)
    if cached is not None:
        return cached
    index = load_or_build_metadata_index(
        kind,
        index_path=resolved_index,
        pairs_path=resolved_pairs,
        holdout_path=resolved_holdout,
    )
    _INDEX_CACHE[cache_key] = index
    return index


def clear_metadata_index_cache() -> None:
    _INDEX_CACHE.clear()


def translate_record_metadata_if_needed(
    record: dict[str, Any],
    *,
    project_root: Path | None = None,
    config: ChatConfig | None = None,
    top_k: int = DEFAULT_METADATA_TOP_K,
    pairs_path: Path = DEFAULT_METADATA_PAIRS_PATH,
    holdout_path: Path = DEFAULT_HOLDOUT_PATH,
    title_index_path: Path = DEFAULT_METADATA_TITLE_INDEX_PATH,
    description_index_path: Path = DEFAULT_METADATA_DESCRIPTION_INDEX_PATH,
    enabled: bool | None = None,
) -> MetadataTranslateResult:
    """
    Translate EN title/description into bgTitle / bgDescription on the record.

    Gated by TRANSLATION_PROVIDER (``none`` skips all AI) unless ``enabled`` is
    passed explicitly.
    """
    if enabled is None:
        enabled = ai_prefill_enabled()
    if not enabled:
        return MetadataTranslateResult(ok=True, skipped=True)

    en_title = resolve_english_title(record)
    en_description = resolve_english_description(record)
    if not en_title and not en_description:
        return MetadataTranslateResult(
            ok=True,
            skipped=True,
            errors=["no English title or description on record"],
        )

    chat = config or chat_config_from_env()
    errors: list[str] = []
    title_ok = False
    description_ok = False

    if en_title and not _text_or_none(record.get("bgTitle")):
        try:
            index = get_metadata_index(
                "title",
                project_root=project_root,
                pairs_path=pairs_path,
                holdout_path=holdout_path,
                title_index_path=title_index_path,
                description_index_path=description_index_path,
            )
            record["bgTitle"] = translate_metadata_field(
                en_title,
                kind="title",
                index=index,
                config=chat,
                top_k=top_k,
            )
            title_ok = True
        except Exception as exc:  # noqa: BLE001 — ingest must continue
            errors.append(f"title: {exc}")

    if en_description and not _text_or_none(record.get("bgDescription")):
        try:
            index = get_metadata_index(
                "description",
                project_root=project_root,
                pairs_path=pairs_path,
                holdout_path=holdout_path,
                title_index_path=title_index_path,
                description_index_path=description_index_path,
            )
            record["bgDescription"] = translate_metadata_field(
                en_description,
                kind="description",
                index=index,
                config=chat,
                top_k=top_k,
            )
            description_ok = True
        except Exception as exc:  # noqa: BLE001 — ingest must continue
            errors.append(f"description: {exc}")

    translated_any = title_ok or description_ok
    return MetadataTranslateResult(
        ok=translated_any or not errors,
        title_translated=title_ok,
        description_translated=description_ok,
        skipped=not translated_any and not errors,
        errors=errors,
    )
