"""SRT parse/align helpers for bilingual subtitle corpora."""
from __future__ import annotations

import re
from dataclasses import dataclass

TIMESTAMP_ARROW = "-->"
TIMESTAMP_PATTERN = re.compile(
    r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})"
)


@dataclass(frozen=True)
class Cue:
    index: int
    start: str
    end: str
    text: str


@dataclass(frozen=True)
class AlignedCue:
    cue_index: int
    start: str
    end: str
    source_text: str
    target_text: str


def parse_srt(content: str) -> list[Cue]:
    """Parse SRT content into cues. Empty or whitespace-only input returns []."""
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    blocks = re.split(r"\n\s*\n", normalized)
    cues: list[Cue] = []

    for block in blocks:
        lines = [line.strip("\ufeff") for line in block.split("\n") if line.strip()]
        if len(lines) < 2:
            continue

        index_line = lines[0]
        if not index_line.isdigit():
            continue
        index = int(index_line)

        timestamp_match = TIMESTAMP_PATTERN.match(lines[1])
        if timestamp_match is None:
            continue

        text = "\n".join(lines[2:]).strip()
        cues.append(
            Cue(
                index=index,
                start=timestamp_match.group(1),
                end=timestamp_match.group(2),
                text=text,
            )
        )

    return cues


def write_srt(cues: list[Cue]) -> str:
    blocks: list[str] = []
    for cue in cues:
        blocks.append(
            f"{cue.index}\n{cue.start} {TIMESTAMP_ARROW} {cue.end}\n{cue.text}".rstrip()
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def timestamp_to_ms(value: str) -> int:
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(",")
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(seconds) * 1_000
        + int(millis)
    )


def cue_interval_ms(cue: Cue) -> tuple[int, int]:
    return timestamp_to_ms(cue.start), timestamp_to_ms(cue.end)


def _join_cue_texts(cues: list[Cue]) -> str:
    parts: list[str] = []
    for cue in cues:
        text = " ".join(cue.text.split())
        if text:
            parts.append(text)
    return " ".join(parts).strip()


def _cue_sort_key(cue: Cue) -> tuple[int, int, int]:
    start_ms, end_ms = cue_interval_ms(cue)
    return start_ms, end_ms, cue.index


def align_cues(
    source: list[Cue],
    target: list[Cue],
) -> tuple[list[AlignedCue], list[str]]:
    """
    Align EN source cues to BG target cues for Smartcat confirmed exports.

    Confirmed BG segments often keep the *last* EN fragment's timestamps while
    the BG text translates several preceding EN fragments. We therefore:

    1. Sort both sides by start time
    2. For each BG cue, join every EN cue whose start is after the previous BG
       cue's start and at or before this BG cue's start
    """
    issues: list[str] = []
    if not source or not target:
        issues.append("empty source or target cue list")
        return [], issues

    sources_sorted = sorted(source, key=_cue_sort_key)
    targets_sorted = sorted(target, key=_cue_sort_key)

    aligned: list[AlignedCue] = []
    unmatched_target = 0
    multi_source = 0
    used_source_indexes: set[int] = set()

    for index, target_cue in enumerate(targets_sorted):
        if not target_cue.text.strip():
            issues.append(f"target cue {target_cue.index}: empty text")
            continue

        prev_start_ms = (
            timestamp_to_ms(targets_sorted[index - 1].start) if index > 0 else -1
        )
        cur_start_ms = timestamp_to_ms(target_cue.start)
        matched_sources = [
            source_cue
            for source_cue in sources_sorted
            if prev_start_ms < timestamp_to_ms(source_cue.start) <= cur_start_ms
        ]

        if not matched_sources:
            unmatched_target += 1
            continue

        source_text = _join_cue_texts(matched_sources)
        if not source_text:
            issues.append(f"target cue {target_cue.index}: empty joined source text")
            continue

        if len(matched_sources) > 1:
            multi_source += 1
        for source_cue in matched_sources:
            used_source_indexes.add(id(source_cue))

        aligned.append(
            AlignedCue(
                cue_index=target_cue.index,
                start=target_cue.start,
                end=target_cue.end,
                source_text=source_text,
                target_text=target_cue.text,
            )
        )

    unused_sources = len(sources_sorted) - len(used_source_indexes)
    if unmatched_target:
        issues.append(f"{unmatched_target} target cue(s) had no source match")
    if multi_source:
        issues.append(f"{multi_source} target cue(s) joined multiple source cues")
    if unused_sources:
        issues.append(f"{unused_sources} source cue(s) were not assigned")
    if len(source) != len(target):
        issues.append(f"cue count differs: source={len(source)} target={len(target)}")

    return aligned, issues
