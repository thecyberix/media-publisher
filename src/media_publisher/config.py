from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from media_publisher.runtime_env import materialize_credentials


@dataclass(frozen=True)
class Settings:
    airtable_token: str
    airtable_base_id: str
    airtable_table_name: str
    airtable_api_base: str = "https://api.airtable.com/v0"
    airtable_view: str | None = None
    happyscribe_api_key: str | None = None
    happyscribe_api_base: str = "https://www.happyscribe.com/api/v1"
    happyscribe_organization_id: str | None = None
    happyscribe_folder_id: str | None = None
    happyscribe_library_url: str | None = None
    happyscribe_download_dir: str = "downloads/happyscribe"
    happyscribe_ffmpeg: str | None = None
    happyscribe_browser_state: str = "credentials/happyscribe-browser.json"
    happyscribe_browser_profile: str = "credentials/happyscribe-browser-profile"
    happyscribe_browser_channel: str = "chrome"
    happyscribe_email: str | None = None
    happyscribe_password: str | None = None
    canva_client_id: str | None = None
    canva_client_secret: str | None = None
    canva_token: str = "credentials/canva-token.json"
    canva_redirect_uri: str = "http://127.0.0.1:8765/callback"
    canva_api_base: str = "https://api.canva.com/rest/v1"
    canva_download_dir: str = "downloads/canva"
    canva_long_video_thumbnails_url: str = (
        "https://www.canva.com/folder/FAHOgLx_jAw"
    )
    canva_short_video_thumbnails_url: str = (
        "https://www.canva.com/folder/FAHOgF-NT8Q"
    )
    canva_quotes_design_id: str | None = None
    canva_quotes_folder_id: str = "https://www.canva.com/folder/FAHOgWUCQqs"
    quotes_publish_timezone: str = "Europe/Sofia"
    quotes_publish_hour: int = 8
    publish_timezone: str = "Europe/Sofia"
    publish_hour: int = 18
    youtube_client_secrets: str = "credentials/youtube-client.json"
    youtube_token: str = "credentials/youtube-token.json"
    youtube_channel_handle: str = "SadhguruBulgarian"
    youtube_channel_url: str = "https://www.youtube.com/channel/UCg8jXnEr8ZKmuwm3S9J4e-Q"
    youtube_short_cover_end_seconds: float = 2.0
    meta_access_token: str | None = None
    meta_page_id: str | None = None
    meta_instagram_account_id: str | None = None
    meta_page_username: str = "SadhguruBulgarian"
    meta_instagram_username: str = "sadhguru.bulgarian"
    meta_app_id: str | None = None
    meta_app_secret: str | None = None
    meta_api_version: str = "v21.0"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def update_env_values(path: Path, updates: dict[str, str]) -> None:
    if not updates:
        return

    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    remaining = dict(updates)
    updated_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in remaining:
                updated_lines.append(f"{key}={remaining.pop(key)}")
                continue
        updated_lines.append(line)

    for key, value in remaining.items():
        updated_lines.append(f"{key}={value}")

    path.write_text("\n".join(updated_lines).rstrip() + "\n", encoding="utf-8")


