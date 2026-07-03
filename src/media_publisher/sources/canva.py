from __future__ import annotations

from media_publisher.models import PublishJob


class CanvaError(RuntimeError):
    pass


def enrich_job_from_canva(job: PublishJob, *, client_id: str, client_secret: str) -> PublishJob:
    """Attach thumbnail or design assets from Canva."""
    raise NotImplementedError("Canva integration is not implemented yet")
