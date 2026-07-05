from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

PlatformName = Literal["youtube", "facebook", "instagram"]
VideoFormat = Literal["post", "short_form"]
ContentKind = Literal["video", "image"]


@dataclass
class PublishJob:
    """Normalized unit of work for a single video publish flow."""

    title: str
    description: str = ""
    video_path: str | None = None
    video_url: str | None = None
    thumbnail_path: str | None = None
    airtable_record_id: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    video_format: VideoFormat = "post"
    content_kind: ContentKind = "video"
    image_path: str | None = None
    publish_at: datetime | None = None
    privacy_status: str = "public"


@dataclass(frozen=True)
class PlatformScheduleTask:
    """One platform publish action derived from an Airtable catalog row."""

    platform: PlatformName
    publish_at: datetime
    job: PublishJob
    record_id: str
    record_fields: dict[str, object] = field(default_factory=dict)
