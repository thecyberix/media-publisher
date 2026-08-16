"""Quality heuristics for bilingual subtitle pairs."""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from catalog_parser.translation.srt import Cue, parse_srt

LATIN_RE = re.compile(r"[A-Za-z]")
DEFAULT_MIN_TARGET_SCRIPT_RATE = 0.80
DEFAULT_MAX_IDENTICAL_RATE = 0.20
# Back-compat names used by corpus export scripts.
DEFAULT_MIN_CYRILLIC_RATE = DEFAULT_MIN_TARGET_SCRIPT_RATE


@lru_cache(maxsize=4)
def _target_letter_re() -> re.Pattern[str]:
    from media_publisher.languages import selected_language

    pattern = selected_language().require_ingest().letter_pattern
    return re.compile(pattern)


def text_target_script_ratio(text: str) -> float:
    target_re = _target_letter_re()
    letters = target_re.findall(text) + LATIN_RE.findall(text)
    if not letters:
        return 0.0
    return len(target_re.findall(text)) / len(letters)


def text_cyrillic_ratio(text: str) -> float:
    return text_target_script_ratio(text)


def looks_mostly_target_script(text: str, *, min_ratio: float = 0.3) -> bool:
    return text_target_script_ratio(text) >= min_ratio


def looks_mostly_cyrillic(text: str, *, min_ratio: float = 0.3) -> bool:
    return looks_mostly_target_script(text, min_ratio=min_ratio)


def score_srt_text(content: str) -> dict[str, Any]:
    cues = parse_srt(content)
    if not cues:
        return {
            "cue_count": 0,
            "cyrillic_rate": 0.0,
            "empty_rate": 1.0,
        }
    cyrillic_cues = sum(1 for cue in cues if looks_mostly_target_script(cue.text))
    empty_cues = sum(1 for cue in cues if not cue.text.strip())
    return {
        "cue_count": len(cues),
        "cyrillic_rate": cyrillic_cues / len(cues),
        "empty_rate": empty_cues / len(cues),
    }


def scorecard_pair(source_srt: str, target_srt: str) -> dict[str, Any]:
    source = parse_srt(source_srt)
    target = parse_srt(target_srt)
    n = min(len(source), len(target))
    if n == 0:
        return {
            "source_cues": len(source),
            "target_cues": len(target),
            "identical_rate": 0.0,
            "target_cyrillic_rate": 0.0,
            "source_cyrillic_rate": 0.0,
        }

    identical = 0
    for index in range(n):
        if source[index].text.strip() == target[index].text.strip():
            identical += 1

    return {
        "source_cues": len(source),
        "target_cues": len(target),
        "identical_rate": identical / n,
        "target_cyrillic_rate": score_srt_text(target_srt)["cyrillic_rate"],
        "source_cyrillic_rate": score_srt_text(source_srt)["cyrillic_rate"],
    }


def passes_bilingual_gates(
    source_srt: str,
    target_srt: str,
    *,
    min_cyrillic_rate: float = DEFAULT_MIN_TARGET_SCRIPT_RATE,
    max_identical_rate: float = DEFAULT_MAX_IDENTICAL_RATE,
) -> tuple[bool, str]:
    card = scorecard_pair(source_srt, target_srt)
    if card["target_cues"] == 0:
        return False, "target SRT has no cues"
    if card["source_cues"] == 0:
        return False, "source SRT has no cues"
    if card["target_cyrillic_rate"] < min_cyrillic_rate:
        return (
            False,
            f"target script rate {card['target_cyrillic_rate']:.0%} "
            f"< {min_cyrillic_rate:.0%}",
        )
    if card["identical_rate"] > max_identical_rate:
        return (
            False,
            f"identical EN/BG rate {card['identical_rate']:.0%} "
            f"> {max_identical_rate:.0%}",
        )
    return True, "ok"


def filter_aligned_for_bulgarian(
    aligned: list[Any],
) -> list[Any]:
    """Drop empty, identical, or non-Cyrillic Bulgarian target cues."""
    kept = []
    for pair in aligned:
        source = (pair.source_text or "").strip()
        target = (pair.target_text or "").strip()
        if not source or not target:
            continue
        if source == target:
            continue
        if not looks_mostly_target_script(target):
            continue
        kept.append(pair)
    return kept
