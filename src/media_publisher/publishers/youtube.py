from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from media_publisher.models import PublishJob
from media_publisher.sources.image_video import (
    ImageVideoError,
    SHORT_COVER_INTRO_SECONDS,
    ensure_short_with_cover_intro,
)

API_BASE = "https://www.googleapis.com/youtube/v3"
UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8766/callback"
DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
)
DEFAULT_CHANNEL_HANDLE = "SadhguruBulgarian"
DEFAULT_YOUTUBE_PLAYLIST_TITLE = "Съзнателна Планета"
DEFAULT_DAILY_PLAYLIST_SLOTS_PATH = "data/youtube_daily_playlist_slots.json"
DAILY_PLAYLIST_SLOTS = ("quote", "reel", "lau")
DailyPlaylistSlot = Literal["quote", "reel", "lau"]
CHANNEL_HANDLE_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?youtube\.com/@([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
MIN_SCHEDULE_LEAD_SECONDS = 60
VIDEO_PROCESSING_POLL_INTERVAL_SECONDS = 5.0
VIDEO_PROCESSING_POLL_MAX_ATTEMPTS = 120
THUMBNAIL_SET_MAX_ATTEMPTS = 12
THUMBNAIL_SET_RETRY_SECONDS = 5.0


class YouTubePublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class YouTubeToken:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str | None = None
    token_type: str = "Bearer"


@dataclass(frozen=True)
class YouTubeClientSecrets:
    client_id: str
    client_secret: str
    redirect_uri: str


@dataclass(frozen=True)
class YouTubePendingAuth:
    state: str
    redirect_uri: str


@dataclass(frozen=True)
class YouTubeChannel:
    id: str
    title: str
    handle: str | None = None
    custom_url: str | None = None

    @property
    def url(self) -> str:
        if self.handle:
            return f"https://www.youtube.com/@{self.handle}"
        return f"https://www.youtube.com/channel/{self.id}"


def parse_channel_handle(value: str) -> str:
    text = value.strip()
    if not text:
        raise YouTubePublishError("YouTube channel handle is empty")

    match = CHANNEL_HANDLE_URL_RE.search(text)
    if match:
        return match.group(1)

    if text.startswith("@"):
        return text[1:]
    return text


def channel_handle_from_snippet(snippet: dict[str, Any]) -> str | None:
    custom_url = snippet.get("customUrl")
    if not isinstance(custom_url, str) or not custom_url.strip():
        return None
    return parse_channel_handle(custom_url)


def channel_from_api_item(item: dict[str, Any]) -> YouTubeChannel | None:
    channel_id = item.get("id")
    snippet = item.get("snippet")
    if not isinstance(channel_id, str) or not channel_id:
        return None
    if not isinstance(snippet, dict):
        snippet = {}

    title = snippet.get("title")
    custom_url = snippet.get("customUrl")
    handle = channel_handle_from_snippet(snippet)
    return YouTubeChannel(
        id=channel_id,
        title=title if isinstance(title, str) else "",
        handle=handle,
        custom_url=custom_url if isinstance(custom_url, str) else None,
    )


def format_channel_list(channels: list[YouTubeChannel]) -> str:
    if not channels:
        return "(none)"
    parts: list[str] = []
    for channel in channels:
        label = channel.title or channel.id
        if channel.handle:
            parts.append(f"{label} (@{channel.handle})")
        else:
            parts.append(label)
    return ", ".join(parts)


def _video_is_ready_for_thumbnail(item: dict[str, Any]) -> bool:
    status = item.get("status")
    processing = item.get("processingDetails")
    upload_status = status.get("uploadStatus") if isinstance(status, dict) else None
    processing_status = (
        processing.get("processingStatus") if isinstance(processing, dict) else None
    )
    if upload_status == "processed":
        return True
    if processing_status == "succeeded":
        return True
    return False


def _video_processing_failure(item: dict[str, Any]) -> str | None:
    status = item.get("status")
    processing = item.get("processingDetails")
    upload_status = status.get("uploadStatus") if isinstance(status, dict) else None
    processing_status = (
        processing.get("processingStatus") if isinstance(processing, dict) else None
    )
    if upload_status in {"failed", "rejected", "deleted"}:
        return f"uploadStatus={upload_status}"
    if processing_status in {"failed", "terminated"}:
        failure = (
            processing.get("processingFailureReason")
            if isinstance(processing, dict)
            else None
        )
        if isinstance(failure, str) and failure.strip():
            return failure.strip()
        return f"processingStatus={processing_status}"
    return None


def _thumbnail_retryable_error(detail: str) -> bool:
    lowered = detail.lower()
    return any(
        phrase in lowered
        for phrase in (
            "not been processed",
            "still being processed",
            "processing",
            "backenderror",
            "internalerror",
            "ratelimitexceeded",
            "userratelimitexceeded",
        )
    )


def prepare_youtube_thumbnail(
    thumbnail_path: Path,
    *,
    short_form: bool = False,
    ffmpeg_path: str | None = None,
) -> Path:
    """Normalize a thumbnail for YouTube upload (JPEG, Shorts-friendly 9:16 when needed)."""
    source = thumbnail_path.resolve()
    if not source.is_file():
        raise YouTubePublishError(f"Thumbnail file not found: {source}")

    if not short_form and source.suffix.lower() in {".jpg", ".jpeg"}:
        return source

    ffmpeg = ffmpeg_path or shutil.which("ffmpeg")
    if not ffmpeg:
        return source

    destination = source.with_name(f"{source.stem}.youtube-thumb.jpg")
    if destination.is_file() and destination.stat().st_mtime >= source.stat().st_mtime:
        return destination

    if short_form:
        vf = (
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920"
        )
    else:
        vf = (
            "scale=1280:720:force_original_aspect_ratio=increase,"
            "crop=1280:720"
        )

    command = [
        ffmpeg,
        "-y",
        "-i",
        str(source),
        "-vf",
        vf,
        "-q:v",
        "2",
        str(destination),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise YouTubePublishError(
            f"ffmpeg failed to prepare YouTube thumbnail for {source.name}: {detail}"
        ) from exc

    if not destination.is_file():
        raise YouTubePublishError(
            f"ffmpeg did not create YouTube thumbnail file: {destination}"
        )
    return destination


def generate_state() -> str:
    return secrets.token_urlsafe(48)


def format_publish_at(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def validate_schedule_time(publish_at: datetime) -> None:
    if publish_at.tzinfo is None:
        publish_at = publish_at.replace(tzinfo=timezone.utc)
    else:
        publish_at = publish_at.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if publish_at <= now:
        raise YouTubePublishError("publish_at must be in the future")
    lead_seconds = (publish_at - now).total_seconds()
    if lead_seconds < MIN_SCHEDULE_LEAD_SECONDS:
        raise YouTubePublishError(
            f"publish_at must be at least {MIN_SCHEDULE_LEAD_SECONDS} seconds in the future"
        )


def load_client_secrets(path: Path) -> YouTubeClientSecrets:
    if not path.exists():
        raise YouTubePublishError(f"YouTube client secrets file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise YouTubePublishError("YouTube client secrets file is invalid")

    config = payload.get("installed") or payload.get("web")
    if not isinstance(config, dict):
        raise YouTubePublishError(
            "YouTube client secrets file must contain an 'installed' or 'web' section"
        )

    client_id = config.get("client_id")
    client_secret = config.get("client_secret")
    if not isinstance(client_id, str) or not client_id.strip():
        raise YouTubePublishError("YouTube client secrets file is missing client_id")
    if not isinstance(client_secret, str) or not client_secret.strip():
        raise YouTubePublishError("YouTube client secrets file is missing client_secret")

    redirect_uri = DEFAULT_REDIRECT_URI
    redirect_uris = config.get("redirect_uris")
    if isinstance(redirect_uris, list):
        # Prefer an explicit loopback host:port URI. Bare http://localhost (no port)
        # is common in Desktop OAuth clients but cannot receive the callback locally.
        for candidate in redirect_uris:
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            value = candidate.strip()
            parsed = urllib.parse.urlparse(value)
            if parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port:
                redirect_uri = value
                break

    return YouTubeClientSecrets(
        client_id=client_id.strip(),
        client_secret=client_secret.strip(),
        redirect_uri=redirect_uri,
    )


def save_token(path: Path, token: YouTubeToken) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "access_token": token.access_token,
                "refresh_token": token.refresh_token,
                "expires_at": token.expires_at,
                "scope": token.scope,
                "token_type": token.token_type,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def load_token(path: Path) -> YouTubeToken:
    if not path.exists():
        raise YouTubePublishError(f"YouTube token file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise YouTubePublishError("YouTube token file is invalid")

    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    expires_at = payload.get("expires_at")
    if not isinstance(access_token, str) or not access_token:
        raise YouTubePublishError("YouTube token file is missing access_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise YouTubePublishError("YouTube token file is missing refresh_token")
    if not isinstance(expires_at, (int, float)):
        raise YouTubePublishError("YouTube token file is missing expires_at")

    scope = payload.get("scope")
    token_type = payload.get("token_type", "Bearer")
    return YouTubeToken(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=float(expires_at),
        scope=scope if isinstance(scope, str) else None,
        token_type=token_type if isinstance(token_type, str) else "Bearer",
    )


def token_from_response(
    payload: dict[str, Any],
    *,
    fallback_refresh_token: str | None = None,
) -> YouTubeToken:
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token") or fallback_refresh_token
    expires_in = payload.get("expires_in")
    if not isinstance(access_token, str) or not access_token:
        raise YouTubePublishError("YouTube token response is missing access_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise YouTubePublishError("YouTube token response is missing refresh_token")
    if not isinstance(expires_in, (int, float)):
        raise YouTubePublishError("YouTube token response is missing expires_in")

    scope = payload.get("scope")
    token_type = payload.get("token_type", "Bearer")
    return YouTubeToken(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=time.time() + float(expires_in),
        scope=scope if isinstance(scope, str) else None,
        token_type=token_type if isinstance(token_type, str) else "Bearer",
    )


def save_pending_auth(path: Path, pending: YouTubePendingAuth) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "state": pending.state,
                "redirect_uri": pending.redirect_uri,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def load_pending_auth(path: Path) -> YouTubePendingAuth:
    if not path.exists():
        raise YouTubePublishError(f"Pending YouTube auth file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise YouTubePublishError("Pending YouTube auth file is invalid")

    state = payload.get("state")
    redirect_uri = payload.get("redirect_uri")
    if not isinstance(state, str) or not state:
        raise YouTubePublishError("Pending YouTube auth file is missing state")
    if not isinstance(redirect_uri, str) or not redirect_uri.strip():
        redirect_uri = DEFAULT_REDIRECT_URI
    return YouTubePendingAuth(state=state, redirect_uri=redirect_uri.strip())


def build_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: tuple[str, ...] = DEFAULT_SCOPES,
    state: str | None = None,
) -> tuple[str, YouTubePendingAuth]:
    auth_state = state or generate_state()
    query = urllib.parse.urlencode(
        {
            "client_id": client_id.strip(),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "prompt": "consent",
            "state": auth_state,
        }
    )
    pending = YouTubePendingAuth(state=auth_state, redirect_uri=redirect_uri)
    return f"{AUTH_URL}?{query}", pending


def build_video_status(job: PublishJob) -> dict[str, Any]:
    status: dict[str, Any] = {"containsSyntheticMedia": False}
    if job.publish_at is not None:
        validate_schedule_time(job.publish_at)
        status.update(
            {
                "privacyStatus": "private",
                "publishAt": format_publish_at(job.publish_at),
            }
        )
        return status

    privacy_status = job.privacy_status.strip().lower() or "public"
    if privacy_status not in {"public", "private", "unlisted"}:
        raise YouTubePublishError(
            f"Unsupported privacy_status {job.privacy_status!r}; "
            "expected public, private, or unlisted"
        )
    status["privacyStatus"] = privacy_status
    return status


def build_video_body(
    job: PublishJob,
    *,
    category_id: str = "22",
) -> dict[str, Any]:
    snippet: dict[str, Any] = {
        "title": job.title.strip(),
        "categoryId": category_id,
    }
    if job.description.strip():
        snippet["description"] = job.description.strip()
    if job.tags:
        snippet["tags"] = job.tags

    return {
        "snippet": snippet,
        "status": build_video_status(job),
    }


class YouTubeClient:
    def __init__(
        self,
        client_secrets_path: Path,
        token_path: Path,
        *,
        pending_auth_path: Path | None = None,
        expected_channel_handle: str | None = DEFAULT_CHANNEL_HANDLE,
    ) -> None:
        self.client_secrets_path = client_secrets_path
        self.token_path = token_path
        self.pending_auth_path = pending_auth_path or token_path.with_name(
            "youtube-auth-pending.json"
        )
        self.expected_channel_handle = (
            parse_channel_handle(expected_channel_handle)
            if expected_channel_handle
            else None
        )
        self._secrets: YouTubeClientSecrets | None = None
        self._playlist_id_cache: dict[str, str] = {}

    @property
    def secrets(self) -> YouTubeClientSecrets:
        if self._secrets is None:
            self._secrets = load_client_secrets(self.client_secrets_path)
        return self._secrets

    def _token_request(
        self,
        form: dict[str, str],
        *,
        fallback_refresh_token: str | None = None,
    ) -> YouTubeToken:
        data = urllib.parse.urlencode(form).encode("utf-8")
        request = urllib.request.Request(TOKEN_URL, data=data, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise YouTubePublishError(
                f"YouTube token request failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise YouTubePublishError(f"YouTube token request failed: {exc.reason}") from exc

        if not isinstance(payload, dict):
            raise YouTubePublishError("YouTube token response is not a JSON object")
        return token_from_response(
            payload,
            fallback_refresh_token=fallback_refresh_token,
        )

    def exchange_authorization_code(
        self,
        code: str,
        *,
        redirect_uri: str | None = None,
    ) -> YouTubeToken:
        token = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code.strip(),
                "client_id": self.secrets.client_id,
                "client_secret": self.secrets.client_secret,
                "redirect_uri": redirect_uri or self.secrets.redirect_uri,
            }
        )
        save_token(self.token_path, token)
        return token

    def refresh_access_token(self, refresh_token: str) -> YouTubeToken:
        token = self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.secrets.client_id,
                "client_secret": self.secrets.client_secret,
            },
            fallback_refresh_token=refresh_token,
        )
        save_token(self.token_path, token)
        return token

    def ensure_access_token(self) -> str:
        token = load_token(self.token_path)
        if token.expires_at > time.time() + 60:
            return token.access_token
        refreshed = self.refresh_access_token(token.refresh_token)
        return refreshed.access_token

    def start_authorization(self) -> str:
        url, pending = build_authorization_url(
            client_id=self.secrets.client_id,
            redirect_uri=self.secrets.redirect_uri,
        )
        save_pending_auth(self.pending_auth_path, pending)
        return url

    def complete_authorization(self, code: str, *, state: str | None = None) -> YouTubeToken:
        pending = load_pending_auth(self.pending_auth_path)
        if state is not None and state != pending.state:
            raise YouTubePublishError("YouTube authorization state does not match")
        token = self.exchange_authorization_code(
            code,
            redirect_uri=pending.redirect_uri,
        )
        if self.pending_auth_path.exists():
            self.pending_auth_path.unlink()
        return token

    def _request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 120,
    ) -> tuple[int, dict[str, str], bytes]:
        access_token = self.ensure_access_token()
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {access_token}")
        if headers:
            for key, value in headers.items():
                request.add_header(key, value)

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_headers = {
                    key.lower(): value
                    for key, value in response.headers.items()
                }
                return response.status, response_headers, response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()
            response_headers = {
                key.lower(): value for key, value in exc.headers.items()
            }
            return exc.code, response_headers, detail

    def _start_resumable_upload(
        self,
        *,
        video_path: Path,
        body: dict[str, Any],
    ) -> str:
        file_size = video_path.stat().st_size
        content_type = mimetypes.guess_type(video_path.name)[0] or "video/*"
        query = urllib.parse.urlencode(
            {
                "uploadType": "resumable",
                "part": "snippet,status",
            }
        )
        url = f"{UPLOAD_BASE}/videos?{query}"
        status, headers, payload = self._request(
            "POST",
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": content_type,
                "X-Upload-Content-Length": str(file_size),
            },
        )
        if status not in {200, 201}:
            detail = payload.decode("utf-8", errors="replace").strip()
            raise YouTubePublishError(
                f"YouTube resumable upload init failed with HTTP {status}: {detail}"
            )

        location = headers.get("location")
        if not location:
            raise YouTubePublishError(
                "YouTube resumable upload init did not return a Location header"
            )
        return location

    def _upload_video_bytes(self, upload_url: str, video_path: Path) -> dict[str, Any]:
        content_type = mimetypes.guess_type(video_path.name)[0] or "video/*"
        video_bytes = video_path.read_bytes()
        request = urllib.request.Request(upload_url, data=video_bytes, method="PUT")
        request.add_header("Content-Type", content_type)
        request.add_header("Content-Length", str(len(video_bytes)))

        try:
            with urllib.request.urlopen(request, timeout=3600) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise YouTubePublishError(
                f"YouTube video upload failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise YouTubePublishError(f"YouTube video upload failed: {exc.reason}") from exc

        if not isinstance(payload, dict):
            raise YouTubePublishError("YouTube upload response is not a JSON object")
        return payload

    def upload_video(
        self,
        video_path: Path,
        *,
        title: str,
        description: str = "",
        tags: list[str] | None = None,
        publish_at: datetime | None = None,
        privacy_status: str = "public",
        category_id: str = "22",
    ) -> str:
        if not video_path.exists():
            raise YouTubePublishError(f"Video file not found: {video_path}")

        self.verify_authorized_channel()

        job = PublishJob(
            title=title,
            description=description,
            tags=tags or [],
            publish_at=publish_at,
            privacy_status=privacy_status,
        )
        body = build_video_body(job, category_id=category_id)
        upload_url = self._start_resumable_upload(video_path=video_path, body=body)
        response = self._upload_video_bytes(upload_url, video_path)

        video_id = response.get("id")
        if not isinstance(video_id, str) or not video_id:
            raise YouTubePublishError("YouTube upload response is missing video id")
        return video_id

    def _parse_playlist_items(self, payload: bytes) -> list[dict[str, Any]]:
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            raise YouTubePublishError("YouTube playlist response is invalid")
        items = data.get("items", [])
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def list_my_playlists(self) -> list[dict[str, Any]]:
        playlists: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            query_items = {
                "part": "snippet",
                "mine": "true",
                "maxResults": "50",
            }
            if page_token:
                query_items["pageToken"] = page_token
            query = urllib.parse.urlencode(query_items)
            url = f"{API_BASE}/playlists?{query}"
            status, _, payload = self._request("GET", url)
            if status != 200:
                detail = payload.decode("utf-8", errors="replace").strip()
                raise YouTubePublishError(
                    f"YouTube playlist lookup failed with HTTP {status}: {detail}"
                )
            items = self._parse_playlist_items(payload)
            playlists.extend(items)
            data = json.loads(payload.decode("utf-8"))
            next_token = data.get("nextPageToken") if isinstance(data, dict) else None
            if not isinstance(next_token, str) or not next_token.strip():
                break
            page_token = next_token.strip()
        return playlists

    def resolve_playlist_id(
        self,
        title: str,
        *,
        playlist_id: str | None = None,
    ) -> str:
        if playlist_id and playlist_id.strip():
            return playlist_id.strip()

        normalized_title = title.strip()
        if not normalized_title:
            raise YouTubePublishError("YouTube playlist title is empty")

        cached = self._playlist_id_cache.get(normalized_title.casefold())
        if cached:
            return cached

        target = normalized_title.casefold()
        for item in self.list_my_playlists():
            playlist_key = item.get("id")
            snippet = item.get("snippet")
            if not isinstance(playlist_key, str) or not playlist_key:
                continue
            if not isinstance(snippet, dict):
                continue
            item_title = snippet.get("title")
            if isinstance(item_title, str) and item_title.strip().casefold() == target:
                self._playlist_id_cache[normalized_title.casefold()] = playlist_key
                return playlist_key

        raise YouTubePublishError(
            f"YouTube playlist {normalized_title!r} was not found on the authorized channel"
        )

    def add_video_to_playlist(
        self,
        video_id: str,
        playlist_id: str,
        *,
        position: int | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {
                    "kind": "youtube#video",
                    "videoId": video_id,
                },
            }
        }
        if position is not None:
            body["snippet"]["position"] = int(position)
        query = urllib.parse.urlencode({"part": "snippet"})
        url = f"{API_BASE}/playlistItems?{query}"
        status, _, payload = self._request(
            "POST",
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        if status not in {200, 201}:
            detail = payload.decode("utf-8", errors="replace").strip()
            raise YouTubePublishError(
                f"YouTube playlist insert failed with HTTP {status}: {detail}"
            )

    def set_playlist_item_position(
        self,
        *,
        playlist_item_id: str,
        playlist_id: str,
        video_id: str,
        position: int,
    ) -> None:
        body = {
            "id": playlist_item_id,
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {
                    "kind": "youtube#video",
                    "videoId": video_id,
                },
                "position": int(position),
            },
        }
        query = urllib.parse.urlencode({"part": "snippet"})
        url = f"{API_BASE}/playlistItems?{query}"
        status, _, payload = self._request(
            "PUT",
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        if status != 200:
            detail = payload.decode("utf-8", errors="replace").strip()
            raise YouTubePublishError(
                f"YouTube playlist reorder failed with HTTP {status}: {detail}"
            )

    def reorder_daily_playlist_slots(
        self,
        playlist_id: str,
        slots: dict[str, str],
    ) -> None:
        """Place quote / reel / lau in that order, packed to positions 0..n-1."""
        items = self.list_playlist_items(playlist_id)
        by_video: dict[str, dict[str, Any]] = {}
        for item in items:
            video_id = _playlist_item_video_id(item)
            item_id = item.get("id")
            if not isinstance(video_id, str) or not video_id:
                continue
            if not isinstance(item_id, str) or not item_id:
                continue
            by_video[video_id] = item

        position = 0
        for slot in DAILY_PLAYLIST_SLOTS:
            video_id = slots.get(slot)
            if not isinstance(video_id, str) or not video_id.strip():
                continue
            item = by_video.get(video_id.strip())
            if item is None:
                continue
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                continue
            snippet = item.get("snippet")
            current_position = None
            if isinstance(snippet, dict):
                current_position = snippet.get("position")
            if current_position != position:
                self.set_playlist_item_position(
                    playlist_item_id=item_id,
                    playlist_id=playlist_id,
                    video_id=video_id.strip(),
                    position=position,
                )
            position += 1

    def list_playlist_items(self, playlist_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            query_items = {
                "part": "snippet,contentDetails",
                "playlistId": playlist_id,
                "maxResults": "50",
            }
            if page_token:
                query_items["pageToken"] = page_token
            query = urllib.parse.urlencode(query_items)
            url = f"{API_BASE}/playlistItems?{query}"
            status, _, payload = self._request("GET", url)
            if status != 200:
                detail = payload.decode("utf-8", errors="replace").strip()
                raise YouTubePublishError(
                    f"YouTube playlist items lookup failed with HTTP {status}: {detail}"
                )
            items.extend(self._parse_playlist_items(payload))
            data = json.loads(payload.decode("utf-8"))
            next_token = data.get("nextPageToken") if isinstance(data, dict) else None
            if not isinstance(next_token, str) or not next_token.strip():
                break
            page_token = next_token.strip()
        return items

    def remove_playlist_item(self, playlist_item_id: str) -> None:
        query = urllib.parse.urlencode({"id": playlist_item_id})
        url = f"{API_BASE}/playlistItems?{query}"
        status, _, payload = self._request("DELETE", url)
        if status in {200, 204, 404}:
            return
        detail = payload.decode("utf-8", errors="replace").strip()
        raise YouTubePublishError(
            f"YouTube playlist item delete failed with HTTP {status}: {detail}"
        )

    def clear_playlist(self, playlist_id: str) -> int:
        """Remove every item from a playlist. Returns how many items were deleted."""
        removed = 0
        for item in self.list_playlist_items(playlist_id):
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                continue
            self.remove_playlist_item(item_id)
            removed += 1
        return removed

    def sync_daily_playlist_slot(
        self,
        playlist_id: str,
        video_id: str,
        *,
        slot: str,
        state_path: Path,
    ) -> dict[str, str]:
        """Keep one video each for quote / reel / lau slots in the daily playlist.

        Updates ``slot`` to ``video_id``, removes only the previous video for that
        slot (when known), ensures ``video_id`` is present, and reorders to
        quote → reel → lau (packed positions). Persists slots to ``state_path``.
        """
        if slot not in DAILY_PLAYLIST_SLOTS:
            raise YouTubePublishError(
                f"Unsupported daily playlist slot {slot!r}; expected one of {DAILY_PLAYLIST_SLOTS}"
            )
        video_id = video_id.strip()
        slots = load_daily_playlist_slots(state_path)
        previous_video_id = slots.get(slot)
        slots[slot] = video_id
        desired = {value for value in slots.values() if value}
        target_position = sum(
            1
            for name in DAILY_PLAYLIST_SLOTS
            if name != slot and isinstance(slots.get(name), str) and slots[name].strip()
            and DAILY_PLAYLIST_SLOTS.index(name) < DAILY_PLAYLIST_SLOTS.index(slot)
        )

        if previous_video_id and previous_video_id != video_id:
            for item in self.list_playlist_items(playlist_id):
                item_id = item.get("id")
                if not isinstance(item_id, str) or not item_id:
                    continue
                if _playlist_item_video_id(item) == previous_video_id:
                    self.remove_playlist_item(item_id)

        already_present = False
        for item in self.list_playlist_items(playlist_id):
            item_video_id = _playlist_item_video_id(item)
            if item_video_id == video_id:
                already_present = True
                break

        if not already_present:
            self.add_video_to_playlist(
                video_id,
                playlist_id,
                position=target_position,
            )

        # Prune only when we can identify a stale video id (never delete unknown rows).
        stale_ids = set()
        if previous_video_id and previous_video_id != video_id:
            stale_ids.add(previous_video_id)
        for item in self.list_playlist_items(playlist_id):
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                continue
            item_video_id = _playlist_item_video_id(item)
            if item_video_id is None:
                continue
            if item_video_id in desired:
                continue
            if item_video_id in stale_ids:
                self.remove_playlist_item(item_id)

        self.reorder_daily_playlist_slots(playlist_id, slots)
        pending = load_daily_playlist_pending(state_path)
        pending.pop(slot, None)
        save_daily_playlist_state(state_path, slots, pending)
        return slots

    def flush_pending_daily_playlist_slots(
        self,
        playlist_id: str,
        *,
        state_path: Path,
        now: datetime | None = None,
    ) -> list[str]:
        """Add pending slot videos to Днес once they are public/unlisted.

        Returns the slot names that were synced on this call.
        """
        now = now or datetime.now(timezone.utc)
        pending = load_daily_playlist_pending(state_path)
        if not pending:
            return []

        synced: list[str] = []
        for slot, entry in list(pending.items()):
            if slot not in DAILY_PLAYLIST_SLOTS:
                continue
            if not isinstance(entry, dict):
                continue
            video_id = entry.get("video_id")
            if not isinstance(video_id, str) or not video_id.strip():
                continue
            video_id = video_id.strip()
            publish_at_raw = entry.get("publish_at")
            if isinstance(publish_at_raw, str) and publish_at_raw.strip():
                try:
                    publish_at = datetime.fromisoformat(
                        publish_at_raw.strip().replace("Z", "+00:00")
                    )
                except ValueError:
                    publish_at = None
                if publish_at is not None:
                    if publish_at.tzinfo is None:
                        publish_at = publish_at.replace(tzinfo=timezone.utc)
                    else:
                        publish_at = publish_at.astimezone(timezone.utc)
                    if publish_at > now:
                        continue
            item = self.get_video_status_item(video_id)
            if item is None or not _video_is_listable_on_playlist(item):
                continue
            self.sync_daily_playlist_slot(
                playlist_id,
                video_id,
                slot=slot,
                state_path=state_path,
            )
            synced.append(slot)
        return synced

    def update_video_snippet(
        self,
        video_id: str,
        *,
        title: str,
        description: str,
        tags: list[str] | None = None,
        category_id: str = "22",
    ) -> None:
        body = {
            "id": video_id,
            "snippet": {
                "title": title.strip(),
                "description": description.strip(),
                "categoryId": category_id,
            },
        }
        if tags:
            body["snippet"]["tags"] = tags
        query = urllib.parse.urlencode({"part": "snippet"})
        url = f"{API_BASE}/videos?{query}"
        status, _, payload = self._request(
            "PUT",
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        if status != 200:
            detail = payload.decode("utf-8", errors="replace").strip()
            raise YouTubePublishError(
                f"YouTube video update failed with HTTP {status}: {detail}"
            )

    def get_video_status_item(self, video_id: str) -> dict[str, Any] | None:
        query = urllib.parse.urlencode(
            {"part": "status,processingDetails", "id": video_id}
        )
        url = f"{API_BASE}/videos?{query}"
        status, _, payload = self._request("GET", url)
        if status != 200:
            detail = payload.decode("utf-8", errors="replace").strip()
            raise YouTubePublishError(
                f"YouTube video status lookup failed with HTTP {status}: {detail}"
            )
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            raise YouTubePublishError("YouTube video status response is invalid")
        items = data.get("items", [])
        if not isinstance(items, list) or not items:
            return None
        item = items[0]
        return item if isinstance(item, dict) else None

    def wait_for_video_ready_for_thumbnail(self, video_id: str) -> None:
        for _ in range(VIDEO_PROCESSING_POLL_MAX_ATTEMPTS):
            item = self.get_video_status_item(video_id)
            if item is not None:
                failure = _video_processing_failure(item)
                if failure is not None:
                    raise YouTubePublishError(
                        f"YouTube video {video_id!r} processing failed: {failure}"
                    )
                if _video_is_ready_for_thumbnail(item):
                    return
            time.sleep(VIDEO_PROCESSING_POLL_INTERVAL_SECONDS)

        raise YouTubePublishError(
            f"YouTube video {video_id!r} did not finish processing in time for "
            "thumbnail upload"
        )

    def set_thumbnail(self, video_id: str, thumbnail_path: Path) -> None:
        if not thumbnail_path.exists():
            raise YouTubePublishError(f"Thumbnail file not found: {thumbnail_path}")

        self.wait_for_video_ready_for_thumbnail(video_id)

        content_type = mimetypes.guess_type(thumbnail_path.name)[0] or "image/jpeg"
        query = urllib.parse.urlencode({"videoId": video_id})
        url = f"{UPLOAD_BASE}/thumbnails/set?{query}"
        image_bytes = thumbnail_path.read_bytes()
        last_detail = ""

        for attempt in range(1, THUMBNAIL_SET_MAX_ATTEMPTS + 1):
            status, _, payload = self._request(
                "POST",
                url,
                data=image_bytes,
                headers={
                    "Content-Type": content_type,
                    "Content-Length": str(len(image_bytes)),
                },
            )
            if status in {200, 201}:
                return

            last_detail = payload.decode("utf-8", errors="replace").strip()
            if attempt < THUMBNAIL_SET_MAX_ATTEMPTS and _thumbnail_retryable_error(
                last_detail
            ):
                time.sleep(THUMBNAIL_SET_RETRY_SECONDS)
                continue
            break

        raise YouTubePublishError(
            f"YouTube thumbnail upload failed with HTTP {status}: {last_detail}"
        )

    def _parse_channel_list_response(self, payload: bytes) -> list[YouTubeChannel]:
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            raise YouTubePublishError("YouTube channel response is invalid")

        items = data.get("items", [])
        if not isinstance(items, list):
            return []

        channels: list[YouTubeChannel] = []
        for item in items:
            if isinstance(item, dict):
                channel = channel_from_api_item(item)
                if channel is not None:
                    channels.append(channel)
        return channels

    def list_my_channels(self) -> list[YouTubeChannel]:
        query = urllib.parse.urlencode({"part": "snippet", "mine": "true"})
        url = f"{API_BASE}/channels?{query}"
        status, _, payload = self._request("GET", url)
        if status != 200:
            detail = payload.decode("utf-8", errors="replace").strip()
            raise YouTubePublishError(
                f"YouTube channel lookup failed with HTTP {status}: {detail}"
            )
        return self._parse_channel_list_response(payload)

    def get_channel_by_handle(self, handle: str) -> YouTubeChannel:
        normalized = parse_channel_handle(handle)
        query = urllib.parse.urlencode({"part": "snippet", "forHandle": normalized})
        url = f"{API_BASE}/channels?{query}"
        status, _, payload = self._request("GET", url)
        if status != 200:
            detail = payload.decode("utf-8", errors="replace").strip()
            raise YouTubePublishError(
                f"YouTube channel lookup failed with HTTP {status}: {detail}"
            )
        channels = self._parse_channel_list_response(payload)
        if not channels:
            raise YouTubePublishError(f"YouTube channel @{normalized} was not found")
        return channels[0]

    def verify_authorized_channel(self) -> YouTubeChannel:
        if not self.expected_channel_handle:
            channels = self.list_my_channels()
            if not channels:
                raise YouTubePublishError("No YouTube channel is linked to this token")
            return channels[0]

        expected = self.expected_channel_handle
        my_channels = self.list_my_channels()
        for channel in my_channels:
            if channel.handle and channel.handle.lower() == expected.lower():
                return channel

        try:
            target_channel = self.get_channel_by_handle(expected)
        except YouTubePublishError:
            target_channel = None

        if target_channel is not None:
            for channel in my_channels:
                if channel.id == target_channel.id:
                    return channel

        raise YouTubePublishError(
            "Authorized Google account does not manage "
            f"https://www.youtube.com/@{expected}. "
            f"Available channels: {format_channel_list(my_channels)}. "
            "Re-run --youtube-auth while signed into that channel in YouTube."
        )

    def get_channel_info(self) -> dict[str, Any]:
        query = urllib.parse.urlencode({"part": "snippet", "mine": "true"})
        url = f"{API_BASE}/channels?{query}"
        status, _, payload = self._request("GET", url)
        if status != 200:
            detail = payload.decode("utf-8", errors="replace").strip()
            raise YouTubePublishError(
                f"YouTube channel lookup failed with HTTP {status}: {detail}"
            )
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            raise YouTubePublishError("YouTube channel response is invalid")
        return data

    def test_connection(self) -> YouTubeToken:
        token = load_token(self.token_path)
        refreshed = self.refresh_access_token(token.refresh_token)
        return refreshed


def youtube_video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def daily_playlist_slot_for_job(job: PublishJob) -> DailyPlaylistSlot:
    if job.content_kind == "image":
        return "quote"
    if job.video_format == "short_form":
        return "reel"
    return "lau"


def job_ready_for_daily_playlist(
    job: PublishJob, *, now: datetime | None = None
) -> bool:
    """True when the upload is (or will immediately be) public/unlisted for Smartlink."""
    now = now or datetime.now(timezone.utc)
    if job.publish_at is not None:
        publish_at = job.publish_at
        if publish_at.tzinfo is None:
            publish_at = publish_at.replace(tzinfo=timezone.utc)
        else:
            publish_at = publish_at.astimezone(timezone.utc)
        if publish_at > now:
            return False
    privacy = (job.privacy_status or "public").strip().lower()
    return privacy in {"public", "unlisted"}


def should_queue_daily_playlist(
    job: PublishJob, *, now: datetime | None = None
) -> bool:
    """True when a scheduled private upload should wait for go-live before Днес."""
    return job.publish_at is not None and not job_ready_for_daily_playlist(
        job, now=now
    )


def _daily_playlist_payload(path: Path) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    env_payload = os.getenv("YOUTUBE_DAILY_PLAYLIST_SLOTS_JSON", "").strip()
    if env_payload:
        try:
            parsed = json.loads(env_payload)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            merged.update(parsed)
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            merged.update(payload)
    return merged


def load_daily_playlist_slots(path: Path) -> dict[str, str]:
    slots: dict[str, str] = {}
    payload = _daily_playlist_payload(path)
    for key in DAILY_PLAYLIST_SLOTS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            slots[key] = value.strip()
    return slots


def load_daily_playlist_pending(path: Path) -> dict[str, dict[str, str]]:
    payload = _daily_playlist_payload(path)
    raw = payload.get("pending")
    if not isinstance(raw, dict):
        return {}
    pending: dict[str, dict[str, str]] = {}
    for key in DAILY_PLAYLIST_SLOTS:
        entry = raw.get(key)
        if not isinstance(entry, dict):
            continue
        video_id = entry.get("video_id")
        if not isinstance(video_id, str) or not video_id.strip():
            continue
        cleaned: dict[str, str] = {"video_id": video_id.strip()}
        publish_at = entry.get("publish_at")
        if isinstance(publish_at, str) and publish_at.strip():
            cleaned["publish_at"] = publish_at.strip()
        pending[key] = cleaned
    return pending


def save_daily_playlist_slots(path: Path, slots: dict[str, str]) -> None:
    save_daily_playlist_state(path, slots, load_daily_playlist_pending(path))


def save_daily_playlist_state(
    path: Path,
    slots: dict[str, str],
    pending: dict[str, dict[str, str]] | None = None,
) -> None:
    cleaned = {
        key: slots[key].strip()
        for key in DAILY_PLAYLIST_SLOTS
        if isinstance(slots.get(key), str) and slots[key].strip()
    }
    pending_cleaned: dict[str, dict[str, str]] = {}
    if pending:
        for key in DAILY_PLAYLIST_SLOTS:
            entry = pending.get(key)
            if not isinstance(entry, dict):
                continue
            video_id = entry.get("video_id")
            if not isinstance(video_id, str) or not video_id.strip():
                continue
            cleaned_entry: dict[str, str] = {"video_id": video_id.strip()}
            publish_at = entry.get("publish_at")
            if isinstance(publish_at, str) and publish_at.strip():
                cleaned_entry["publish_at"] = publish_at.strip()
            pending_cleaned[key] = cleaned_entry
    if pending_cleaned:
        cleaned["pending"] = pending_cleaned
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cleaned, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def queue_pending_daily_playlist_slot(
    state_path: Path,
    *,
    slot: str,
    video_id: str,
    publish_at: datetime | None,
) -> None:
    if slot not in DAILY_PLAYLIST_SLOTS:
        raise YouTubePublishError(
            f"Unsupported daily playlist slot {slot!r}; expected one of {DAILY_PLAYLIST_SLOTS}"
        )
    slots = load_daily_playlist_slots(state_path)
    pending = load_daily_playlist_pending(state_path)
    entry: dict[str, str] = {"video_id": video_id.strip()}
    if publish_at is not None:
        entry["publish_at"] = format_publish_at(publish_at)
    pending[slot] = entry
    save_daily_playlist_state(state_path, slots, pending)


def _video_is_listable_on_playlist(item: dict[str, Any]) -> bool:
    status = item.get("status")
    if not isinstance(status, dict):
        return False
    privacy = status.get("privacyStatus")
    return privacy in {"public", "unlisted"}


def _playlist_item_video_id(item: dict[str, Any]) -> str | None:
    snippet = item.get("snippet")
    if not isinstance(snippet, dict):
        return None
    resource = snippet.get("resourceId")
    if not isinstance(resource, dict):
        return None
    video_id = resource.get("videoId")
    return video_id.strip() if isinstance(video_id, str) and video_id.strip() else None


def publish_to_youtube(
    job: PublishJob,
    *,
    client_secrets_path: Path,
    token_path: Path,
    category_id: str = "22",
    expected_channel_handle: str | None = DEFAULT_CHANNEL_HANDLE,
    facebook_url: str | None = None,
    instagram_url: str | None = None,
    youtube_channel_url: str | None = None,
    ffmpeg_path: str | None = None,
    cover_intro_seconds: float | None = None,
    playlist_id: str | None = None,
    playlist_title: str | None = DEFAULT_YOUTUBE_PLAYLIST_TITLE,
    daily_playlist_id: str | None = None,
    daily_playlist_slots_path: Path | None = None,
) -> str:
    """Upload a video to YouTube and return the published video ID."""
    from media_publisher.post_templates import (
        DEFAULT_FACEBOOK_PAGE_URL,
        DEFAULT_INSTAGRAM_PROFILE_URL,
        DEFAULT_YOUTUBE_CHANNEL_URL,
        inject_published_video_url,
        prepare_publish_job,
    )

    if not job.video_path:
        raise YouTubePublishError("Publish job is missing video_path")

    job = prepare_publish_job(
        job,
        "youtube",
        facebook_url=facebook_url or DEFAULT_FACEBOOK_PAGE_URL,
        instagram_url=instagram_url or DEFAULT_INSTAGRAM_PROFILE_URL,
        youtube_channel_url=youtube_channel_url or DEFAULT_YOUTUBE_CHANNEL_URL,
    )
    video_path = Path(job.video_path)
    upload_path = video_path
    if (
        job.video_format == "short_form"
        and job.thumbnail_path
        and job.content_kind != "image"
    ):
        thumbnail = prepare_youtube_thumbnail(
            Path(job.thumbnail_path),
            short_form=True,
            ffmpeg_path=ffmpeg_path,
        )
        try:
            upload_path = ensure_short_with_cover_intro(
                video_path,
                thumbnail,
                ffmpeg_path=ffmpeg_path,
                intro_seconds=cover_intro_seconds or SHORT_COVER_INTRO_SECONDS,
            )
        except ImageVideoError as exc:
            raise YouTubePublishError(str(exc)) from exc

    client = YouTubeClient(
        client_secrets_path,
        token_path,
        expected_channel_handle=expected_channel_handle,
    )
    video_id = client.upload_video(
        upload_path,
        title=job.title,
        description=job.description,
        tags=job.tags,
        publish_at=job.publish_at,
        privacy_status=job.privacy_status,
        category_id=category_id,
    )

    if job.video_format == "post":
        updated_description = inject_published_video_url(job.description, video_id)
        if updated_description != job.description:
            client.update_video_snippet(
                video_id,
                title=job.title,
                description=updated_description,
                tags=job.tags,
                category_id=category_id,
            )

    if job.thumbnail_path:
        thumbnail = prepare_youtube_thumbnail(
            Path(job.thumbnail_path),
            short_form=job.video_format == "short_form",
            ffmpeg_path=ffmpeg_path,
        )
        try:
            client.set_thumbnail(video_id, thumbnail)
        except YouTubePublishError as exc:
            if job.video_format != "short_form":
                raise
            # Shorts often reject API thumbnails; upload still succeeds.

    if playlist_title or playlist_id:
        resolved_playlist_id = client.resolve_playlist_id(
            playlist_title or "",
            playlist_id=playlist_id,
        )
        client.add_video_to_playlist(video_id, resolved_playlist_id)

    if daily_playlist_id:
        resolved_daily_id = client.resolve_playlist_id(
            "",
            playlist_id=daily_playlist_id,
        )
        slots_path = daily_playlist_slots_path or Path(DEFAULT_DAILY_PLAYLIST_SLOTS_PATH)
        client.flush_pending_daily_playlist_slots(
            resolved_daily_id,
            state_path=slots_path,
        )
        slot = daily_playlist_slot_for_job(job)
        if job_ready_for_daily_playlist(job):
            client.sync_daily_playlist_slot(
                resolved_daily_id,
                video_id,
                slot=slot,
                state_path=slots_path,
            )
        elif should_queue_daily_playlist(job):
            queue_pending_daily_playlist_slot(
                slots_path,
                slot=slot,
                video_id=video_id,
                publish_at=job.publish_at,
            )

    return video_id


def flush_configured_daily_playlist(
    *,
    client_secrets_path: Path,
    token_path: Path,
    expected_channel_handle: str | None = DEFAULT_CHANNEL_HANDLE,
    daily_playlist_id: str | None = None,
    daily_playlist_slots_path: Path | None = None,
) -> list[str]:
    """Promote any due pending daily-playlist slots. No-op when none are pending."""
    if not daily_playlist_id:
        return []
    slots_path = daily_playlist_slots_path or Path(DEFAULT_DAILY_PLAYLIST_SLOTS_PATH)
    if not load_daily_playlist_pending(slots_path):
        return []
    client = YouTubeClient(
        client_secrets_path,
        token_path,
        expected_channel_handle=expected_channel_handle,
    )
    resolved_daily_id = client.resolve_playlist_id(
        "",
        playlist_id=daily_playlist_id,
    )
    return client.flush_pending_daily_playlist_slots(
        resolved_daily_id,
        state_path=slots_path,
    )
