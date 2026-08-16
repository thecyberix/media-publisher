from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from media_publisher.languages import selected_language
from media_publisher.runtime_env import (
    DAILY_PLAYLIST_JSON_VARIABLE,
    DAILY_PLAYLIST_SLOTS_RELATIVE_PATH,
    daily_playlist_id_from_payload,
    load_publish_timing,
    materialize_credentials,
    parse_daily_playlist_payload,
)
from media_publisher.sources.airtable import apply_airtable_url_env


@dataclass(frozen=True)
class Settings:
    airtable_token: str
    airtable_base_id: str
    airtable_table_name: str
    airtable_api_base: str = "https://api.airtable.com/v0"
    airtable_view: str | None = None
    happyscribe_api_key: str | None = None
    happyscribe_api_base: str = "https://www.happyscribe.com/api/v1"
    happyscribe_url: str | None = None
    happyscribe_published_folder_id: str | None = None
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
    canva_url: str = ""
    canva_quotes_design_id: str | None = None
    quotes_publish_hour: int | None = None
    quotes_sources_config: str = "config/quotes_sources.json"
    translated_quotes_url: str = ""
    quotes_work_dir: str = "downloads/quotes"
    publish_timezone: str = ""
    videos_publish_hour: int | None = None
    youtube_client_secrets: str = "credentials/youtube-client.json"
    youtube_token: str = "credentials/youtube-token.json"
    youtube_channel_handle: str = ""
    smartlink_url: str = ""
    target_language: str = "bg"
    target_language_name: str = "Bulgarian"
    target_country: str = "България"
    youtube_short_cover_intro_seconds: float = 5.0
    youtube_playlist_id: str | None = None
    youtube_daily_playlist_id: str | None = None
    youtube_daily_playlist_slots: str = ""
    channel_report_mapping: str = "config/channel_report.json"
    channel_report_snapshots: str = "data/channel_report_snapshots.json"
    google_sheets_service_account: str = "credentials/google-sheets-service-account.json"
    drive_url: str = ""
    publish_override_thumbnails_subfolder: str = "Thumbnails"
    publish_override_videos_subfolder: str = "Videos"
    canva_published_subfolder_name: str = "Published"
    tn_original_thumbnail_dir: str = "downloads/original-thumbnails"
    tn_cache_dir: str = "downloads/tn-cache"
    tn_render_output_dir: str = "downloads/tn-rendered"
    tn_english_override_file: str = "downloads/tn-english-overrides.json"
    thumbnail_review_approved_subfolder: str = "Approved"
    publish_media_download_dir: str = "downloads/publish-media"
    meta_access_token: str | None = None
    meta_page_username: str = ""
    meta_instagram_username: str = ""
    meta_app_id: str | None = None
    meta_app_secret: str | None = None
    meta_api_version: str = "v21.0"


def load_env_file(path: Path) -> None:
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())
    apply_airtable_url_env()


