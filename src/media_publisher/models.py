from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


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
    publish_at: datetime | None = None
    privacy_status: str = "public"
