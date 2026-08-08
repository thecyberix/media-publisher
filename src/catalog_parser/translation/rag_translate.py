"""Per-cue RAG translator for English→Bulgarian subtitles (OpenAI + Anthropic)."""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
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

SUBTITLE_FORMAT_RULES = (
    "Formatting rules:\n"
    "- Do not insert line breaks unless the English text itself contains a "
    "line break.\n"
    "- Use Bulgarian quotation marks „…“ (not ASCII \"). "
    "If this cue opens a quotation, include „. "
    "If this cue closes a quotation that began earlier, end with “.\n"
    "- When previous/next subtitle context is provided, keep the wording "
    "continuous with that dialogue; translate only the English marked for "
    "translation."
)

BG_QUOTE_OPEN = "„"
BG_QUOTE_CLOSE = "“"

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

METADATA_CAPTION_SYSTEM_PROMPT = (
    "You are a professional thumbnail caption translator for Sadhguru / Isha "
    "content. Translate English overlay caption lines into natural Bulgarian that "
    "matches the tone and phrasing of the provided example title translations. "
    "Preserve the same number of lines and line breaks as the English source. "
    "Do not add credits, hashtags, logos, or explanations. Return only the "
    "Bulgarian caption text."
)

CAPTION_EXTRACT_PROMPT = (
    "Extract the English overlay caption text from this video thumbnail image.\n"
    "Return ONLY a JSON array of strings, one string per visual caption line "
    "from top to bottom (e.g. [\"Line one\", \"Line two\"]).\n"
    "Preserve the capitalization of each line as shown in the image.\n"
    "Prefer complete caption lines over omitting part of the headline; a little "
    "extra overlay text is better than missing title lines.\n"
    "Ignore watermarks, logos, channel names, and non-caption UI chrome.\n"
    "If there is no readable caption text, return []."
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


def match_source_newlines(source: str, translation: str) -> str:
    """Keep translation line breaks aligned with the English source."""
    src = (source or "").replace("\r\n", "\n").replace("\r", "\n")
    tr = (translation or "").replace("\r\n", "\n").replace("\r", "\n")
    if "\n" not in src.strip():
        return " ".join(tr.split())
    # Drop blank lines the model invents between subtitle rows.
    lines = [line.strip() for line in tr.split("\n")]
    return "\n".join(line for line in lines if line)


def _line_is_all_caps(line: str) -> bool:
    letters = [ch for ch in line if ch.isalpha()]
    return bool(letters) and all(ch.isupper() for ch in letters)


def _line_is_title_case(line: str) -> bool:
    words = [word for word in re.split(r"\s+", line.strip()) if word]
    if len(words) < 2:
        return False
    titled = 0
    for word in words:
        letters = [ch for ch in word if ch.isalpha()]
        if not letters:
            continue
        if letters[0].isupper() and all(ch.islower() for ch in letters[1:]):
            titled += 1
    return titled >= max(2, (len(words) + 1) // 2)


def _to_title_case_words(line: str) -> str:
    parts: list[str] = []
    for word in re.split(r"(\s+)", line):
        if not word or word.isspace():
            parts.append(word)
            continue
        letters = [ch for ch in word if ch.isalpha()]
        if not letters:
            parts.append(word)
            continue
        # Title-case alphabetic runs inside the token.
        chars = list(word)
        seen_letter = False
        for index, ch in enumerate(chars):
            if ch.isalpha():
                chars[index] = ch.upper() if not seen_letter else ch.lower()
                seen_letter = True
        parts.append("".join(chars))
    return "".join(parts)


def match_source_line_casing(source: str, translation: str) -> str:
    """Match per-line casing style of the English caption source."""
    src = (source or "").replace("\r\n", "\n").replace("\r", "\n")
    tr = (translation or "").replace("\r\n", "\n").replace("\r", "\n")
    src_lines = [line for line in src.split("\n") if line.strip()]
    tr_lines = [line.strip() for line in tr.split("\n") if line.strip()]
    if not src_lines or not tr_lines:
        return tr.strip()

    # Align counts: pad/truncate translation lines to source line count.
    if len(tr_lines) < len(src_lines):
        joined = " ".join(tr_lines)
        tr_lines = [joined] + [""] * (len(src_lines) - 1)
        tr_lines = tr_lines[: len(src_lines)]
    elif len(tr_lines) > len(src_lines):
        head = tr_lines[: len(src_lines) - 1]
        tail = " ".join(tr_lines[len(src_lines) - 1 :])
        tr_lines = head + [tail]

    out: list[str] = []
    for src_line, tr_line in zip(src_lines, tr_lines, strict=True):
        if not tr_line:
            continue
        if _line_is_all_caps(src_line):
            out.append(tr_line.upper())
        elif _line_is_title_case(src_line):
            out.append(_to_title_case_words(tr_line))
        else:
            out.append(tr_line)
    return "\n".join(out)


def _ensure_bg_opening_quote(text: str) -> str:
    if BG_QUOTE_OPEN in text:
        return text
    cleaned = text.replace('"', "").replace("“", "").replace("„", "")
    for marker in (": ", ", "):
        idx = cleaned.find(marker)
        if idx >= 0:
            at = idx + len(marker)
            return cleaned[:at] + BG_QUOTE_OPEN + cleaned[at:]
    return BG_QUOTE_OPEN + cleaned


def _ensure_bg_closing_quote(text: str) -> str:
    cleaned = text.replace('"', "").rstrip()
    cleaned = normalize_bg_quote_punctuation(cleaned)
    if BG_QUOTE_CLOSE in cleaned:
        return cleaned
    if cleaned.endswith(BG_QUOTE_OPEN):
        return cleaned + BG_QUOTE_CLOSE
    # Bulgarian style: „…?“ / „…!“ — closing quote after sentence punctuation.
    return cleaned + BG_QUOTE_CLOSE


def normalize_bg_quote_punctuation(text: str) -> str:
    """Move sentence punctuation inside closing quotes: „…“? → „…?“."""
    if not text:
        return text
    # Also handle ASCII " left before punctuation.
    fixed = re.sub(r'"([?!….])', rf"\1{BG_QUOTE_CLOSE}", text)
    return re.sub(
        rf"{re.escape(BG_QUOTE_CLOSE)}([?!….])",
        rf"\1{BG_QUOTE_CLOSE}",
        fixed,
    )


def _english_quote_count(text: str) -> int:
    return sum((text or "").count(ch) for ch in '"“„”')


def repair_bulgarian_quotes(
    sources: list[str],
    translations: list[str],
) -> list[str]:
    """
    Pair Bulgarian „…“ across subtitle fragments split by Smartcat.

    English ASR often splits one quoted utterance across segments
    (opens with \" in segment N, closes in N+1).
    """
    if len(sources) != len(translations):
        raise ValueError("sources and translations must be the same length")
    quote_open = False
    out: list[str] = []
    for source, translation in zip(sources, translations):
        quote_count = _english_quote_count(source)
        ends_open = quote_open ^ (quote_count % 2 == 1)
        bg = (translation or "").replace('"', "")
        started_open = quote_open
        if not started_open and ends_open:
            bg = _ensure_bg_opening_quote(bg)
        if started_open and not ends_open:
            bg = _ensure_bg_closing_quote(bg)
        if not started_open and not ends_open and quote_count >= 2:
            # Opens and closes within the same fragment.
            bg = _ensure_bg_opening_quote(bg)
            bg = _ensure_bg_closing_quote(bg)
        quote_open = ends_open
        out.append(normalize_bg_quote_punctuation(bg))
    if quote_open and out:
        out[-1] = _ensure_bg_closing_quote(out[-1])
        out[-1] = normalize_bg_quote_punctuation(out[-1])
    return out


def polish_subtitle_translations(
    sources: list[str],
    translations: list[str],
) -> list[str]:
    """Apply newline + quote fixes after model translation."""
    if len(sources) != len(translations):
        raise ValueError("sources and translations must be the same length")
    lined = [
        match_source_newlines(source, text)
        for source, text in zip(sources, translations)
    ]
    return repair_bulgarian_quotes(sources, lined)


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
    previous_en: str | None = None,
    next_en: str | None = None,
) -> list[dict[str, str]]:
    casing = _casing_instruction(record_type)
    casing_block = f"\n{casing}\n" if casing else "\n"
    context_parts: list[str] = []
    if previous_en and previous_en.strip():
        context_parts.append(
            f"Previous subtitle (context only):\n{previous_en.strip()}"
        )
    if next_en and next_en.strip():
        context_parts.append(
            f"Next subtitle (context only):\n{next_en.strip()}"
        )
    context_block = ("\n\n".join(context_parts) + "\n\n") if context_parts else ""
    user = (
        "Translate the English subtitle cue into Bulgarian.\n\n"
        f"{SUBTITLE_FORMAT_RULES}\n\n"
        f"Examples from prior Sadhguru translations:\n{format_examples(examples)}\n\n"
        f"{context_block}"
        f"English to translate:\n{cue_en}\n"
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
    previous_en: str | None = None,
    next_en: str | None = None,
    all_document_cues: list[str] | None = None,
    batch_start: int = 0,
) -> list[dict[str, str]]:
    blocks: list[str] = []
    doc = all_document_cues or cues
    for offset, (cue_en, examples) in enumerate(zip(cues, examples_per_cue)):
        absolute = batch_start + offset
        prev = (
            doc[absolute - 1]
            if absolute > 0
            else previous_en
        )
        nxt = (
            doc[absolute + 1]
            if absolute + 1 < len(doc)
            else next_en
        )
        context_parts: list[str] = []
        if prev and str(prev).strip():
            context_parts.append(
                f"Previous subtitle (context only):\n{str(prev).strip()}"
            )
        if nxt and str(nxt).strip():
            context_parts.append(
                f"Next subtitle (context only):\n{str(nxt).strip()}"
            )
        context_block = (
            ("\n".join(context_parts) + "\n") if context_parts else ""
        )
        blocks.append(
            f"### Cue {offset + 1}\n"
            f"{context_block}"
            f"Examples:\n{format_examples(examples)}\n"
            f"English to translate:\n{cue_en}"
        )
    casing = _casing_instruction(record_type)
    casing_block = f"\n{casing}\n" if casing else "\n"
    user = (
        "Translate each English subtitle cue into Bulgarian.\n"
        "Use the examples for that cue when choosing wording and register.\n\n"
        f"{SUBTITLE_FORMAT_RULES}\n\n"
        + "\n\n".join(blocks)
        + casing_block
        + "Respond with a JSON array of strings, one Bulgarian translation per cue, "
        "in the same order. No markdown fences. "
        "If a translation contains double quotes, escape them as \\\". "
        "Prefer Bulgarian quotation marks „…“ and avoid ASCII \" inside strings."
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


def _guess_image_media_type(path: Path | None, raw: bytes) -> str:
    if path is not None:
        guessed, _ = mimetypes.guess_type(str(path))
        if guessed and guessed.startswith("image/"):
            return guessed
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"RIFF") and b"WEBP" in raw[:16]:
        return "image/webp"
    return "image/jpeg"


def chat_completion_with_image(
    prompt: str,
    image_bytes: bytes,
    config: ChatConfig,
    *,
    media_type: str = "image/jpeg",
    session: requests.Session | None = None,
) -> str:
    """Multimodal chat: text prompt + one image."""
    if not image_bytes:
        raise ValueError("image_bytes is required")
    http = session or requests.Session()
    encoded = base64.b64encode(image_bytes).decode("ascii")
    if config.provider == "anthropic":
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": encoded,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        return _anthropic_messages_multimodal(messages, config, session=http)

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{media_type};base64,{encoded}",
                    },
                },
            ],
        }
    ]
    return _openai_chat_completion_multimodal(messages, config, session=http)


