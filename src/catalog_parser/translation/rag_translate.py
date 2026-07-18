"""Per-cue RAG translator for English→Bulgarian subtitles (OpenAI + Anthropic)."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import requests

from catalog_parser.translation.index import CorpusHit
from catalog_parser.translation.srt import Cue, parse_srt, write_srt

Provider = Literal["openai", "anthropic"]

DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_TOP_K = 8
DEFAULT_BATCH_SIZE = 5
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 4096

# Back-compat aliases used by older imports / docs.
DEFAULT_MODEL = DEFAULT_OPENAI_MODEL
DEFAULT_BASE_URL = DEFAULT_OPENAI_BASE_URL

SYSTEM_PROMPT = (
    "You are a professional subtitle translator for Sadhguru / Isha content. "
    "Translate English cues into natural Bulgarian that matches the spiritual, "
    "spoken register of the provided example translations. Preserve meaning; "
    "do not add explanations. Return only the requested output format."
)

METADATA_TITLE_SYSTEM_PROMPT = (
    "You are a professional YouTube metadata translator for Sadhguru / Isha "
    "content. Translate English video titles into natural Bulgarian that matches "
    "the tone and phrasing of the provided example title translations. Keep "
    "titles concise and YouTube-ready. Preserve meaning; do not add explanations. "
    "Return only the Bulgarian title text."
)

METADATA_DESCRIPTION_SYSTEM_PROMPT = (
    "You are a professional YouTube metadata translator for Sadhguru / Isha "
    "content. Translate English video descriptions into natural Bulgarian that "
    "matches the tone of the provided example description translations. Preserve "
    "paragraph breaks. Do not invent links, hashtags, or calls to action that are "
    "not in the source. Preserve meaning; do not add explanations. Return only "
    "the Bulgarian description text."
)

# Reel/Short subtitles are always displayed in ALL CAPS; long-form Video uses
# normal (sentence) capitalization.
ALL_CAPS_RECORD_TYPES = frozenset({"reel", "short"})
DEFAULT_METADATA_TOP_K = 8
DEFAULT_METADATA_TITLE_MAX_TOKENS = 256
DEFAULT_METADATA_DESCRIPTION_MAX_TOKENS = 2048


def requires_all_caps(record_type: str | None) -> bool:
    if not record_type or not str(record_type).strip():
        return False
    return str(record_type).strip().casefold() in ALL_CAPS_RECORD_TYPES


def apply_translation_casing(text: str, record_type: str | None) -> str:
    """Enforce display casing: ALL CAPS for Reel/Short; unchanged otherwise."""
    if requires_all_caps(record_type):
        return text.upper()
    return text


def _casing_instruction(record_type: str | None) -> str:
    if requires_all_caps(record_type):
        return (
            "Capitalization: write the Bulgarian translation in ALL CAPS "
            "(uppercase letters only for letters)."
        )
    if record_type and str(record_type).strip().casefold() == "video":
        return (
            "Capitalization: use normal sentence capitalization "
            "(not ALL CAPS)."
        )
    return ""


class RetrievalIndex(Protocol):
    def retrieve(self, query_en: str, k: int = 8) -> list[CorpusHit]: ...


@dataclass(frozen=True)
class ChatConfig:
    api_key: str
    provider: Provider = "openai"
    base_url: str = DEFAULT_OPENAI_BASE_URL
    model: str = DEFAULT_OPENAI_MODEL
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout_seconds: float = 120.0
    anthropic_version: str = DEFAULT_ANTHROPIC_VERSION


# Back-compat name for callers/tests that still import OpenAIChatConfig.
OpenAIChatConfig = ChatConfig


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def chat_config_from_env() -> ChatConfig:
    """
    Resolve chat provider from env.

    Preference:
    1. TRANSLATION_PROVIDER=anthropic|openai (explicit)
    2. ANTHROPIC_API_KEY set → Anthropic
    3. OPENAI_API_KEY set → OpenAI-compatible
    """
    provider_raw = _env("TRANSLATION_PROVIDER").casefold()
    anthropic_key = _env("ANTHROPIC_API_KEY")
    openai_key = _env("OPENAI_API_KEY")

    if provider_raw in {"anthropic", "claude"}:
        provider: Provider = "anthropic"
    elif provider_raw in {"openai", "open-ai"}:
        provider = "openai"
    elif anthropic_key:
        provider = "anthropic"
    elif openai_key:
        provider = "openai"
    else:
        raise RuntimeError(
            "Set ANTHROPIC_API_KEY (preferred for Claude) or OPENAI_API_KEY "
            "for translation"
        )

    if provider == "anthropic":
        if not anthropic_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required for Anthropic translation")
        base_url = (
            _env("ANTHROPIC_BASE_URL", DEFAULT_ANTHROPIC_BASE_URL)
            or DEFAULT_ANTHROPIC_BASE_URL
        )
        model = _env("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL) or DEFAULT_ANTHROPIC_MODEL
        return ChatConfig(
            api_key=anthropic_key,
            provider="anthropic",
            base_url=base_url.rstrip("/"),
            model=model,
        )

    if not openai_key:
        raise RuntimeError("OPENAI_API_KEY is required for OpenAI-compatible translation")
    base_url = _env("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL) or DEFAULT_OPENAI_BASE_URL
    model = _env("OPENAI_MODEL", DEFAULT_OPENAI_MODEL) or DEFAULT_OPENAI_MODEL
    return ChatConfig(
        api_key=openai_key,
        provider="openai",
        base_url=base_url.rstrip("/"),
        model=model,
    )


def format_examples(examples: list[CorpusHit]) -> str:
    if not examples:
        return "(no examples retrieved)"
    lines: list[str] = []
    for index, hit in enumerate(examples, start=1):
        lines.append(f"{index}. EN: {hit.en}")
        lines.append(f"   BG: {hit.bg}")
    return "\n".join(lines)


def build_single_cue_messages(
    cue_en: str,
    examples: list[CorpusHit],
    *,
    record_type: str | None = None,
) -> list[dict[str, str]]:
    casing = _casing_instruction(record_type)
    casing_block = f"\n{casing}\n" if casing else "\n"
    user = (
        "Translate the English subtitle cue into Bulgarian.\n\n"
        f"Examples from prior Sadhguru translations:\n{format_examples(examples)}\n\n"
        f"English cue:\n{cue_en}\n"
        f"{casing_block}"
        "Respond with Bulgarian translation text only."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def build_batch_messages(
    cues: list[str],
    examples_per_cue: list[list[CorpusHit]],
    *,
    record_type: str | None = None,
) -> list[dict[str, str]]:
    blocks: list[str] = []
    for index, (cue_en, examples) in enumerate(zip(cues, examples_per_cue), start=1):
        blocks.append(
            f"### Cue {index}\n"
            f"Examples:\n{format_examples(examples)}\n"
            f"English:\n{cue_en}"
        )
    casing = _casing_instruction(record_type)
    casing_block = f"\n{casing}\n" if casing else "\n"
    user = (
        "Translate each English subtitle cue into Bulgarian.\n"
        "Use the examples for that cue when choosing wording and register.\n\n"
        + "\n\n".join(blocks)
        + casing_block
        + "Respond with a JSON array of strings, one Bulgarian translation per cue, "
        "in the same order. No markdown fences."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _split_system_messages(
    messages: list[dict[str, str]],
) -> tuple[str | None, list[dict[str, str]]]:
    system_parts: list[str] = []
    chat_messages: list[dict[str, str]] = []
    for message in messages:
        role = message.get("role", "")
        content = message.get("content", "")
        if role == "system":
            if content:
                system_parts.append(content)
            continue
        chat_messages.append({"role": role, "content": content})
    system = "\n\n".join(system_parts) if system_parts else None
    return system, chat_messages


def _openai_chat_completion(
    messages: list[dict[str, str]],
    config: ChatConfig,
    *,
    session: requests.Session,
) -> str:
    url = f"{config.base_url.rstrip('/')}/chat/completions"
    response = session.post(
        url,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": config.model,
            "temperature": config.temperature,
            "messages": messages,
        },
        timeout=config.timeout_seconds,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"OpenAI Chat Completions HTTP {response.status_code}: {response.text[:500]}"
        )
    payload = response.json()
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected OpenAI chat response: {payload!r}") from exc
    if not isinstance(content, str):
        raise RuntimeError(f"OpenAI chat response content is not a string: {content!r}")
    return content.strip()


def _anthropic_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        raise RuntimeError(f"Unexpected Anthropic content shape: {content!r}")
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    if not parts:
        raise RuntimeError(f"Anthropic response had no text blocks: {content!r}")
    return "\n".join(parts).strip()


def _anthropic_messages(
    messages: list[dict[str, str]],
    config: ChatConfig,
    *,
    session: requests.Session,
) -> str:
    system, chat_messages = _split_system_messages(messages)
    if not chat_messages:
        raise RuntimeError("Anthropic request requires at least one non-system message")

    url = f"{config.base_url.rstrip('/')}/v1/messages"
    body: dict[str, Any] = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "temperature": config.temperature,
        "messages": chat_messages,
    }
    if system:
        body["system"] = system

    response = session.post(
        url,
        headers={
            "x-api-key": config.api_key,
            "anthropic-version": config.anthropic_version,
            "Content-Type": "application/json",
        },
        json=body,
        timeout=config.timeout_seconds,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Anthropic Messages HTTP {response.status_code}: {response.text[:500]}"
        )
    payload = response.json()
    try:
        content = payload["content"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"Unexpected Anthropic response: {payload!r}") from exc
    return _anthropic_text_from_content(content)


def chat_completion(
    messages: list[dict[str, str]],
    config: ChatConfig,
    *,
    session: requests.Session | None = None,
) -> str:
    http = session or requests.Session()
    if config.provider == "anthropic":
        return _anthropic_messages(messages, config, session=http)
    return _openai_chat_completion(messages, config, session=http)


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def parse_batch_translations(raw: str, expected: int) -> list[str]:
    cleaned = _strip_code_fence(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fallback: one translation per non-empty line
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        if len(lines) == expected:
            return lines
        raise RuntimeError(
            f"Could not parse batch translations as JSON (expected {expected}): {raw[:300]!r}"
        )
    if not isinstance(parsed, list) or len(parsed) != expected:
        raise RuntimeError(
            f"Batch translation JSON must be a list of length {expected}, got {parsed!r}"
        )
    out: list[str] = []
    for item in parsed:
        if not isinstance(item, str):
            raise RuntimeError(f"Batch translation item is not a string: {item!r}")
        text = item.strip()
        if not text:
            raise RuntimeError("Batch translation contains an empty string")
        out.append(text)
    return out


def translate_cue_texts(
    cue_texts: list[str],
    index: RetrievalIndex,
    config: ChatConfig,
    *,
    top_k: int = DEFAULT_TOP_K,
    batch_size: int = DEFAULT_BATCH_SIZE,
    record_type: str | None = None,
    session: requests.Session | None = None,
) -> list[str]:
    if not cue_texts:
        return []
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    http = session or requests.Session()
    translations: list[str] = []
    for start in range(0, len(cue_texts), batch_size):
        batch = cue_texts[start : start + batch_size]
        examples_per_cue = [index.retrieve(text, k=top_k) for text in batch]
        if len(batch) == 1:
            messages = build_single_cue_messages(
                batch[0],
                examples_per_cue[0],
                record_type=record_type,
            )
            translations.append(chat_completion(messages, config, session=http))
            continue
        messages = build_batch_messages(
            batch,
            examples_per_cue,
            record_type=record_type,
        )
        raw = chat_completion(messages, config, session=http)
        translations.extend(parse_batch_translations(raw, len(batch)))
    return [
        apply_translation_casing(text, record_type) for text in translations
    ]


def translate_cues(
    cues: list[Cue],
    index: RetrievalIndex,
    config: ChatConfig,
    *,
    top_k: int = DEFAULT_TOP_K,
    batch_size: int = DEFAULT_BATCH_SIZE,
    record_type: str | None = None,
    session: requests.Session | None = None,
) -> list[Cue]:
    texts = translate_cue_texts(
        [cue.text for cue in cues],
        index,
        config,
        top_k=top_k,
        batch_size=batch_size,
        record_type=record_type,
        session=session,
    )
    return [
        Cue(index=cue.index, start=cue.start, end=cue.end, text=bg)
        for cue, bg in zip(cues, texts)
    ]


def translate_srt_text(
    source_srt: str,
    index: RetrievalIndex,
    config: ChatConfig,
    *,
    top_k: int = DEFAULT_TOP_K,
    batch_size: int = DEFAULT_BATCH_SIZE,
    record_type: str | None = None,
    session: requests.Session | None = None,
) -> str:
    cues = parse_srt(source_srt)
    translated = translate_cues(
        cues,
        index,
        config,
        top_k=top_k,
        batch_size=batch_size,
        record_type=record_type,
        session=session,
    )
    return write_srt(translated)


def build_metadata_messages(
    en_text: str,
    examples: list[CorpusHit],
    *,
    kind: Literal["title", "description"],
) -> list[dict[str, str]]:
    if kind == "title":
        system = METADATA_TITLE_SYSTEM_PROMPT
        field_label = "title"
        instructions = (
            "Translate the English YouTube title into Bulgarian.\n"
            "Keep it concise and natural for YouTube.\n\n"
        )
    elif kind == "description":
        system = METADATA_DESCRIPTION_SYSTEM_PROMPT
        field_label = "description"
        instructions = (
            "Translate the English YouTube description into Bulgarian.\n"
            "Preserve paragraph breaks. Do not invent links or hashtags.\n\n"
        )
    else:
        raise ValueError(f"Unsupported metadata kind: {kind!r}")

    user = (
        f"{instructions}"
        f"Examples from prior Sadhguru translations:\n{format_examples(examples)}\n\n"
        f"English {field_label}:\n{en_text}\n\n"
        f"Respond with Bulgarian {field_label} text only."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def translate_metadata_field(
    en_text: str,
    *,
    kind: Literal["title", "description"],
    index: RetrievalIndex,
    config: ChatConfig,
    top_k: int = DEFAULT_METADATA_TOP_K,
    session: requests.Session | None = None,
) -> str:
    text = (en_text or "").strip()
    if not text:
        raise ValueError(f"Cannot translate empty metadata {kind}")

    examples = index.retrieve(text, k=top_k)
    messages = build_metadata_messages(text, examples, kind=kind)
    field_config = config
    if kind == "title" and config.max_tokens > DEFAULT_METADATA_TITLE_MAX_TOKENS:
        field_config = ChatConfig(
            api_key=config.api_key,
            provider=config.provider,
            base_url=config.base_url,
            model=config.model,
            temperature=config.temperature,
            max_tokens=DEFAULT_METADATA_TITLE_MAX_TOKENS,
            timeout_seconds=config.timeout_seconds,
            anthropic_version=config.anthropic_version,
        )
    elif (
        kind == "description"
        and config.max_tokens < DEFAULT_METADATA_DESCRIPTION_MAX_TOKENS
    ):
        field_config = ChatConfig(
            api_key=config.api_key,
            provider=config.provider,
            base_url=config.base_url,
            model=config.model,
            temperature=config.temperature,
            max_tokens=DEFAULT_METADATA_DESCRIPTION_MAX_TOKENS,
            timeout_seconds=config.timeout_seconds,
            anthropic_version=config.anthropic_version,
        )

    translated = chat_completion(messages, field_config, session=session).strip()
    if not translated:
        raise RuntimeError(f"Empty {kind} translation returned by model")
    return translated


def token_jaccard(a: str, b: str) -> float:
    left = set(re.findall(r"\w+", a.casefold(), flags=re.UNICODE))
    right = set(re.findall(r"\w+", b.casefold(), flags=re.UNICODE))
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def compare_srt_pair(human_srt: str, ai_srt: str) -> dict[str, Any]:
    human = parse_srt(human_srt)
    ai = parse_srt(ai_srt)
    n = min(len(human), len(ai))
    rows: list[dict[str, Any]] = []
    overlaps: list[float] = []
    for index in range(n):
        overlap = token_jaccard(human[index].text, ai[index].text)
        overlaps.append(overlap)
        rows.append(
            {
                "cue_index": human[index].index,
                "start": human[index].start,
                "end": human[index].end,
                "en": "",  # filled by caller when available
                "human_bg": human[index].text,
                "ai_bg": ai[index].text,
                "token_jaccard": round(overlap, 4),
            }
        )
    mean_overlap = sum(overlaps) / len(overlaps) if overlaps else 0.0
    return {
        "human_cues": len(human),
        "ai_cues": len(ai),
        "compared_cues": n,
        "mean_token_jaccard": round(mean_overlap, 4),
        "rows": rows,
    }
