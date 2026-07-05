from __future__ import annotations

from datetime import date, datetime, timezone

from media_publisher.models import PlatformName, PlatformScheduleTask, PublishJob
from media_publisher.timezones import get_timezone

MIN_SCHEDULE_LEAD_SECONDS = 600
INSTAGRAM_PUBLISH_EARLY_SECONDS = 300
INSTAGRAM_PUBLISH_GRACE_SECONDS = 3600
FACEBOOK_MAX_SCHEDULE_LEAD_SECONDS = 60 * 60 * 24 * 30


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def instagram_is_due(publish_at: datetime, *, now: datetime | None = None) -> bool:
    """True when the runner should publish to Instagram now.

    Instagram has no reliable native schedule API. We publish shortly before
    ``publish_at`` and allow a grace window if a periodic runner was delayed.
    """
    current = as_utc(now or datetime.now(timezone.utc))
    target = as_utc(publish_at)
    delta = (current - target).total_seconds()
    return -INSTAGRAM_PUBLISH_EARLY_SECONDS <= delta <= INSTAGRAM_PUBLISH_GRACE_SECONDS


def platform_supports_native_schedule(platform: PlatformName) -> bool:
    return platform in {"youtube", "facebook"}


def is_platform_ready(
    platform: PlatformName,
    publish_at: datetime,
    *,
    now: datetime | None = None,
) -> bool:
    if platform == "instagram":
        return instagram_is_due(publish_at, now=now)
    return True


def filter_ready_tasks(
    tasks: list[PlatformScheduleTask],
    *,
    now: datetime | None = None,
) -> list[PlatformScheduleTask]:
    return [
        task
        for task in tasks
        if is_platform_ready(task.platform, task.publish_at, now=now)
    ]


def facebook_can_schedule(publish_at: datetime, *, now: datetime | None = None) -> bool:
    """True when Facebook accepts a native scheduled publish time for this post."""
    current = as_utc(now or datetime.now(timezone.utc))
    target = as_utc(publish_at)
    lead = (target - current).total_seconds()
    return MIN_SCHEDULE_LEAD_SECONDS <= lead <= FACEBOOK_MAX_SCHEDULE_LEAD_SECONDS


def facebook_wait_message(publish_at: datetime) -> str:
    return (
        f"waiting until {publish_at.isoformat()} "
        "(Facebook only accepts scheduled posts within 30 days — run --quotes again closer to that date)"
    )


def instagram_wait_message(publish_at: datetime) -> str:
    return (
        f"waiting until {publish_at.isoformat()} "
        "(Instagram has no native schedule API — use --watch or Task Scheduler)"
    )


def publish_local_date(publish_at: datetime, publish_timezone: str) -> date:
    return as_utc(publish_at).astimezone(get_timezone(publish_timezone)).date()


def filter_tasks_for_local_date(
    tasks: list[PlatformScheduleTask],
    target_date: date,
    *,
    publish_timezone: str,
) -> list[PlatformScheduleTask]:
    return [
        task
        for task in tasks
        if publish_local_date(task.publish_at, publish_timezone) == target_date
    ]


def prepare_job_for_immediate_publish(job: PublishJob, *, private: bool) -> None:
    """Clear native platform scheduling and optionally mark content as private/unpublished."""
    job.publish_at = None
    if private:
        job.privacy_status = "private"
