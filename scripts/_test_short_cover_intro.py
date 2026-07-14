"""Generate a YouTube Short cover-intro test file for one catalog reel."""
from __future__ import annotations

import os
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_env() -> None:
    env = PROJECT_ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    load_env()
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

    from catalog_parser.runtime_env import materialize_credentials

    materialize_credentials(PROJECT_ROOT)

    from media_publisher.config import load_settings
    from media_publisher.sources.airtable import (
        AirtableClient,
        FIELD_ORIGINAL_VIDEO_THUMBNAIL,
        FIELD_TITLE,
        FIELD_TYPE,
        FIELD_TRANSLATION_RESOURCES,
    )
    from media_publisher.sources.happyscribe import (
        ensure_catalog_video_downloaded,
        find_downloaded_video,
    )
    from media_publisher.__main__ import (
        happyscribe_client_from_settings,
        happyscribe_library_from_settings,
    )
    from media_publisher.sources.image_video import ensure_short_with_cover_intro
    from media_publisher.publishers.youtube import prepare_youtube_thumbnail

    settings = load_settings()
    client = AirtableClient(
        token=settings.airtable_token,
        base_id=settings.airtable_base_id,
        table_name=settings.airtable_table_name,
    )

    chosen = None
    candidates: list[tuple[int, object]] = []
    for record in client.iter_records():
        fields = record.fields
        if fields.get(FIELD_TYPE) != "Reel":
            continue
        thumb = fields.get(FIELD_ORIGINAL_VIDEO_THUMBNAIL)
        if not isinstance(thumb, list) or not thumb:
            continue
        url = thumb[0].get("url") if isinstance(thumb[0], dict) else None
        if not url:
            continue
        status = str(fields.get("Status") or "")
        score = 0
        if "Synchronization done" in status:
            score += 100
        if "Translation done" in status:
            score += 50
        if "Editing done" in status:
            score += 30
        candidates.append((score, record))

    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        chosen = candidates[0][1]

    if chosen is None:
        print("No Reel with Original Video Thumbnail found.")
        return 1

    title = chosen.fields.get(FIELD_TITLE) or chosen.id
    print(f"Selected: {chosen.id}\t{title}")
    print(f"Status: {chosen.fields.get('Status')}")

    output_dir = PROJECT_ROOT / "output" / "short-cover-intro-test"
    output_dir.mkdir(parents=True, exist_ok=True)

    thumb_url = chosen.fields[FIELD_ORIGINAL_VIDEO_THUMBNAIL][0]["url"]
    raw_thumb = output_dir / "thumbnail-source.jpg"
    print(f"Downloading thumbnail...")
    urllib.request.urlretrieve(thumb_url, raw_thumb)

    happyscribe = happyscribe_client_from_settings(settings)
    location = happyscribe_library_from_settings(settings)
    download_dir = Path(settings.happyscribe_download_dir)
    transcriptions = happyscribe.list_search_transcriptions(location)
    smartcat_url = chosen.fields.get(FIELD_TRANSLATION_RESOURCES)

    print("Downloading/burning subtitled video from HappyScribe...")
    cached = find_downloaded_video(download_dir, str(title))
    if cached is not None:
        video_path = cached
        print(f"Using cached video: {video_path}")
    else:
        video_path = ensure_catalog_video_downloaded(
            str(title),
            download_dir=download_dir,
            client=happyscribe,
            location=location,
            browser_state_path=settings.happyscribe_browser_state,
            browser_profile_dir=settings.happyscribe_browser_profile,
            browser_channel=settings.happyscribe_browser_channel,
            api_key=settings.happyscribe_api_key,
            headless=True,
            transcriptions=transcriptions,
            force_regenerate=False,
            use_web_export=False,
            smartcat_url=str(smartcat_url) if smartcat_url else None,
        )
    print(f"Subtitled video: {video_path}")

    prepared_thumb = prepare_youtube_thumbnail(
        raw_thumb,
        short_form=True,
        ffmpeg_path=settings.happyscribe_ffmpeg,
    )
    print(f"Prepared thumbnail: {prepared_thumb}")

    baked = ensure_short_with_cover_intro(
        video_path,
        prepared_thumb,
        ffmpeg_path=settings.happyscribe_ffmpeg,
        intro_seconds=settings.youtube_short_cover_intro_seconds,
    )
    print(f"Cover intro video: {baked}")
    print(f"Duration intro: {settings.youtube_short_cover_intro_seconds}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