def _youtube_daily_playlist_settings(root: Path) -> tuple[str | None, str]:
    payload: dict | None = None
    env_raw = os.getenv(DAILY_PLAYLIST_JSON_VARIABLE, "").strip()
    if env_raw:
        payload = parse_daily_playlist_payload(env_raw)
    path = root / DAILY_PLAYLIST_SLOTS_RELATIVE_PATH
    if path.is_file():
        file_payload = parse_daily_playlist_payload(
            path.read_text(encoding="utf-8")
        )
        if file_payload:
            payload = {**(payload or {}), **file_payload}
    playlist_id = daily_playlist_id_from_payload(payload)
    if not playlist_id:
        return None, ""
    return playlist_id, DAILY_PLAYLIST_SLOTS_RELATIVE_PATH


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

    def optional_int(name: str) -> int | None:
        raw = os.getenv(name, "").strip()
        if not raw:
            return None
        return int(raw)

    intro_seconds_raw = (
        os.getenv("YOUTUBE_SHORT_COVER_INTRO_SECONDS", "").strip()
        or os.getenv("YOUTUBE_SHORT_COVER_END_SECONDS", "5").strip()
        or "5"
    )
    language = selected_language()
    daily_playlist_id, daily_playlist_slots = _youtube_daily_playlist_settings(root)
    publish_timing = load_publish_timing()

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
        happyscribe_url=optional("HAPPYSCRIBE_URL"),
        happyscribe_published_folder_id=optional("HAPPYSCRIBE_PUBLISHED_FOLDER_ID"),
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
        canva_url=os.getenv("CANVA_URL", "").strip(),
        canva_quotes_design_id=optional("CANVA_QUOTES_DESIGN_ID"),
        quotes_publish_hour=publish_timing.quotes_hour,
        quotes_sources_config=os.getenv(
            "QUOTES_SOURCES_CONFIG", "config/quotes_sources.json"
        ).strip()
        or "config/quotes_sources.json",
        translated_quotes_url=os.getenv("TRANSLATED_QUOTES_URL", "").strip(),
        quotes_work_dir=os.getenv("QUOTES_WORK_DIR", "downloads/quotes").strip()
        or "downloads/quotes",
        publish_timezone=publish_timing.timezone,
        videos_publish_hour=publish_timing.videos_hour,
        youtube_client_secrets=os.getenv(
            "YOUTUBE_CLIENT_SECRETS", "credentials/youtube-client.json"
        ).strip()
        or "credentials/youtube-client.json",
        youtube_token=os.getenv("YOUTUBE_TOKEN", "credentials/youtube-token.json").strip()
        or "credentials/youtube-token.json",
        youtube_channel_handle=os.getenv("YOUTUBE_CHANNEL_HANDLE", "").strip(),
        smartlink_url=os.getenv("SMARTLINK_URL", "").strip(),
        target_language=language.alias,
        target_language_name=language.name,
        target_country=language.country,
        youtube_short_cover_intro_seconds=float(intro_seconds_raw),
        youtube_playlist_id=optional("YOUTUBE_PLAYLIST_ID"),
        youtube_daily_playlist_id=daily_playlist_id,
        youtube_daily_playlist_slots=daily_playlist_slots,
        channel_report_mapping=os.getenv(
            "CHANNEL_REPORT_MAPPING", "config/channel_report.json"
        ).strip()
        or "config/channel_report.json",
        channel_report_snapshots=os.getenv(
            "CHANNEL_REPORT_SNAPSHOTS", "data/channel_report_snapshots.json"
        ).strip()
        or "data/channel_report_snapshots.json",
        google_sheets_service_account=os.getenv(
            "GOOGLE_SHEETS_SERVICE_ACCOUNT",
            "credentials/google-sheets-service-account.json",
        ).strip()
        or "credentials/google-sheets-service-account.json",
        drive_url=os.getenv("DRIVE_URL", "").strip(),
        publish_override_thumbnails_subfolder=os.getenv(
            "PUBLISH_OVERRIDE_THUMBNAILS_SUBFOLDER",
            "Thumbnails",
        ).strip()
        or "Thumbnails",
        publish_override_videos_subfolder=os.getenv(
            "PUBLISH_OVERRIDE_VIDEOS_SUBFOLDER",
            "Videos",
        ).strip()
        or "Videos",
        canva_published_subfolder_name=os.getenv(
            "CANVA_PUBLISHED_SUBFOLDER_NAME",
            "Published",
        ).strip()
        or "Published",
        tn_original_thumbnail_dir=os.getenv(
            "TN_ORIGINAL_THUMBNAIL_DIR",
            "downloads/original-thumbnails",
        ).strip()
        or "downloads/original-thumbnails",
        tn_cache_dir=os.getenv("TN_CACHE_DIR", "downloads/tn-cache").strip()
        or "downloads/tn-cache",
        tn_render_output_dir=os.getenv(
            "TN_RENDER_OUTPUT_DIR",
            "downloads/tn-rendered",
        ).strip()
        or "downloads/tn-rendered",
        tn_english_override_file=os.getenv(
            "TN_ENGLISH_OVERRIDE_FILE",
            "downloads/tn-english-overrides.json",
        ).strip()
        or "downloads/tn-english-overrides.json",
        thumbnail_review_approved_subfolder=os.getenv(
            "THUMBNAIL_REVIEW_APPROVED_SUBFOLDER",
            "Approved",
        ).strip()
        or "Approved",
        publish_media_download_dir=os.getenv(
            "PUBLISH_MEDIA_DOWNLOAD_DIR",
            "downloads/publish-media",
        ).strip()
        or "downloads/publish-media",
        meta_access_token=optional("META_ACCESS_TOKEN"),
        meta_page_username=os.getenv("META_PAGE_USERNAME", "").strip(),
        meta_instagram_username=os.getenv("META_INSTAGRAM_USERNAME", "").strip(),
        meta_app_id=optional("META_APP_ID"),
        meta_app_secret=optional("META_APP_SECRET"),
        meta_api_version=os.getenv("META_API_VERSION", "v21.0").strip() or "v21.0",
    )


def missing_required_publish_settings(settings: Settings) -> list[str]:
    """Repo variables that must be set for publish workflows (no code defaults)."""
    missing: list[str] = []
    if not settings.airtable_token:
        missing.append("AIRTABLE_TOKEN")
    if not settings.airtable_base_id or not settings.airtable_table_name:
        missing.append("AIRTABLE_URL")
    if not settings.drive_url:
        missing.append("DRIVE_URL")
    if not settings.canva_url:
        missing.append("CANVA_URL")
    if not settings.happyscribe_url:
        missing.append("HAPPYSCRIBE_URL")
    if not settings.youtube_channel_handle:
        missing.append("YOUTUBE_CHANNEL_HANDLE")
    if not settings.meta_page_username:
        missing.append("META_PAGE_USERNAME")
    if not settings.meta_instagram_username:
        missing.append("META_INSTAGRAM_USERNAME")
    if (
        not settings.publish_timezone
        or settings.quotes_publish_hour is None
        or settings.videos_publish_hour is None
    ):
        missing.append("PUBLISH_JSON")
    if not settings.translated_quotes_url:
        missing.append("TRANSLATED_QUOTES_URL")
    if not settings.smartlink_url:
        missing.append("SMARTLINK_URL")
    try:
        selected_language()
    except Exception:
        missing.append("TARGET_LANGUAGE")
    return missing