def load_settings(project_root: Path | None = None) -> Settings:
    root = project_root or Path(__file__).resolve().parents[2]
    load_env_file(root / ".env")
    materialize_credentials(root)

    def optional(name: str) -> str | None:
        value = os.getenv(name, "").strip()
        return value or None

    return Settings(
        airtable_token=os.getenv("AIRTABLE_TOKEN", "").strip(),
        airtable_base_id=os.getenv("AIRTABLE_BASE_ID", "").strip(),
        airtable_table_name=os.getenv("AIRTABLE_TABLE_NAME", "").strip(),
        airtable_api_base=os.getenv("AIRTABLE_API_BASE", "https://api.airtable.com/v0").strip()
        or "https://api.airtable.com/v0",
        airtable_view=optional("AIRTABLE_VIEW"),
        happyscribe_api_key=optional("HAPPYSCRIBE_API_KEY"),
        happyscribe_api_base=os.getenv(
            "HAPPYSCRIBE_API_BASE", "https://www.happyscribe.com/api/v1"
        ).strip()
        or "https://www.happyscribe.com/api/v1",
        happyscribe_organization_id=optional("HAPPYSCRIBE_ORGANIZATION_ID"),
        happyscribe_folder_id=optional("HAPPYSCRIBE_FOLDER_ID"),
        happyscribe_library_url=optional("HAPPYSCRIBE_LIBRARY_URL"),
        happyscribe_download_dir=os.getenv(
            "HAPPYSCRIBE_DOWNLOAD_DIR", "downloads/happyscribe"
        ).strip()
        or "downloads/happyscribe",
        happyscribe_ffmpeg=optional("HAPPYSCRIBE_FFMPEG"),
        happyscribe_browser_state=os.getenv(
            "HAPPYSCRIBE_BROWSER_STATE", "credentials/happyscribe-browser.json"
        ).strip()
        or "credentials/happyscribe-browser.json",
        happyscribe_browser_profile=os.getenv(
            "HAPPYSCRIBE_BROWSER_PROFILE", "credentials/happyscribe-browser-profile"
        ).strip()
        or "credentials/happyscribe-browser-profile",
        happyscribe_browser_channel=os.getenv("HAPPYSCRIBE_BROWSER_CHANNEL", "chrome").strip()
        or "chrome",
        happyscribe_email=optional("HAPPYSCRIBE_EMAIL"),
        happyscribe_password=optional("HAPPYSCRIBE_PASSWORD"),
        canva_client_id=optional("CANVA_CLIENT_ID"),
        canva_client_secret=optional("CANVA_CLIENT_SECRET"),
        canva_token=os.getenv("CANVA_TOKEN", "credentials/canva-token.json").strip()
        or "credentials/canva-token.json",
        canva_redirect_uri=os.getenv(
            "CANVA_REDIRECT_URI", "http://127.0.0.1:8765/callback"
        ).strip()
        or "http://127.0.0.1:8765/callback",
        canva_api_base=os.getenv("CANVA_API_BASE", "https://api.canva.com/rest/v1").strip()
        or "https://api.canva.com/rest/v1",
        canva_download_dir=os.getenv("CANVA_DOWNLOAD_DIR", "downloads/canva").strip()
        or "downloads/canva",
        canva_long_video_thumbnails_url=os.getenv(
            "CANVA_LONG_VIDEO_THUMBNAILS_URL",
            "https://www.canva.com/folder/FAHOgLx_jAw",
        ).strip()
        or "https://www.canva.com/folder/FAHOgLx_jAw",
        canva_short_video_thumbnails_url=os.getenv(
            "CANVA_SHORT_VIDEO_THUMBNAILS_URL",
            "https://www.canva.com/folder/FAHOgF-NT8Q",
        ).strip()
        or "https://www.canva.com/folder/FAHOgF-NT8Q",
        canva_quotes_design_id=optional("CANVA_QUOTES_DESIGN_ID"),
        canva_quotes_folder_id=os.getenv(
            "CANVA_QUOTES_FOLDER_ID",
            "https://www.canva.com/folder/FAHOgWUCQqs",
        ).strip()
        or "https://www.canva.com/folder/FAHOgWUCQqs",
        quotes_publish_timezone=os.getenv("QUOTES_PUBLISH_TIMEZONE", "Europe/Sofia").strip()
        or "Europe/Sofia",
        quotes_publish_hour=int(os.getenv("QUOTES_PUBLISH_HOUR", "8").strip() or "8"),
        publish_timezone=os.getenv("PUBLISH_TIMEZONE", "Europe/Sofia").strip()
        or "Europe/Sofia",
        publish_hour=int(os.getenv("PUBLISH_HOUR", "18").strip() or "18"),
        youtube_client_secrets=os.getenv(
            "YOUTUBE_CLIENT_SECRETS", "credentials/youtube-client.json"
        ).strip()
        or "credentials/youtube-client.json",
        youtube_token=os.getenv("YOUTUBE_TOKEN", "credentials/youtube-token.json").strip()
        or "credentials/youtube-token.json",
        youtube_channel_handle=os.getenv(
            "YOUTUBE_CHANNEL_HANDLE", "SadhguruBulgarian"
        ).strip()
        or "SadhguruBulgarian",
        youtube_channel_url=os.getenv(
            "YOUTUBE_CHANNEL_URL",
            "https://www.youtube.com/channel/UCg8jXnEr8ZKmuwm3S9J4e-Q",
        ).strip()
        or "https://www.youtube.com/channel/UCg8jXnEr8ZKmuwm3S9J4e-Q",
        youtube_short_cover_end_seconds=float(
            os.getenv("YOUTUBE_SHORT_COVER_END_SECONDS", "2").strip() or "2"
        ),
        meta_access_token=optional("META_ACCESS_TOKEN"),
        meta_page_id=optional("META_PAGE_ID"),
        meta_instagram_account_id=optional("META_INSTAGRAM_ACCOUNT_ID"),
        meta_page_username=os.getenv("META_PAGE_USERNAME", "SadhguruBulgarian").strip()
        or "SadhguruBulgarian",
        meta_instagram_username=os.getenv(
            "META_INSTAGRAM_USERNAME", "sadhguru.bulgarian"
        ).strip()
        or "sadhguru.bulgarian",
        meta_app_id=optional("META_APP_ID"),
        meta_app_secret=optional("META_APP_SECRET"),
        meta_api_version=os.getenv("META_API_VERSION", "v21.0").strip() or "v21.0",
    )
