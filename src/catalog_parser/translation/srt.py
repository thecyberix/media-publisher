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
    # Windows text-mode writes can turn CRLF into CRCRLF (\r\r\n). Reading that
    # back with universal newlines then inserts a blank line between every row.
    normalized = (
        content.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    )
    raw_lines = [line.strip("\ufeff") for line in normalized.split("\n")]
    if not any(line.strip() for line in raw_lines):
        return []

    def skip_empty(index: int) -> int:
        while index < len(raw_lines) and not raw_lines[index].strip():
            index += 1
        return index

    cues: list[Cue] = []
    index = 0
    while True:
        index = skip_empty(index)
        if index >= len(raw_lines):
            break
        index_line = raw_lines[index].strip()
        if not index_line.isdigit():
            index += 1
            continue
        cue_index = int(index_line)
        index = skip_empty(index + 1)
        if index >= len(raw_lines):
            break
        timestamp_match = TIMESTAMP_PATTERN.match(raw_lines[index].strip())
        if timestamp_match is None:
            continue
        index += 1
        text_lines: list[str] = []
        while index < len(raw_lines):
            stripped = raw_lines[index].strip()
            if not stripped:
                peeked = skip_empty(index)
                if peeked >= len(raw_lines):
                    index = peeked
                    break
                if raw_lines[peeked].strip().isdigit():
                    after_index = skip_empty(peeked + 1)
                    if after_index < len(raw_lines) and TIMESTAMP_PATTERN.match(
                        raw_lines[after_index].strip()
                    ):
                        index = peeked
                        break
                index += 1
                continue
            if stripped.isdigit():
                after_index = skip_empty(index + 1)
                if after_index < len(raw_lines) and TIMESTAMP_PATTERN.match(
                    raw_lines[after_index].strip()
                ):
                    break
            text_lines.append(stripped)
            index += 1
        cues.append(
            Cue(
                index=cue_index,
                start=timestamp_match.group(1),
                end=timestamp_match.group(2),
                text="\n".join(text_lines).strip(),
            )
        )
    return cues


def apply_cue_timings(timing_cues: list[Cue], text_cues: list[Cue]) -> list[Cue]:
    """Copy start/end from ``timing_cues`` onto the text of ``text_cues``."""
    if len(timing_cues) != len(text_cues):
        raise ValueError(
            "Cannot copy timings: "
            f"{len(timing_cues)} timing cue(s) vs {len(text_cues)} text cue(s)"
        )
    return [
        Cue(
            index=text.index,
            start=timing.start,
            end=timing.end,
            text=text.text,
        )
        for timing, text in zip(timing_cues, text_cues)
    ]


def apply_retimed_timings_to_target(
    original_timing: list[Cue],
    retimed_timing: list[Cue],
    target: list[Cue],
) -> list[Cue]:
    """Map retimed EN cues onto BG cues, joining EN fragments when counts differ."""
    if len(original_timing) != len(retimed_timing):
        raise ValueError(
            "Original and retimed cue counts must match: "
            f"{len(original_timing)} vs {len(retimed_timing)}"
        )
    if len(original_timing) == len(target):
        return apply_cue_timings(retimed_timing, target)

    retimed_by_index = {
        original.index: retimed
        for original, retimed in zip(original_timing, retimed_timing)
    }
    sources_sorted = sorted(original_timing, key=_cue_sort_key)
    targets_sorted = sorted(target, key=_cue_sort_key)
    out: list[Cue] = []
    for index, target_cue in enumerate(targets_sorted):
        previous = targets_sorted[index - 1] if index > 0 else None
        matched_sources = source_cues_for_target(
            sources_sorted,
            target_cue,
            previous_target=previous,
        )
        retimed_matched = [
            retimed_by_index[source_cue.index]
            for source_cue in matched_sources
            if source_cue.index in retimed_by_index
        ]
        if not retimed_matched:
            out.append(target_cue)
            continue
        start_ms = min(timestamp_to_ms(item.start) for item in retimed_matched)
        end_ms = max(timestamp_to_ms(item.end) for item in retimed_matched)
        if end_ms < start_ms:
            end_ms = start_ms
        out.append(
            Cue(
                index=target_cue.index,
                start=ms_to_timestamp(start_ms),
                end=ms_to_timestamp(end_ms),
                text=target_cue.text,
            )
        )
    return out


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


def ms_to_timestamp(value: int) -> str:
    millis = max(0, int(value))
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def cue_interval_ms(cue: Cue) -> tuple[int, int]:
    return timestamp_to_ms(cue.start), timestamp_to_ms(cue.end)


@dataclass(frozen=True)
class CueTimingDelta:
    cue_index: int
    field: str
    expected: str
    actual: str
    delta_ms: int


def compare_cue_timings(
    expected: list[Cue],
    actual: list[Cue],
    *,
    tolerance_ms: int = 0,
) -> list[CueTimingDelta]:
    """Return start/end mismatches beyond tolerance. Empty list means a match."""
    deltas: list[CueTimingDelta] = []
    if len(expected) != len(actual):
        deltas.append(
            CueTimingDelta(
                cue_index=0,
                field="count",
                expected=str(len(expected)),
                actual=str(len(actual)),
                delta_ms=abs(len(actual) - len(expected)),
            )
        )
        return deltas
    for left, right in zip(expected, actual):
        if left.index != right.index:
            deltas.append(
                CueTimingDelta(
                    cue_index=left.index,
                    field="index",
                    expected=str(left.index),
                    actual=str(right.index),
                    delta_ms=0,
                )
            )
            continue
        for field, exp, act in (
            ("start", left.start, right.start),
            ("end", left.end, right.end),
        ):
            delta = timestamp_to_ms(act) - timestamp_to_ms(exp)
            if abs(delta) > tolerance_ms:
                deltas.append(
                    CueTimingDelta(
                        cue_index=left.index,
                        field=field,
                        expected=exp,
                        actual=act,
                        delta_ms=delta,
                    )
                )
    return deltas


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


def source_cues_for_target(
    source: list[Cue],
    target_cue: Cue,
    *,
    previous_target: Cue | None,
) -> list[Cue]:
    """EN cues owned by a BG cue (starts after the previous BG start, up to this one)."""
    prev_start_ms = timestamp_to_ms(previous_target.start) if previous_target else -1
    cur_start_ms = timestamp_to_ms(target_cue.start)
    return [
        source_cue
        for source_cue in sorted(source, key=_cue_sort_key)
        if prev_start_ms < timestamp_to_ms(source_cue.start) <= cur_start_ms
    ]


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

        previous = targets_sorted[index - 1] if index > 0 else None
        matched_sources = source_cues_for_target(
            sources_sorted,
            target_cue,
            previous_target=previous,
        )

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
