"""One-off Instagram upload test for a catalog record; deletes on success or failure."""
from __future__ import annotations

import sys
from pathlib import Path

import requests

from media_publisher.__main__ import (
    PROJECT_ROOT,
    attach_local_video_path,
    canva_client_from_settings,
    canva_download_dir_from_settings,
    canva_settings_missing,
    meta_client_from_settings,
    resolve_meta_targets,
    template_urls_from_settings,
)
from media_publisher.config import load_settings
from media_publisher.models import PublishJob
from media_publisher.post_templates import prepare_publish_job
from media_publisher.publishers.instagram import InstagramPublishError
from media_publisher.publishers.meta import MetaClient, MetaError
from media_publisher.sources.airtable import AirtableClient, record_to_publish_job
from media_publisher.sources.canva import CanvaError, ensure_catalog_thumbnail_from_canva
from media_publisher.video_duration import (
    InstagramVideoPrepError,
    reencode_instagram_upload_video,
)

RECORD_ID = "recRdsBGlZGgSaKik"
REENCODE_MAX_BYTES = 25 * 1024 * 1024


def _build_caption(job: PublishJob) -> str:
    return job.description.strip() or job.title


def _delete_graph_object(client: MetaClient, object_id: str, *, label: str) -> bool:
    url = f"{client.api_base}/{client.api_version}/{object_id}"
    response = requests.delete(
        url,
        params={"access_token": client.access_token},
        timeout=60,
    )
    print(f"{label} DELETE {object_id}: HTTP {response.status_code} {response.text.strip()}", flush=True)
    return response.ok


def _cleanup_test_artifacts(
    client: MetaClient,
    *,
    media_id: str | None,
    hosted_asset_ids: list[str],
) -> None:
    if media_id:
        if not _delete_graph_object(client, media_id, label="Instagram media"):
            permalink = client.get_instagram_media_permalink(media_id)
            print(f"Instagram post may still be live at {permalink}", flush=True)
    for asset_id in hosted_asset_ids:
        _delete_graph_object(client, asset_id, label="Facebook host asset")


def main() -> int:
    print("Starting Instagram re-encode upload test...", flush=True)
    settings = load_settings(PROJECT_ROOT)
    page_id, ig_id, _page_info = resolve_meta_targets(settings)
    client = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    record = client.get_record(RECORD_ID)
    job = record_to_publish_job(record)
    attach_local_video_path(job, settings)
    source_path = Path(job.video_path)
    print(f"Source video: {source_path}", flush=True)

    if not canva_settings_missing(settings):
        try:
            canva = canva_client_from_settings(settings)
            job = ensure_catalog_thumbnail_from_canva(
                job,
                client=canva,
                download_dir=canva_download_dir_from_settings(settings),
                long_catalog_url=settings.canva_long_video_thumbnails_url,
                short_catalog_url=settings.canva_short_video_thumbnails_url,
            )
            print(f"Thumbnail: {job.thumbnail_path}", flush=True)
        except CanvaError as exc:
            print(f"Thumbnail skipped: {exc}", flush=True)

    try:
        reencoded_path = reencode_instagram_upload_video(
            source_path,
            ffmpeg_path=settings.happyscribe_ffmpeg,
            max_bytes=REENCODE_MAX_BYTES,
            force=True,
        )
    except InstagramVideoPrepError as exc:
        print(f"REENCODE_FAILED: {exc}", flush=True)
        return 1

    reencoded_size_mb = reencoded_path.stat().st_size / (1024 * 1024)
    print(f"Re-encoded video: {reencoded_path} ({reencoded_size_mb:.1f} MB)", flush=True)

    job = prepare_publish_job(
        job,
        "instagram",
        **template_urls_from_settings(settings),
    )
    caption = _build_caption(job)
    cover_path = None  # skip cover for upload isolation during re-encode test

    meta = meta_client_from_settings(settings)
    media_id: str | None = None
    hosted_asset_ids: list[str] = []
    exit_code = 1

    try:
        print("Uploading re-encoded video to Instagram via rupload...", flush=True)
        media_id = meta.schedule_instagram_reel(
            instagram_account_id=ig_id,
            caption=caption,
            video_path=reencoded_path,
            page_id=page_id,
            cover_path=cover_path,
            prefer_resumable_upload=True,
        )
        permalink = meta.get_instagram_media_permalink(media_id)
        print(f"UPLOAD_OK: {media_id} {permalink}", flush=True)
        exit_code = 0
    except (InstagramPublishError, MetaError) as exc:
        print(f"UPLOAD_FAILED: {exc}", flush=True)
    finally:
        print("Cleaning up test publish artifacts...", flush=True)
        _cleanup_test_artifacts(
            meta,
            media_id=media_id,
            hosted_asset_ids=hosted_asset_ids,
        )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