def _openai_chat_completion_multimodal(
    messages: list[dict[str, Any]],
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


def _anthropic_messages_multimodal(
    messages: list[dict[str, Any]],
    config: ChatConfig,
    *,
    session: requests.Session,
) -> str:
    url = f"{config.base_url.rstrip('/')}/v1/messages"
    body: dict[str, Any] = {
        "model": config.model,
        "max_tokens": min(config.max_tokens, 1024),
        "temperature": config.temperature,
        "messages": messages,
    }
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


def parse_caption_lines_json(raw: str) -> list[str]:
    """Parse vision caption extraction output into non-empty lines."""
    cleaned = _strip_code_fence(raw)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            lines = [
                item.strip()
                for item in parsed
                if isinstance(item, str) and item.strip()
            ]
            return lines
    except json.JSONDecodeError:
        pass
    extracted = _extract_json_string_array(cleaned)
    if extracted is not None:
        return [item.strip() for item in extracted if item.strip()]
    # Fallback: plain lines
    return [line.strip() for line in cleaned.splitlines() if line.strip()]


def extract_caption_lines_from_image(
    image_bytes: bytes,
    config: ChatConfig,
    *,
    media_type: str = "image/jpeg",
    session: requests.Session | None = None,
) -> list[str]:
    raw = chat_completion_with_image(
        CAPTION_EXTRACT_PROMPT,
        image_bytes,
        config,
        media_type=media_type,
        session=session,
    )
    return parse_caption_lines_json(raw)


def extract_caption_lines_from_image_path(
    path: Path,
    config: ChatConfig,
    *,
    session: requests.Session | None = None,
) -> list[str]:
    raw = path.read_bytes()
    media_type = _guess_image_media_type(path, raw)
    return extract_caption_lines_from_image(
        raw,
        config,
        media_type=media_type,
        session=session,
    )


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def _repair_llm_json_string_array(text: str) -> str:
    """Fix common LLM mistakes: unescaped \" ending as \"\", before , or ]."""
    # "…?"",  /  "…."",  → close the string with a single "
    return re.sub(r'""(\s*[,]])', r'"\1', text)


def _extract_json_string_array(text: str) -> list[str] | None:
    """
    Lenient scan for a JSON-looking array of strings.

    Treats the end of each element as a quote followed by comma/] (or the
    broken LLM form \"\", / \"\"] ), so internal ASCII quotes from dialogue
    do not have to be escaped.
    """
    cleaned = text.strip()
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start < 0 or end <= start:
        return None
    body = cleaned[start + 1 : end]
    items: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        while i < n and body[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break
        if body[i] != '"':
            return None
        i += 1
        chunk_start = i
        match = re.search(r'""?\s*(,|$)', body[i:])
        if match is None:
            return None
        chunk = body[chunk_start : i + match.start()]
        # If the model used "", keep content before the extra quote.
        items.append(chunk)
        i = i + match.end()
    return items


def _normalize_batch_translation_list(
    parsed: list[Any],
    expected: int,
) -> list[str]:
    if len(parsed) != expected:
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


def parse_batch_translations(raw: str, expected: int) -> list[str]:
    cleaned = _strip_code_fence(raw)
    candidates = [cleaned, _repair_llm_json_string_array(cleaned)]
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            return _normalize_batch_translation_list(parsed, expected)

    extracted = _extract_json_string_array(cleaned)
    if extracted is not None:
        return _normalize_batch_translation_list(extracted, expected)

    # Fallback: one translation per non-empty line (no JSON wrapper).
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if len(lines) == expected and not lines[0].startswith("["):
        return lines
    raise RuntimeError(
        f"Could not parse batch translations as JSON (expected {expected}): {raw[:300]!r}"
    )


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
            absolute = start
            previous_en = cue_texts[absolute - 1] if absolute > 0 else None
            next_en = (
                cue_texts[absolute + 1] if absolute + 1 < len(cue_texts) else None
            )
            messages = build_single_cue_messages(
                batch[0],
                examples_per_cue[0],
                record_type=record_type,
                previous_en=previous_en,
                next_en=next_en,
            )
            translations.append(chat_completion(messages, config, session=http))
            continue
        messages = build_batch_messages(
            batch,
            examples_per_cue,
            record_type=record_type,
            all_document_cues=cue_texts,
            batch_start=start,
        )
        raw = chat_completion(messages, config, session=http)
        try:
            translations.extend(parse_batch_translations(raw, len(batch)))
        except RuntimeError:
            # Batch JSON still unusable — translate cues one at a time.
            for offset, (cue_en, examples) in enumerate(zip(batch, examples_per_cue)):
                absolute = start + offset
                previous_en = cue_texts[absolute - 1] if absolute > 0 else None
                next_en = (
                    cue_texts[absolute + 1]
                    if absolute + 1 < len(cue_texts)
                    else None
                )
                single_messages = build_single_cue_messages(
                    cue_en,
                    examples,
                    record_type=record_type,
                    previous_en=previous_en,
                    next_en=next_en,
                )
                translations.append(
                    chat_completion(single_messages, config, session=http)
                )
    polished = polish_subtitle_translations(cue_texts, translations)
    return [
        apply_translation_casing(text, record_type) for text in polished
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
    kind: Literal["title", "description", "caption"],
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
    elif kind == "caption":
        system = METADATA_CAPTION_SYSTEM_PROMPT
        field_label = "caption"
        instructions = (
            "Translate the English thumbnail caption into Bulgarian.\n"
            "Preserve the exact number of lines and line breaks.\n"
            "Do not add credits, hashtags, or logos.\n\n"
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
    kind: Literal["title", "description", "caption"],
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
    if kind in {"title", "caption"} and config.max_tokens > DEFAULT_METADATA_TITLE_MAX_TOKENS:
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
    if kind == "caption":
        translated = match_source_newlines(text, translated)
        translated = match_source_line_casing(text, translated)
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
