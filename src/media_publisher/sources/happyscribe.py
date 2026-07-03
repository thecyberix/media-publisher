from __future__ import annotations

from media_publisher.models import PublishJob


class HappyScribeError(RuntimeError):
    pass


def enrich_job_from_happyscribe(job: PublishJob, *, api_key: str) -> PublishJob:
    """Attach transcript or subtitle assets from HappyScribe."""
    raise NotImplementedError("HappyScribe integration is not implemented yet")
