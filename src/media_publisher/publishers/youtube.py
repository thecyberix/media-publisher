from __future__ import annotations

import json
import mimetypes
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from media_publisher.models import PublishJob

API_BASE = "https://www.googleapis.com/youtube/v3"
UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8766/callback"
DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
)
METADATA_YOUTUBE_VIDEO_ID = "youtube_video_id"
DEFAULT_CHANNEL_HANDLE = "SadhguruBulgarian"
CHANNEL_HANDLE_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?youtube\.com/@([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
MIN_SCHEDULE_LEAD_SECONDS = 60


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
        for candidate in redirect_uris:
            if isinstance(candidate, str) and candidate.strip():
                redirect_uri = candidate.strip()
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
    if job.publish_at is not None:
        validate_schedule_time(job.publish_at)
        return {
            "privacyStatus": "private",
            "publishAt": format_publish_at(job.publish_at),
        }

    privacy_status = job.privacy_status.strip().lower() or "public"
    if privacy_status not in {"public", "private", "unlisted"}:
        raise YouTubePublishError(
            f"Unsupported privacy_status {job.privacy_status!r}; "
            "expected public, private, or unlisted"
        )
    return {"privacyStatus": privacy_status}


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

    def set_thumbnail(self, video_id: str, thumbnail_path: Path) -> None:
        if not thumbnail_path.exists():
            raise YouTubePublishError(f"Thumbnail file not found: {thumbnail_path}")

        content_type = mimetypes.guess_type(thumbnail_path.name)[0] or "image/jpeg"
        query = urllib.parse.urlencode({"videoId": video_id})
        url = f"{UPLOAD_BASE}/thumbnails/set?{query}"
        image_bytes = thumbnail_path.read_bytes()
        status, _, payload = self._request(
            "POST",
            url,
            data=image_bytes,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(image_bytes)),
            },
        )
        if status not in {200, 201}:
            detail = payload.decode("utf-8", errors="replace").strip()
            raise YouTubePublishError(
                f"YouTube thumbnail upload failed with HTTP {status}: {detail}"
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


def publish_to_youtube(
    job: PublishJob,
    *,
    client_secrets_path: Path,
    token_path: Path,
    category_id: str = "22",
    expected_channel_handle: str | None = DEFAULT_CHANNEL_HANDLE,
) -> str:
    """Upload a video to YouTube and return the published video ID."""
    if not job.video_path:
        raise YouTubePublishError("Publish job is missing video_path")

    video_path = Path(job.video_path)
    client = YouTubeClient(
        client_secrets_path,
        token_path,
        expected_channel_handle=expected_channel_handle,
    )
    video_id = client.upload_video(
        video_path,
        title=job.title,
        description=job.description,
        tags=job.tags,
        publish_at=job.publish_at,
        privacy_status=job.privacy_status,
        category_id=category_id,
    )

    if job.thumbnail_path:
        client.set_thumbnail(video_id, Path(job.thumbnail_path))

    return video_id
