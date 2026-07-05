"""Debug Instagram rupload via Facebook-hosted file_url."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

from media_publisher.__main__ import (
    PROJECT_ROOT,
    attach_local_video_path,
    resolve_meta_targets,
)
from media_publisher.config import load_settings
from media_publisher.publishers.meta import MetaClient
from media_publisher.sources.airtable import AirtableClient, record_to_publish_job

RECORD_ID = "recRdsBGlZGgSaKik"


def main() -> int:
    settings = load_settings(PROJECT_ROOT)
    page_id, ig_id, _ = resolve_meta_targets(settings)
    client = MetaClient(settings.meta_access_token or "", app_id=settings.meta_app_id)

    airtable = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    job = record_to_publish_job(airtable.get_record(RECORD_ID))
    attach_local_video_path(job, settings)
    video_path = Path(job.video_path)
    print(f"Video: {video_path}", flush=True)

    video_asset = client.upload_unpublished_video_url(page_id, video_path)
    print(f"Hosted URL: {video_asset.url[:80]}...", flush=True)

    container_id, upload_uri = client.create_instagram_resumable_reel_container(
        instagram_account_id=ig_id,
        caption=job.description or job.title,
    )
    print(f"container={container_id}", flush=True)
    print(f"upload_uri={upload_uri}", flush=True)

    response = requests.post(
        upload_uri,
        headers={
            "Authorization": f"OAuth {client.access_token}",
            "file_url": video_asset.url,
        },
        timeout=600,
    )
    print(f"rupload: HTTP {response.status_code} {response.text.strip()}", flush=True)
    if not response.ok:
        client.cleanup_hosting_assets(video_asset)
        return 1

    for i in range(120):
        status = client.get_container_status(container_id)
        print(f"poll {i}: {status.status_code} {status.status}", flush=True)
        if status.status_code == "FINISHED":
            media_id = client.publish_instagram_container(
                instagram_account_id=ig_id,
                container_id=container_id,
            )
            permalink = client.get_instagram_media_permalink(media_id)
            print(f"PUBLISHED {media_id} {permalink}", flush=True)
            delete = requests.delete(
                f"{client.api_base}/{client.api_version}/{media_id}",
                params={"access_token": client.access_token},
                timeout=60,
            )
            print(f"DELETE {delete.status_code} {delete.text.strip()}", flush=True)
            client.cleanup_hosting_assets(video_asset)
            return 0 if delete.ok else 2
        if status.status_code == "ERROR":
            detail = client._request(
                "GET",
                container_id,
                query={"fields": "status,status_code,video_status"},
            )
            print(f"ERROR detail: {detail}", flush=True)
            client.cleanup_hosting_assets(video_asset)
            return 1
        time.sleep(5)

    client.cleanup_hosting_assets(video_asset)
    print("Timed out waiting for FINISHED", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
