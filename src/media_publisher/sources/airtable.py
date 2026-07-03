from __future__ import annotations

from typing import Any

from media_publisher.models import PublishJob


class AirtableError(RuntimeError):
    pass


def fetch_publish_jobs(
    *,
    token: str,
    base_id: str,
    table_name: str,
) -> list[PublishJob]:
    """Load records from Airtable and map them to publish jobs."""
    raise NotImplementedError("Airtable source integration is not implemented yet")
