from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from catalog_parser.parser import TYPE_REEL, TYPE_VIDEO
from catalog_parser.workflow.status_history import (
    DEFAULT_HISTORY_PATH,
    StatusWorkEvent,
    load_status_history,
)

REPORT_TIMEZONE = timezone(timedelta(hours=3))
VIDEO_TYPES = (TYPE_VIDEO, TYPE_REEL)
WorkKind = Literal["translator", "editor"]


@dataclass(frozen=True)
class WeekRange:
    start: datetime
    end: datetime
    label: str


@dataclass(frozen=True)
class WorkEvent:
    participant_name: str
    participant_id: str
    record_id: str
    record_title: str
    record_type: str
    duration_seconds: int
    kind: WorkKind
    created_time: datetime


@dataclass
class ParticipantSummary:
    participant_name: str
    participant_id: str
    videos: int = 0
    reels: int = 0
    seconds: int = 0
    record_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class WeeklyWorkReport:
    week: WeekRange
    events_scanned: int
    translation_events: tuple[WorkEvent, ...]
    editing_events: tuple[WorkEvent, ...]

    @property
    def records_scanned(self) -> int:
        return self.events_scanned

    @property
    def translation_by_participant(self) -> list[ParticipantSummary]:
        return _summarize_by_participant(self.translation_events)

    @property
    def editing_by_participant(self) -> list[ParticipantSummary]:
        return _summarize_by_participant(self.editing_events)


def previous_calendar_week_range(
    *,
    tz: timezone = REPORT_TIMEZONE,
    reference: datetime | None = None,
) -> WeekRange:
    ref = reference or datetime.now(tz)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=tz)
    else:
        ref = ref.astimezone(tz)

    this_monday = ref.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=ref.weekday()
    )
    start = this_monday - timedelta(days=7)
    end = this_monday - timedelta(microseconds=1)
    label = f"{start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')} (UTC+3)"
    return WeekRange(start=start, end=end, label=label)


def parse_detected_at(value: str) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = value.strip()
        if parsed.endswith("Z"):
            parsed = parsed[:-1] + "+00:00"
        return datetime.fromisoformat(parsed).astimezone(REPORT_TIMEZONE)
    except ValueError:
        return None


def _in_week(created_time: datetime, week: WeekRange) -> bool:
    return week.start <= created_time <= week.end


def _status_event_to_work_event(event: StatusWorkEvent) -> WorkEvent | None:
    created_time = parse_detected_at(event.detected_at)
    if created_time is None:
        return None
    return WorkEvent(
        participant_name=event.participant_name,
        participant_id=event.participant_name,
        record_id=event.record_id,
        record_title=event.record_title,
        record_type=event.record_type,
        duration_seconds=event.duration_seconds,
        kind=event.kind,
        created_time=created_time,
    )


def collect_weekly_work_events_from_history(
    history_path: Path,
    *,
    week: WeekRange,
) -> tuple[list[WorkEvent], int]:
    history = load_status_history(history_path)
    events: list[WorkEvent] = []
    seen: set[tuple[str, str, WorkKind]] = set()

    for item in history:
        if item.record_type not in VIDEO_TYPES:
            continue
        work_event = _status_event_to_work_event(item)
        if work_event is None or not _in_week(work_event.created_time, week):
            continue

        dedupe_key = (work_event.participant_id, work_event.record_id, work_event.kind)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        events.append(work_event)

    return events, len(history)


def build_weekly_work_report(
    history_path: Path,
    *,
    week: WeekRange | None = None,
) -> WeeklyWorkReport:
    effective_week = week or previous_calendar_week_range()
    events, events_scanned = collect_weekly_work_events_from_history(
        history_path,
        week=effective_week,
    )
    translation = tuple(event for event in events if event.kind == "translator")
    editing = tuple(event for event in events if event.kind == "editor")
    return WeeklyWorkReport(
        week=effective_week,
        events_scanned=events_scanned,
        translation_events=translation,
        editing_events=editing,
    )


def build_weekly_work_report_for_project(
    project_root: Path,
    *,
    week: WeekRange | None = None,
    history_path: Path | None = None,
) -> WeeklyWorkReport:
    target = project_root / (history_path or DEFAULT_HISTORY_PATH)
    return build_weekly_work_report(target, week=week)


def _summarize_by_participant(events: tuple[WorkEvent, ...]) -> list[ParticipantSummary]:
    by_id: dict[str, ParticipantSummary] = {}
    for event in events:
        summary = by_id.get(event.participant_id)
        if summary is None:
            summary = ParticipantSummary(
                participant_name=event.participant_name,
                participant_id=event.participant_id,
            )
            by_id[event.participant_id] = summary
        if event.record_id in summary.record_ids:
            continue
        summary.record_ids.add(event.record_id)
        summary.seconds += event.duration_seconds
        if event.record_type == TYPE_VIDEO:
            summary.videos += 1
        elif event.record_type == TYPE_REEL:
            summary.reels += 1

    return sorted(
        by_id.values(),
        key=lambda item: (item.videos + item.reels, item.seconds),
        reverse=True,
    )


def _format_duration(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _format_participant_lines(summaries: list[ParticipantSummary]) -> list[str]:
    if not summaries:
        return ["  (none)"]
    lines: list[str] = []
    for item in summaries:
        total_records = item.videos + item.reels
        lines.append(
            f"  {item.participant_name}: {total_records} items "
            f"({item.videos} videos, {item.reels} reels), {_format_duration(item.seconds)}"
        )
    return lines


def _format_totals(summaries: list[ParticipantSummary]) -> str:
    videos = sum(item.videos for item in summaries)
    reels = sum(item.reels for item in summaries)
    seconds = sum(item.seconds for item in summaries)
    return f"{videos} videos, {reels} reels, {_format_duration(seconds)}"


def format_weekly_work_report_email(report: WeeklyWorkReport) -> tuple[str, str]:
    translation = report.translation_by_participant
    editing = report.editing_by_participant

    subject = f"catalog-parser weekly report ({report.week.label})"
    body_lines = [
        "Weekly translation & editing report",
        f"Period: {report.week.label}",
        f"Status history events scanned: {report.events_scanned}",
        "",
        "Counts are based on daily Airtable status snapshots:",
        "- Translation — record entered '2. Translation done' without an Editor",
        "- Editing — record entered '3. Editing done' without Combined Media File",
        "If a record jumps from '1. To do' to '3. Editing done' in one day,",
        "translation and editing are credited separately.",
        "Attribution uses the Translator / Editor fields on each record.",
        "",
        "TRANSLATION",
        *_format_participant_lines(translation),
        f"  Total: {_format_totals(translation)}",
        "",
        "EDITING",
        *_format_participant_lines(editing),
        f"  Total: {_format_totals(editing)}",
    ]

    detail_lines: list[str] = []
    if report.translation_events:
        detail_lines.extend(["", "Translation details:"])
        for event in sorted(report.translation_events, key=lambda item: item.created_time):
            detail_lines.append(
                f"  - {event.created_time.strftime('%Y-%m-%d %H:%M')} "
                f"{event.participant_name}: {event.record_title} ({event.record_type})"
            )
    if report.editing_events:
        detail_lines.extend(["", "Editing details:"])
        for event in sorted(report.editing_events, key=lambda item: item.created_time):
            detail_lines.append(
                f"  - {event.created_time.strftime('%Y-%m-%d %H:%M')} "
                f"{event.participant_name}: {event.record_title} ({event.record_type})"
            )

    body_lines.extend(detail_lines)
    return subject, "\n".join(body_lines)
