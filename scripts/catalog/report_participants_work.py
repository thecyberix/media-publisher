"""
One-off report: work done by each Airtable participant.

Logic:
- Consider Airtable records where Type is Video or Reel and Duration is set.
- For each record, scan record comments.
- A "readiness comment" is a comment whose text matches known ready markers
  (translator and editor). The comment's author is treated as the participant.
- For each participant, count unique processed records, split by Video/Reel,
  and sum their durations (seconds).
- For weekly buckets, for each participant we take their oldest readiness
  comment timestamp, then bucket each (participant, record) using the
  earliest readiness timestamp for that record by that participant:
    week_index = floor((t - oldest_t) / 7 days)
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(SCRIPTS_DIR))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from catalog_parser.__main__ import load_env_file
from catalog_parser.airtable import FIELD_DURATION, FIELD_TITLE, FIELD_TYPE, YT_DESCRIPTION_COMMENT_PREFIX
from catalog_parser.__main__ import build_parser

# Reuse the simple Airtable REST client from the other one-off scripts.
from check_missing_description_comments import AirtableApi

FIELD_STATUS = "Status"

STATUS_ALL = [
    "1. To do",
    "2. Translation done",
    "3. Editing done",
    "4. Problematic videos",
    "5. Synchronization done",
    "6. Done & Published",
    "7. Not Assigned",
]

TYPE_VIDEO = "Video"
TYPE_REEL = "Reel"
VIDEO_TYPES = (TYPE_VIDEO, TYPE_REEL)

# Comment prefixes that are clearly NOT readiness markers.
CONTENT_NOTE_PREFIXES = (
    "заглавие:",
    "съкратено заглавие:",
    "описание:",
    "редактирано заглавие:",
    "редакция на заглавието:",
    "редакция на загланието:",
)


def normalize_ws(text: str) -> str:
    return " ".join(text.strip().split())


def parse_airtable_datetime(value: str) -> datetime | None:
    # Airtable uses ISO like "2024-02-22T11:41:28.000Z"
    if not isinstance(value, str) or not value:
        return None
    try:
        v = value.strip()
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        return datetime.fromisoformat(v).astimezone(timezone.utc)
    except Exception:
        return None


def safe_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if int(value) != value and float(value).is_integer() is False:
            # Keep only clean integer seconds.
            return None
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # Remove commas etc.
        s = s.replace(",", "")
        if not re.fullmatch(r"-?\d+", s):
            return None
        return int(s)
    return None


def author_key(author: dict[str, Any] | None) -> tuple[str, str]:
    """
    Returns (display_name, stable_id-ish).
    Airtable author typically includes {id, email, name}.
    """
    if not isinstance(author, dict):
        return ("(unknown)", "(unknown)")
    name = author.get("name") or author.get("email") or author.get("id") or "(unknown)"
    stable = author.get("id") or author.get("email") or name
    return (str(name), str(stable))


@dataclass(frozen=True)
class ReadinessHit:
    participant_name: str
    participant_stable: str
    record_id: str
    record_type: str
    duration_seconds: int
    created_time: datetime
    kind: str  # "translator" | "editor"


def is_content_note(text: str) -> bool:
    lowered = text.strip().casefold()
    return any(lowered.startswith(prefix) for prefix in CONTENT_NOTE_PREFIXES)


# Patterns intentionally match standalone-ish short status comments.
TRANSLATOR_READY_PATTERNS = [
    r"готово\.?$",
    r"готов\.?$",  # sometimes used
    r"преведен\.?$",
    r"преведено\.?$",
    r"преводът е готов\.?$",
    r"превода е готов\.?$",
    r"готов превод\.?$",
    r"translation ready\.?$",
    r"translated\.?$",
    r"готово е\.?$",
    r"преведено е видеото\.?$",
    r"видеото е готово\.?$",
]

EDITOR_READY_PATTERNS = [
    r"редактирано е\.?$",
    r"редактирано\.?$",
    r"редактиран\.?$",
    r"видеото е редактирано\.?$",
    # English / common variants if present
    r"editing done\.?$",
    r"edited\.?$",
]

TRANSLATOR_READY_RE = re.compile("|".join(f"(?:{p})" for p in TRANSLATOR_READY_PATTERNS), re.IGNORECASE)
EDITOR_READY_RE = re.compile("|".join(f"(?:{p})" for p in EDITOR_READY_PATTERNS), re.IGNORECASE)


def classify_readiness(text: str) -> str | None:
    t = normalize_ws(text)
    if not t:
        return None
    if is_content_note(t):
        return None
    if TRANSLATOR_READY_RE.search(t) and re.fullmatch(TRANSLATOR_READY_RE.pattern, t, flags=re.IGNORECASE):
        return "translator"
    if EDITOR_READY_RE.search(t) and re.fullmatch(EDITOR_READY_RE.pattern, t, flags=re.IGNORECASE):
        return "editor"
    # Fallback: exact full-match against each pattern.
    for p in TRANSLATOR_READY_PATTERNS:
        if re.fullmatch(p, t, flags=re.IGNORECASE):
            return "translator"
    for p in EDITOR_READY_PATTERNS:
        if re.fullmatch(p, t, flags=re.IGNORECASE):
            return "editor"
    return None


def readiness_hits_for_record(
    *,
    api: AirtableApi,
    record: dict[str, Any],
) -> list[ReadinessHit]:
    record_id = record.get("id")
    fields = record.get("fields", {}) if isinstance(record.get("fields", {}), dict) else {}
    record_type = fields.get(FIELD_TYPE)
    duration_raw = fields.get(FIELD_DURATION)
    duration_seconds = safe_int(duration_raw)
    if not record_id or not record_type or duration_seconds is None:
        return []
    if record_type not in VIDEO_TYPES:
        return []

    hits: list[ReadinessHit] = []
    try:
        comments = api.list_comments(record_id)
    except Exception:
        return []

    for c in comments:
        text = c.get("text")
        if not isinstance(text, str):
            continue
        kind = classify_readiness(text)
        if not kind:
            continue
        created_time = parse_airtable_datetime(c.get("createdTime"))
        if created_time is None:
            continue
        author = c.get("author")
        pname, pstable = author_key(author)
        hits.append(
            ReadinessHit(
                participant_name=pname,
                participant_stable=pstable,
                record_id=str(record_id),
                record_type=str(record_type),
                duration_seconds=duration_seconds,
                created_time=created_time,
                kind=kind,
            )
        )
    return hits


def main() -> int:
    load_env_file(PROJECT_ROOT / ".env")

    token = os.getenv("AIRTABLE_TOKEN", "").strip()
    base_id = os.getenv("AIRTABLE_BASE_ID", "").strip()
    table_name = os.getenv("AIRTABLE_TABLE_NAME", "").strip()
    if not token or not base_id or not table_name:
        print("Missing AIRTABLE_TOKEN/AIRTABLE_BASE_ID/AIRTABLE_TABLE_NAME in .env")
        return 1

    api = AirtableApi(token, base_id, table_name)

    # We can't reliably filter on "duration is non-blank" across schemas,
    # so we filter by type, then skip missing Duration in code.
    records = api.list_records(
        filter_formula=f"OR({{{FIELD_TYPE}}}='{TYPE_VIDEO}',{{{FIELD_TYPE}}}='{TYPE_REEL}')"
    )
    print(f"Fetched {len(records)} records (Video/Reel). Scanning for readiness comments...")

    # For each participant, for each record, store earliest readiness timestamp for weekly bucketing.
    # Keep kind info to compute subtotals.
    earliest_by_part_record: dict[tuple[str, str, str], tuple[datetime, str]] = {}
    # Key: (participant_stable, participant_name, record_id)
    participant_meta: dict[str, str] = {}

    processed_records_per_participant: set[tuple[str, str]] = set()
    processed_video_records_per_participant: set[tuple[str, str]] = set()
    processed_reel_records_per_participant: set[tuple[str, str]] = set()
    duration_by_part_record: dict[tuple[str, str], int] = {}

    translator_duration_by_part: defaultdict[str, int] = defaultdict(int)
    editor_duration_by_part: defaultdict[str, int] = defaultdict(int)

    for idx, rec in enumerate(records, start=1):
        hits = readiness_hits_for_record(api=api, record=rec)
        if not hits:
            continue
        # Participant can have multiple readiness comments on the same record;
        # take the earliest created_time per (participant, record).
        for h in hits:
            pkey = (h.participant_stable, h.participant_name, h.record_id)
            participant_meta[h.participant_stable] = h.participant_name
            duration_by_part_record[(h.participant_stable, h.record_id)] = h.duration_seconds

            existing = earliest_by_part_record.get(pkey)
            if existing is None or h.created_time < existing[0]:
                earliest_by_part_record[pkey] = (h.created_time, h.kind)

            processed_records_per_participant.add((h.participant_stable, h.record_id))
            if h.record_type == TYPE_VIDEO:
                processed_video_records_per_participant.add((h.participant_stable, h.record_id))
            elif h.record_type == TYPE_REEL:
                processed_reel_records_per_participant.add((h.participant_stable, h.record_id))

            if h.kind == "translator":
                translator_duration_by_part[h.participant_stable] += h.duration_seconds
            elif h.kind == "editor":
                editor_duration_by_part[h.participant_stable] += h.duration_seconds

        if idx % 25 == 0:
            print(f"  Scanned {idx}/{len(records)} records...")

    # Build per-participant rollups.
    per_part_records: dict[str, list[tuple[str, datetime, str]]] = defaultdict(list)
    for (pst, pname, rid), (t, kind) in earliest_by_part_record.items():
        per_part_records[pst].append((rid, t, kind))

    participants = sorted(per_part_records.keys(), key=lambda pst: len(per_part_records[pst]), reverse=True)
    if not participants:
        print("No readiness comments found for Video/Reel records with set Duration.")
        return 0

    # Weekly aggregation (relative week index from each participant's oldest readiness comment).
    WEEK_SECONDS = 7 * 24 * 60 * 60
    weekly: dict[str, dict[int, dict[str, Any]]] = defaultdict(lambda: defaultdict(lambda: {"videos": 0, "reels": 0, "seconds": 0}))

    for pst in participants:
        recs = per_part_records[pst]
        oldest_t = min(t for _rid, t, _kind in recs)
        # Earliest per record by participant (already in per_part_records via earliest_by_part_record)
        for rid, t, _kind in recs:
            duration_seconds = duration_by_part_record.get((pst, rid))
            if duration_seconds is None:
                continue
            week_index = int((t - oldest_t).total_seconds() // WEEK_SECONDS)

            # Need record type for video/reel counts.
            # We can fetch by scanning record map; build a map lazily.
            # We'll build it now by using a dict from record_id -> record_type.
            # (Do this outside loop in simpler implementation by building map first)
            # Here, we fill a placeholder and later correct if missing.
            weekly[pst][week_index]["seconds"] += duration_seconds

    # Build record_id -> type for counting
    record_type_by_id: dict[str, str] = {}
    for rec in records:
        rid = rec.get("id")
        f = rec.get("fields", {}) if isinstance(rec.get("fields", {}), dict) else {}
        if rid and f.get(FIELD_TYPE):
            record_type_by_id[str(rid)] = str(f.get(FIELD_TYPE))

    for pst in participants:
        for week_index, w in list(weekly[pst].items()):
            w["videos"] = 0
            w["reels"] = 0

        recs = per_part_records[pst]
        oldest_t = min(t for _rid, t, _kind in recs)
        for rid, t, _kind in recs:
            duration_seconds = duration_by_part_record.get((pst, rid))
            if duration_seconds is None:
                continue
            week_index = int((t - oldest_t).total_seconds() // WEEK_SECONDS)
            rtype = record_type_by_id.get(rid)
            if rtype == TYPE_VIDEO:
                weekly[pst][week_index]["videos"] += 1
            elif rtype == TYPE_REEL:
                weekly[pst][week_index]["reels"] += 1

    # Also compute seconds totals split by participant's unique processed records.
    total_by_part_seconds: dict[str, int] = defaultdict(int)
    total_by_part_videos: dict[str, int] = defaultdict(int)
    total_by_part_reels: dict[str, int] = defaultdict(int)

    for (pst, rid) in processed_records_per_participant:
        ds = duration_by_part_record.get((pst, rid))
        if ds is None:
            continue
        total_by_part_seconds[pst] += ds
        rtype = record_type_by_id.get(rid)
        if rtype == TYPE_VIDEO:
            total_by_part_videos[pst] += 1
        elif rtype == TYPE_REEL:
            total_by_part_reels[pst] += 1

    # Print report.
    def fmt_seconds(s: int) -> str:
        return f"{s} sec"

    print("\n=== Participants work report (Video/Reel, Duration set) ===")
    for pst in participants:
        pname = participant_meta.get(pst, pst)
        videos_n = total_by_part_videos.get(pst, 0)
        reels_n = total_by_part_reels.get(pst, 0)
        total_n = videos_n + reels_n
        total_seconds = total_by_part_seconds.get(pst, 0)

        print(f"\nParticipant: {pname}")
        print(f"  Processed: {total_n} records ({videos_n} videos, {reels_n} reels)")
        print(f"  Total content: {fmt_seconds(total_seconds)}")
        # Weekly
        weeks = weekly.get(pst, {})
        for widx in sorted(weeks.keys()):
            w = weeks[widx]
            if w["videos"] == 0 and w["reels"] == 0:
                continue
            sec = w["seconds"]
            print(f"  Week {widx}: {w['videos']} videos, {w['reels']} reels, {fmt_seconds(sec)}")

    # Also save a JSON artifact for later.
    out = {
        "records_scanned": len(records),
        "participants": [
            {
                "participant_stable": pst,
                "participant_name": participant_meta.get(pst, pst),
                "processed_videos": total_by_part_videos.get(pst, 0),
                "processed_reels": total_by_part_reels.get(pst, 0),
                "processed_records_total": total_by_part_videos.get(pst, 0)
                + total_by_part_reels.get(pst, 0),
                "total_seconds": total_by_part_seconds.get(pst, 0),
                "weekly": {
                    str(widx): weekly[pst][widx] for widx in sorted(weekly.get(pst, {}).keys())
                },
                "translator_seconds_total": translator_duration_by_part.get(pst, 0),
                "editor_seconds_total": editor_duration_by_part.get(pst, 0),
            }
            for pst in participants
        ],
        "note": "Weekly indices are relative to each participant's oldest readiness comment timestamp.",
    }
    report_path = PROJECT_ROOT / "_tmp_participants_work_report.json"
    report_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved JSON: {report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

