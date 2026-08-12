from __future__ import annotations

import json
import mimetypes
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

import requests
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from media_publisher.video_duration import INSTAGRAM_SINGLE_UPLOAD_MAX_BYTES

DEFAULT_API_VERSION = "v21.0"
DEFAULT_GRAPH_BASE = "https://graph.facebook.com"
DEFAULT_GRAPH_VIDEO_BASE = "https://graph-video.facebook.com"
CONTAINER_POLL_INTERVAL_SECONDS = 5.0
CONTAINER_POLL_MAX_ATTEMPTS = 240
MIN_SCHEDULE_LEAD_SECONDS = 600
MAX_SCHEDULE_LEAD_SECONDS = 60 * 60 * 24 * 75
UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024
UPLOAD_CHUNK_DELAY_SECONDS = 0.15
UPLOAD_MAX_ATTEMPTS = 5
UPLOAD_CHUNK_MAX_ATTEMPTS = 6


class MetaError(RuntimeError):
    pass


def _parse_meta_upload_backoff(response_text: str) -> float | None:
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    backoff_ms = payload.get("backoff")
    if isinstance(backoff_ms, (int, float)) and backoff_ms > 0:
        return backoff_ms / 1000.0
    return None


def _sleep_before_next_upload_chunk(offset: int, file_size: int) -> None:
    if offset < file_size:
        time.sleep(UPLOAD_CHUNK_DELAY_SECONDS)


@dataclass(frozen=True)
class MetaUploadSession:
    session_id: str


@dataclass(frozen=True)
class MetaContainerStatus:
    id: str
    status_code: str | None = None
    status: str | None = None


@dataclass(frozen=True)
class MetaPageInfo:
    page_id: str
    name: str
    username: str | None = None
    instagram_account_id: str | None = None
    instagram_username: str | None = None


@dataclass(frozen=True)
class MetaPageCredentials(MetaPageInfo):
    access_token: str = ""


@dataclass(frozen=True)
class MetaAccessTokenInfo:
    token_type: str
    is_valid: bool
    expires_at: datetime | None
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class FacebookHostingAsset:
    """Unpublished Facebook photo/video uploaded only to host media for Instagram."""

    asset_id: str
    url: str
    kind: Literal["video", "photo"]


def normalize_facebook_page_username(value: str) -> str:
    text = value.strip().rstrip("/")
    if "facebook.com/" in text.lower():
        return text.rsplit("/", 1)[-1].split("?")[0]
    return text


def normalize_facebook_permalink(value: str) -> str:
    text = value.strip()
    if not text:
        return text
    if text.startswith(("http://", "https://")):
        return text
    if text.startswith("/"):
        return f"https://www.facebook.com{text}"
    return f"https://www.facebook.com/{text}"


_FACEBOOK_VIDEO_ID_RE = re.compile(
    r"(?:(?:facebook\.com)/(?:reel|watch|video(?:s)?\.php)|(?:fb\.watch)/)"
    r"(?:.*?[?&]v=)?/?(\d{5,})",
    re.IGNORECASE,
)
_FACEBOOK_NUMERIC_ID_RE = re.compile(r"^\d{5,}$")


def extract_facebook_video_id(value: str) -> str | None:
    """Extract a Facebook video/reel id from a permalink, Graph id, or numeric string."""
    text = value.strip()
    if not text:
        return None
    if _FACEBOOK_NUMERIC_ID_RE.fullmatch(text):
        return text
    match = _FACEBOOK_VIDEO_ID_RE.search(text)
    if match:
        return match.group(1)
    # Common Page video path shapes: /PageName/videos/123.../ or /reel/123...
    for pattern in (
        r"/reel/(\d{5,})",
        r"/videos/(\d{5,})",
        r"[?&]v=(\d{5,})",
    ):
        found = re.search(pattern, text, re.IGNORECASE)
        if found:
            return found.group(1)
    return None


def normalize_instagram_username(value: str) -> str:
    text = value.strip().rstrip("/").lstrip("@")
    if "instagram.com/" in text.lower():
        return text.rsplit("/", 1)[-1].split("?")[0]
    return text


def unix_timestamp(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


def validate_publish_at(value: datetime, *, now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    lead = (value - current).total_seconds()
    if lead < MIN_SCHEDULE_LEAD_SECONDS:
        raise MetaError(
            f"Scheduled publish time must be at least {MIN_SCHEDULE_LEAD_SECONDS // 60} "
            "minutes in the future"
        )
    if lead > MAX_SCHEDULE_LEAD_SECONDS:
        raise MetaError(
            f"Scheduled publish time must be within {MAX_SCHEDULE_LEAD_SECONDS // 86400} days"
        )
    return value


def _guess_video_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "video/mp4"


def _guess_image_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "image/jpeg"


def _graph_get(
    path: str,
    access_token: str,
    *,
    api_version: str = DEFAULT_API_VERSION,
    query: dict[str, str] | None = None,
    timeout: int = 60,
) -> Any:
    params = dict(query or {})
    params["access_token"] = access_token
    url = f"{DEFAULT_GRAPH_BASE}/{api_version}/{path.lstrip('/')}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise MetaError(f"Meta GET {path} failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise MetaError(f"Meta GET {path} failed: {exc.reason}") from exc
    if not payload:
        return {}
    return json.loads(payload.decode("utf-8"))


def inspect_access_token(
    token: str,
    *,
    app_id: str,
    app_secret: str,
    api_version: str = DEFAULT_API_VERSION,
) -> MetaAccessTokenInfo:
    response = _graph_get(
        "debug_token",
        f"{app_id}|{app_secret}",
        api_version=api_version,
        query={"input_token": token},
    )
    data = response.get("data", {})
    if not isinstance(data, dict):
        raise MetaError("Meta debug_token response is invalid")

    expires_at: datetime | None
    expires_raw = data.get("expires_at")
    if isinstance(expires_raw, int) and expires_raw > 0:
        expires_at = datetime.fromtimestamp(expires_raw, tz=timezone.utc)
    else:
        expires_at = None

    scopes_raw = data.get("scopes", [])
    scopes = tuple(str(scope) for scope in scopes_raw) if isinstance(scopes_raw, list) else ()

    return MetaAccessTokenInfo(
        token_type=str(data.get("type") or "unknown"),
        is_valid=bool(data.get("is_valid")),
        expires_at=expires_at,
        scopes=scopes,
    )


def exchange_short_lived_user_token(
    short_lived_token: str,
    *,
    app_id: str,
    app_secret: str,
    api_version: str = DEFAULT_API_VERSION,
) -> str:
    params = urllib.parse.urlencode(
        {
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_lived_token,
        }
    )
    url = f"{DEFAULT_GRAPH_BASE}/{api_version}/oauth/access_token?{params}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise MetaError(f"Meta token exchange failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise MetaError(f"Meta token exchange failed: {exc.reason}") from exc

    if not payload:
        raise MetaError("Meta token exchange returned an empty response")
    response_data = json.loads(payload.decode("utf-8"))
    access_token = response_data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise MetaError("Meta token exchange response is missing access_token")
    return access_token


def _parse_page_account(payload: dict[str, Any]) -> MetaPageCredentials:
    page_id = payload.get("id")
    if not isinstance(page_id, str) or not page_id:
        raise MetaError("Meta page account response is missing page id")

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise MetaError("Meta page account response is missing access_token")

    name = payload.get("name")
    if not isinstance(name, str) or not name:
        name = page_id

    username = payload.get("username")
    ig_account = payload.get("instagram_business_account", {})
    ig_id = None
    ig_username = None
    if isinstance(ig_account, dict):
        raw_ig_id = ig_account.get("id")
        if isinstance(raw_ig_id, str) and raw_ig_id:
            ig_id = raw_ig_id
        raw_ig_username = ig_account.get("username")
        if isinstance(raw_ig_username, str) and raw_ig_username:
            ig_username = raw_ig_username

    return MetaPageCredentials(
        page_id=page_id,
        name=name,
        username=username if isinstance(username, str) else None,
        instagram_account_id=ig_id,
        instagram_username=ig_username,
        access_token=access_token,
    )


def list_managed_page_credentials(
    user_access_token: str,
    *,
    api_version: str = DEFAULT_API_VERSION,
) -> list[MetaPageCredentials]:
    response = _graph_get(
        "me/accounts",
        user_access_token,
        api_version=api_version,
        query={
            "fields": "id,name,username,access_token,instagram_business_account{id,username}",
        },
    )
    pages = response.get("data", [])
    if not isinstance(pages, list):
        raise MetaError("Meta me/accounts response is invalid")
    return [_parse_page_account(page) for page in pages if isinstance(page, dict)]


def resolve_permanent_page_token(
    access_token: str,
    *,
    page_username: str,
    app_id: str,
    app_secret: str,
    api_version: str = DEFAULT_API_VERSION,
) -> MetaPageCredentials:
    normalized_username = normalize_facebook_page_username(page_username).lower()
    token_info = inspect_access_token(
        access_token,
        app_id=app_id,
        app_secret=app_secret,
        api_version=api_version,
    )
    if not token_info.is_valid:
        raise MetaError("The provided Meta access token is not valid")

    if token_info.token_type.upper() == "PAGE":
        client = MetaClient(access_token, api_version=api_version, app_id=app_id)
        page_info = client.resolve_page_by_username(page_username)
        actual_username = (page_info.username or "").lower()
        if actual_username and actual_username != normalized_username:
            raise MetaError(
                f"Token is for page @{page_info.username}, expected @{page_username}"
            )
        return MetaPageCredentials(
            page_id=page_info.page_id,
            name=page_info.name,
            username=page_info.username,
            instagram_account_id=page_info.instagram_account_id,
            instagram_username=page_info.instagram_username,
            access_token=access_token,
        )

    working_token = access_token
    if token_info.token_type.upper() == "USER":
        working_token = exchange_short_lived_user_token(
            access_token,
            app_id=app_id,
            app_secret=app_secret,
            api_version=api_version,
        )
    elif token_info.expires_at is not None:
        remaining = (token_info.expires_at - datetime.now(timezone.utc)).total_seconds()
        if remaining < 7 * 24 * 60 * 60:
            working_token = exchange_short_lived_user_token(
                access_token,
                app_id=app_id,
                app_secret=app_secret,
                api_version=api_version,
            )

    for page in list_managed_page_credentials(working_token, api_version=api_version):
        candidates = {
            (page.username or "").lower(),
            page.name.lower(),
            page.page_id.lower(),
        }
        if normalized_username in candidates:
            return page

    raise MetaError(
        f"No managed Facebook page found for {page_username!r}. "
        "Ensure the token was generated by a page admin."
    )


class MetaClient:
    def __init__(
        self,
        access_token: str,
        *,
        api_version: str = DEFAULT_API_VERSION,
        api_base: str = DEFAULT_GRAPH_BASE,
        app_id: str | None = None,
    ) -> None:
        self.access_token = access_token.strip()
        self.api_version = api_version.strip().lstrip("v") and api_version.strip() or DEFAULT_API_VERSION
        if not self.api_version.startswith("v"):
            self.api_version = f"v{self.api_version}"
        self.api_base = api_base.rstrip("/")
        self.app_id = app_id.strip() if app_id else None
        if not self.access_token:
            raise MetaError("META_ACCESS_TOKEN is required")

    def _graph_url(self, path: str) -> str:
        normalized = path.lstrip("/")
        return f"{self.api_base}/{self.api_version}/{normalized}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str | int | bool] | None = None,
        body: dict[str, Any] | None = None,
        timeout: int = 120,
    ) -> Any:
        params = dict(query or {})
        params.setdefault("access_token", self.access_token)
        url = f"{self._graph_url(path)}?{urllib.parse.urlencode(params, doseq=True)}"

        data = None
        if body is not None:
            data = urllib.parse.urlencode(body).encode("utf-8")

        request = urllib.request.Request(url, data=data, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise MetaError(
                f"Meta {method} {path} failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise MetaError(f"Meta request failed: {exc.reason}") from exc

        if not payload:
            return {}
        return json.loads(payload.decode("utf-8"))

    def _multipart_request(
        self,
        path: str,
        *,
        fields: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
        timeout: int = 600,
        api_base: str | None = None,
    ) -> Any:
        boundary = f"----MetaFormBoundary{secrets.token_hex(16)}"
        body_parts: list[bytes] = []

        for key, value in fields.items():
            body_parts.append(f"--{boundary}\r\n".encode())
            body_parts.append(
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
            )

        for key, (filename, content, mime_type) in files.items():
            body_parts.append(f"--{boundary}\r\n".encode())
            body_parts.append(
                (
                    f'Content-Disposition: form-data; name="{key}"; '
                    f'filename="{filename}"\r\n'
                    f"Content-Type: {mime_type}\r\n\r\n"
                ).encode()
            )
            body_parts.append(content)
            body_parts.append(b"\r\n")

        body_parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(body_parts)

        params = {"access_token": self.access_token}
        base = (api_base or self.api_base).rstrip("/")
        url = f"{base}/{self.api_version}/{path.lstrip('/')}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header(
            "Content-Type",
            f"multipart/form-data; boundary={boundary}",
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise MetaError(
                f"Meta multipart POST {path} failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise MetaError(f"Meta multipart request failed: {exc.reason}") from exc

        if not payload:
            return {}
        return json.loads(payload.decode("utf-8"))

    def test_connection(self, page_id: str) -> dict[str, Any]:
        response = self._request(
            "GET",
            page_id,
            query={"fields": "id,name,username,instagram_business_account{id,username}"},
        )
        if not isinstance(response, dict):
            raise MetaError("Meta page lookup returned an unexpected payload")
        return response

    def resolve_page_by_username(self, page_username: str) -> MetaPageInfo:
        username = normalize_facebook_page_username(page_username)
        if not username:
            raise MetaError("Facebook page username is empty")

        response = self.test_connection(username)
        page_id = response.get("id")
        if not isinstance(page_id, str) or not page_id:
            raise MetaError("Meta page lookup response is missing page id")

        name = response.get("name")
        if not isinstance(name, str) or not name:
            name = username

        page_handle = response.get("username")
        ig_account = response.get("instagram_business_account", {})
        ig_id = None
        ig_username = None
        if isinstance(ig_account, dict):
            raw_ig_id = ig_account.get("id")
            if isinstance(raw_ig_id, str) and raw_ig_id:
                ig_id = raw_ig_id
            raw_ig_username = ig_account.get("username")
            if isinstance(raw_ig_username, str) and raw_ig_username:
                ig_username = raw_ig_username

        return MetaPageInfo(
            page_id=page_id,
            name=name,
            username=page_handle if isinstance(page_handle, str) else username,
            instagram_account_id=ig_id,
            instagram_username=ig_username,
        )

    def verify_instagram_username(
        self,
        page_info: MetaPageInfo,
        expected_username: str,
    ) -> None:
        expected = normalize_instagram_username(expected_username).lower()
        actual = (page_info.instagram_username or "").lower()
        if actual and actual != expected:
            raise MetaError(
                f"Facebook page is linked to Instagram @{page_info.instagram_username}, "
                f"expected @{expected}"
            )

    def create_resumable_upload_session(
        self,
        *,
        file_path: Path,
        file_type: str | None = None,
    ) -> MetaUploadSession:
        if not self.app_id:
            raise MetaError("META_APP_ID is required for resumable video uploads")

        file_path = file_path.resolve()
        if not file_path.is_file():
            raise MetaError(f"Video file not found: {file_path}")

        mime_type = file_type or _guess_video_mime(file_path)
        response = self._request(
            "POST",
            f"{self.app_id}/uploads",
            body={
                "file_name": "media_upload.mp4",
                "file_length": str(file_path.stat().st_size),
                "file_type": mime_type,
            },
        )
        session_id = response.get("id")
        if not isinstance(session_id, str) or not session_id.startswith("upload:"):
            raise MetaError("Meta resumable upload session response is missing upload id")
        return MetaUploadSession(session_id=session_id)

    def upload_resumable_file(
        self,
        session: MetaUploadSession,
        file_path: Path,
        *,
        chunk_size: int = UPLOAD_CHUNK_SIZE,
        max_attempts: int = UPLOAD_MAX_ATTEMPTS,
    ) -> str:
        file_path = file_path.resolve()
        if not file_path.is_file():
            raise MetaError(f"Video file not found: {file_path}")

        upload_url = f"{self.api_base}/{self.api_version}/{session.session_id}"
        file_size = file_path.stat().st_size
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                handle_value = self._upload_resumable_file_once(
                    upload_url,
                    file_path,
                    file_size=file_size,
                    chunk_size=chunk_size,
                )
                return handle_value
            except MetaError as exc:
                last_error = exc
            except requests.RequestException as exc:
                last_error = MetaError(f"Meta resumable upload failed: {exc}")

            if attempt < max_attempts:
                time.sleep(min(2**attempt, 30))

        assert last_error is not None
        raise last_error

    def _post_upload_chunk(
        self,
        upload_url: str,
        *,
        chunk: bytes,
        headers: dict[str, str],
        error_prefix: str,
        max_attempts: int = UPLOAD_CHUNK_MAX_ATTEMPTS,
    ) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.post(
                    upload_url,
                    data=chunk,
                    headers=headers,
                    timeout=600,
                )
                if response.ok:
                    return response
                detail = response.text.strip()
                if response.status_code == 429:
                    backoff = _parse_meta_upload_backoff(detail) or 60.0
                    if attempt < max_attempts:
                        time.sleep(backoff)
                        continue
                raise MetaError(
                    f"{error_prefix} with HTTP {response.status_code}: {detail}"
                )
            except MetaError as exc:
                last_error = exc
            except requests.RequestException as exc:
                last_error = MetaError(f"{error_prefix}: {exc}")

            if attempt < max_attempts:
                time.sleep(min(2**attempt, 30))

        assert last_error is not None
        raise last_error

    def _post_instagram_video_file(
        self,
        upload_url: str,
        *,
        video_path: Path,
        file_size: int,
        error_prefix: str,
        max_attempts: int = UPLOAD_MAX_ATTEMPTS,
    ) -> None:
        """Upload a local video file to Instagram rupload in one request."""
        payload = video_path.read_bytes()
        if len(payload) != file_size:
            raise MetaError(
                f"{error_prefix}: expected {file_size} bytes but read {len(payload)}"
            )
        headers = {
            "Authorization": f"OAuth {self.access_token}",
            "offset": "0",
            "file_size": str(file_size),
            "Content-Type": "application/octet-stream",
        }
        timeout = max(600, int(file_size / (512 * 1024)) + 120)
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.post(
                    upload_url,
                    data=payload,
                    headers=headers,
                    timeout=timeout,
                )
                if response.ok:
                    detail = response.text.strip()
                    if detail:
                        try:
                            parsed = json.loads(detail)
                        except json.JSONDecodeError:
                            parsed = None
                        if isinstance(parsed, dict) and parsed.get("success") is False:
                            raise MetaError(
                                f"{error_prefix} with HTTP {response.status_code}: {detail}"
                            )
                    return
                detail = response.text.strip()
                if response.status_code == 429:
                    backoff = _parse_meta_upload_backoff(detail) or 60.0
                    if attempt < max_attempts:
                        time.sleep(backoff)
                        continue
                raise MetaError(
                    f"{error_prefix} with HTTP {response.status_code}: {detail}"
                )
            except MetaError as exc:
                last_error = exc
            except requests.RequestException as exc:
                last_error = MetaError(f"{error_prefix}: {exc}")

            if attempt < max_attempts:
                time.sleep(min(2**attempt, 30))

        assert last_error is not None
        raise last_error

    def _upload_resumable_file_once(
        self,
        upload_url: str,
        file_path: Path,
        *,
        file_size: int,
        chunk_size: int,
    ) -> str:
        headers = {"Authorization": f"OAuth {self.access_token}"}
        response_data: dict[str, Any] | None = None
        offset = 0

        with file_path.open("rb") as handle:
            while offset < file_size:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                chunk_headers = {
                    **headers,
                    "file_offset": str(offset),
                    "Content-Type": "application/octet-stream",
                }
                response = self._post_upload_chunk(
                    upload_url,
                    chunk=chunk,
                    headers=chunk_headers,
                    error_prefix="Meta resumable upload failed",
                )
                offset += len(chunk)
                _sleep_before_next_upload_chunk(offset, file_size)
                if offset >= file_size and response.text.strip():
                    parsed = response.json()
                    if isinstance(parsed, dict):
                        response_data = parsed

        if not response_data:
            raise MetaError("Meta resumable upload returned an empty response")
        handle_value = response_data.get("h")
        if not isinstance(handle_value, str) or not handle_value:
            raise MetaError("Meta resumable upload response is missing upload handle")
        return handle_value

    def upload_video_handle(self, file_path: Path) -> str:
        session = self.create_resumable_upload_session(file_path=file_path)
        return self.upload_resumable_file(session, file_path)

    def _upload_page_video_chunked(
        self,
        page_id: str,
        video_path: Path,
        *,
        finish_fields: dict[str, str],
        chunk_size: int = UPLOAD_CHUNK_SIZE,
    ) -> str:
        path = video_path.resolve()
        if not path.is_file():
            raise MetaError(f"Video file not found: {path}")

        file_size = path.stat().st_size
        start = self._request(
            "POST",
            f"{page_id}/videos",
            body={
                "upload_phase": "start",
                "file_size": str(file_size),
            },
        )
        if not isinstance(start, dict):
            raise MetaError("Meta Facebook chunked upload start response is invalid")

        video_id = start.get("video_id")
        upload_session_id = start.get("upload_session_id")
        if not isinstance(video_id, str) or not video_id:
            raise MetaError(
                "Meta Facebook chunked upload start response is missing video_id"
            )
        if upload_session_id is None:
            raise MetaError(
                "Meta Facebook chunked upload start response is missing upload_session_id"
            )

        session_id = str(upload_session_id)
        mime_type = _guess_video_mime(path)
        offset = 0
        with path.open("rb") as handle:
            while offset < file_size:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                self._multipart_request(
                    f"{page_id}/videos",
                    fields={
                        "upload_phase": "transfer",
                        "upload_session_id": session_id,
                        "start_offset": str(offset),
                    },
                    files={
                        "video_file_chunk": (path.name, chunk, mime_type),
                    },
                )
                offset += len(chunk)
                _sleep_before_next_upload_chunk(offset, file_size)

        self._multipart_request(
            f"{page_id}/videos",
            fields={
                "upload_phase": "finish",
                "upload_session_id": session_id,
                **finish_fields,
            },
            files={},
        )
        return video_id

    def schedule_facebook_video(
        self,
        *,
        page_id: str,
        title: str,
        description: str = "",
        video_path: Path | None = None,
        video_url: str | None = None,
        publish_at: datetime | None = None,
        unpublished: bool = False,
        thumbnail_path: Path | None = None,
    ) -> str:
        if not video_path and not video_url:
            raise MetaError("A local video file or public video URL is required")

        fields: dict[str, str] = {
            "title": title,
            "description": description,
        }
        if publish_at is not None:
            publish_at = validate_publish_at(publish_at)
            fields["scheduled_publish_time"] = str(unix_timestamp(publish_at))
            fields["published"] = "false"
        elif unpublished:
            fields["published"] = "false"
        else:
            fields["published"] = "true"

        if video_url:
            fields["file_url"] = video_url
            response = self._multipart_request(f"{page_id}/videos", fields=fields, files={})
            video_id = response.get("id")
        else:
            assert video_path is not None
            video_id = self._upload_page_video_chunked(
                page_id,
                video_path,
                finish_fields=fields,
            )

        if not isinstance(video_id, str) or not video_id:
            raise MetaError("Meta Facebook video upload response is missing video id")
        if thumbnail_path is not None:
            self.set_facebook_video_thumbnail(video_id, thumbnail_path)
        return video_id

    def create_instagram_resumable_reel_container(
        self,
        *,
        instagram_account_id: str,
        caption: str,
        publish_at: datetime | None = None,
        trial_reel: bool = False,
        cover_url: str | None = None,
    ) -> tuple[str, str]:
        body: dict[str, str] = {
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption,
        }
        if trial_reel:
            body["trial_params"] = json.dumps({"graduation_strategy": "MANUAL"})
        if publish_at is not None:
            publish_at = validate_publish_at(publish_at)
            body["publish_at"] = str(unix_timestamp(publish_at))
        if cover_url:
            body["cover_url"] = cover_url

        response = self._request("POST", f"{instagram_account_id}/media", body=body)
        container_id = response.get("id")
        upload_uri = response.get("uri")
        if not isinstance(container_id, str) or not container_id:
            raise MetaError("Meta Instagram container response is missing container id")
        if not isinstance(upload_uri, str) or not upload_uri.strip():
            upload_uri = (
                f"https://rupload.facebook.com/ig-api-upload/"
                f"{self.api_version}/{container_id}"
            )
        return container_id, upload_uri.strip()

    def upload_instagram_resumable_video(
        self,
        *,
        container_id: str,
        video_path: Path,
        upload_uri: str | None = None,
        chunk_size: int = UPLOAD_CHUNK_SIZE,
        max_attempts: int = UPLOAD_MAX_ATTEMPTS,
    ) -> None:
        path = video_path.resolve()
        if not path.is_file():
            raise MetaError(f"Video file not found: {path}")

        target_uri = upload_uri or (
            f"https://rupload.facebook.com/ig-api-upload/"
            f"{self.api_version}/{container_id}"
        )
        file_size = path.stat().st_size
        self._post_instagram_video_file(
            target_uri,
            video_path=path,
            file_size=file_size,
            error_prefix="Meta Instagram resumable upload failed",
            max_attempts=max_attempts,
        )

    def create_instagram_media_container(
        self,
        *,
        instagram_account_id: str,
        caption: str,
        video_url: str | None = None,
        video_handle: str | None = None,
        publish_at: datetime | None = None,
        media_type: str = "REELS",
        trial_reel: bool = False,
        cover_url: str | None = None,
    ) -> str:
        if not video_url and not video_handle:
            raise MetaError("A public video URL or resumable upload handle is required")

        body: dict[str, str] = {
            "media_type": media_type,
            "caption": caption,
        }
        if media_type == "REELS":
            if video_handle:
                body["upload_type"] = "resumable"
                body["video_id"] = video_handle
            else:
                assert video_url is not None
                body["video_url"] = video_url
            if trial_reel:
                body["trial_params"] = json.dumps({"graduation_strategy": "MANUAL"})
        else:
            if trial_reel:
                raise MetaError("Trial reels are only supported for Instagram Reels")
            if video_handle:
                raise MetaError("Resumable upload is only supported for Instagram Reels")
            assert video_url is not None
            body["video_url"] = video_url

        if publish_at is not None:
            publish_at = validate_publish_at(publish_at)
            body["publish_at"] = str(unix_timestamp(publish_at))
        if cover_url and media_type == "REELS":
            body["cover_url"] = cover_url

        response = self._request("POST", f"{instagram_account_id}/media", body=body)
        container_id = response.get("id")
        if not isinstance(container_id, str) or not container_id:
            raise MetaError("Meta Instagram container response is missing container id")
        return container_id

    def get_container_status(self, container_id: str) -> MetaContainerStatus:
        response = self._request(
            "GET",
            container_id,
            query={"fields": "status_code,status"},
        )
        if not isinstance(response, dict):
            raise MetaError("Meta container status response is invalid")
        return MetaContainerStatus(
            id=container_id,
            status_code=response.get("status_code")
            if isinstance(response.get("status_code"), str)
            else None,
            status=response.get("status") if isinstance(response.get("status"), str) else None,
        )

    def wait_for_container(self, container_id: str) -> MetaContainerStatus:
        for _ in range(CONTAINER_POLL_MAX_ATTEMPTS):
            status = self.get_container_status(container_id)
            if status.status_code == "FINISHED":
                return status
            if status.status_code == "ERROR":
                detail = status.status or "unknown processing error"
                raise MetaError(
                    f"Meta Instagram container {container_id!r} failed: {detail}"
                )
            time.sleep(CONTAINER_POLL_INTERVAL_SECONDS)

        raise MetaError(
            f"Meta Instagram container {container_id!r} did not finish within the polling window"
        )

    def publish_instagram_container(
        self,
        *,
        instagram_account_id: str,
        container_id: str,
    ) -> str:
        response = self._request(
            "POST",
            f"{instagram_account_id}/media_publish",
            body={"creation_id": container_id},
        )
        media_id = response.get("id")
        if not isinstance(media_id, str) or not media_id:
            raise MetaError("Meta Instagram publish response is missing media id")
        return media_id

    def get_page_insights(
        self,
        page_id: str,
        *,
        metric: str,
        since: int,
        until: int,
        period: str = "day",
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"{page_id}/insights",
            query={
                "metric": metric,
                "period": period,
                "since": since,
                "until": until,
            },
        )
        if not isinstance(response, dict):
            raise MetaError("Meta page insights response is invalid")
        return response

    def get_instagram_account_insights(
        self,
        instagram_account_id: str,
        *,
        metric: str,
        since: int,
        until: int,
        period: str = "day",
        metric_type: str = "total_value",
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"{instagram_account_id}/insights",
            query={
                "metric": metric,
                "period": period,
                "since": since,
                "until": until,
                "metric_type": metric_type,
            },
        )
        if not isinstance(response, dict):
            raise MetaError("Meta Instagram insights response is invalid")
        return response

    def get_facebook_video_permalink(self, video_id: str) -> str:
        response = self._request(
            "GET",
            video_id,
            query={"fields": "permalink_url"},
        )
        if not isinstance(response, dict):
            raise MetaError("Meta Facebook permalink response is invalid")
        permalink = response.get("permalink_url")
        if not isinstance(permalink, str) or not permalink.strip():
            raise MetaError(
                f"Meta Facebook video {video_id!r} response is missing permalink_url"
            )
        return normalize_facebook_permalink(permalink)

    def get_instagram_media_permalink(self, media_id: str) -> str:
        response = self._request(
            "GET",
            media_id,
            query={"fields": "permalink"},
        )
        if not isinstance(response, dict):
            raise MetaError("Meta Instagram permalink response is invalid")
        permalink = response.get("permalink")
        if not isinstance(permalink, str) or not permalink.strip():
            raise MetaError(
                f"Meta Instagram media {media_id!r} response is missing permalink"
            )
        return permalink.strip()

    def _upload_facebook_reel_video(
        self,
        upload_url: str,
        video_path: Path,
        *,
        max_attempts: int = UPLOAD_MAX_ATTEMPTS,
    ) -> None:
        path = video_path.resolve()
        if not path.is_file():
            raise MetaError(f"Video file not found: {path}")

        headers = {
            "Authorization": f"OAuth {self.access_token}",
            "offset": "0",
            "Content-Type": "application/octet-stream",
        }
        file_size = path.stat().st_size
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                content = path.read_bytes()
                response = requests.post(
                    upload_url,
                    data=content,
                    headers={
                        **headers,
                        "Content-Length": str(len(content)),
                        "X-Entity-Length": str(file_size),
                    },
                    timeout=600,
                )
                if not response.ok:
                    detail = response.text.strip()
                    if response.status_code == 429:
                        backoff = _parse_meta_upload_backoff(detail) or 60.0
                        if attempt < max_attempts:
                            time.sleep(backoff)
                            continue
                    raise MetaError(
                        "Meta Facebook Reel upload failed with HTTP "
                        f"{response.status_code}: {detail}"
                    )
                return
            except MetaError as exc:
                last_error = exc
            except requests.RequestException as exc:
                last_error = MetaError(f"Meta Facebook Reel upload failed: {exc}")

            if attempt < max_attempts:
                time.sleep(min(2**attempt, 30))

        assert last_error is not None
        raise last_error

    def set_facebook_video_thumbnail(self, video_id: str, thumbnail_path: Path) -> None:
        path = thumbnail_path.resolve()
        if not path.is_file():
            raise MetaError(f"Thumbnail file not found: {path}")

        content = path.read_bytes()
        self._multipart_request(
            f"{video_id}/thumbnails",
            fields={"is_preferred": "true"},
            files={"source": (path.name, content, _guess_image_mime(path))},
        )

    def _resolve_reel_cover_asset(
        self,
        *,
        page_id: str | None,
        cover_path: Path | None,
        cover_url: str | None,
    ) -> FacebookHostingAsset | None:
        if cover_url or cover_path is None:
            return None
        if not page_id:
            raise MetaError(
                "page_id is required to host a local Reel cover image for Instagram"
            )
        return self.upload_unpublished_photo(page_id, cover_path)

    def cleanup_hosting_assets(self, *assets: FacebookHostingAsset | None) -> None:
        """Delete unpublished Facebook assets created only for Instagram ingestion."""
        for asset in assets:
            if asset is None:
                continue
            try:
                self._request("DELETE", asset.asset_id)
            except MetaError:
                continue

    def schedule_facebook_reel(
        self,
        *,
        page_id: str,
        title: str,
        description: str = "",
        video_path: Path | None = None,
        video_url: str | None = None,
        publish_at: datetime | None = None,
        unpublished: bool = False,
        thumbnail_path: Path | None = None,
    ) -> str:
        if not video_path and not video_url:
            raise MetaError("A local video file or public video URL is required")

        start = self._request(
            "POST",
            f"{page_id}/video_reels",
            body={"upload_phase": "start"},
        )
        video_id = start.get("video_id")
        upload_url = start.get("upload_url")
        if not isinstance(video_id, str) or not video_id:
            raise MetaError("Meta Facebook Reel start response is missing video_id")

        if video_path is not None:
            if not isinstance(upload_url, str) or not upload_url:
                raise MetaError("Meta Facebook Reel start response is missing upload_url")
            self._upload_facebook_reel_video(upload_url, video_path)

        finish: dict[str, str] = {
            "upload_phase": "finish",
            "video_id": video_id,
            "title": title,
            "description": description,
            # Without this, API Reels stay off the Page feed and show 0 Reach in
            # Business Suite while still accumulating Reels-tab plays. Manual
            # publishing defaults to sharing to feed; share_to_feed=false can
            # hide the Reel entirely (Meta developer forum reports).
            "share_to_feed": "true",
        }
        if video_url:
            finish["file_url"] = video_url
        # SCHEDULED = public at the given time (not a private draft).
        # DRAFT is only for explicit unpublished=True with no schedule.
        if publish_at is not None:
            publish_at = validate_publish_at(publish_at)
            finish["scheduled_publish_time"] = str(unix_timestamp(publish_at))
            finish["video_state"] = "SCHEDULED"
        elif unpublished:
            finish["video_state"] = "DRAFT"
        else:
            finish["video_state"] = "PUBLISHED"

        self._request("POST", f"{page_id}/video_reels", body=finish)
        if thumbnail_path is not None:
            self.set_facebook_video_thumbnail(video_id, thumbnail_path)
        return video_id

    def publish_existing_facebook_reel(
        self,
        *,
        page_id: str,
        video_id: str,
        title: str | None = None,
        description: str | None = None,
    ) -> None:
        """Mark an already-uploaded Facebook Reel as publicly published.

        Used to recover drafts created when privacy_status was incorrectly mapped
        to video_state=DRAFT. Calls finish with PUBLISHED on the existing video_id.
        """
        video_id = video_id.strip()
        if not video_id:
            raise MetaError("video_id is required to publish an existing Facebook Reel")

        finish: dict[str, str] = {
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "share_to_feed": "true",
        }
        if title is not None and title.strip():
            finish["title"] = title.strip()
        if description is not None and description.strip():
            finish["description"] = description.strip()

        response = self._request("POST", f"{page_id}/video_reels", body=finish)
        if isinstance(response, dict) and response.get("success") is False:
            raise MetaError(
                f"Meta refused to publish Facebook Reel {video_id!r}: {response}"
            )

    def upload_unpublished_video_url(
        self, page_id: str, video_path: Path
    ) -> FacebookHostingAsset:
        """Upload an unpublished page video and return its CDN URL for Instagram."""
        path = video_path.resolve()
        if not path.is_file():
            raise MetaError(f"Video file not found: {path}")

        video_id = self._upload_page_video_chunked(
            page_id,
            path,
            finish_fields={"published": "false"},
        )
        if not isinstance(video_id, str) or not video_id:
            raise MetaError("Meta Facebook video upload response is missing video id")

        for _ in range(CONTAINER_POLL_MAX_ATTEMPTS):
            video = self._request(
                "GET",
                video_id,
                query={"fields": "status,source"},
            )
            if isinstance(video, dict):
                source = video.get("source")
                if isinstance(source, str) and source.strip():
                    return FacebookHostingAsset(
                        asset_id=video_id,
                        url=source.strip(),
                        kind="video",
                    )
                status = video.get("status")
                if isinstance(status, dict):
                    video_status = status.get("video_status")
                    if video_status in {"error", "expired"}:
                        detail = status.get("processing_phase") or video_status
                        raise MetaError(
                            f"Meta Facebook video {video_id!r} processing failed: {detail}"
                        )
            time.sleep(CONTAINER_POLL_INTERVAL_SECONDS)

        raise MetaError(
            f"Meta Facebook video {video_id!r} source URL was not ready within the polling window"
        )

    def schedule_instagram_reel(
        self,
        *,
        instagram_account_id: str,
        caption: str,
        video_path: Path | None = None,
        video_url: str | None = None,
        page_id: str | None = None,
        publish_at: datetime | None = None,
        trial_reel: bool = False,
        cover_path: Path | None = None,
        cover_url: str | None = None,
        prefer_resumable_upload: bool = False,
    ) -> str:
        cover_asset = self._resolve_reel_cover_asset(
            page_id=page_id,
            cover_path=cover_path,
            cover_url=cover_url,
        )
        resolved_cover_url = cover_url or (cover_asset.url if cover_asset else None)

        use_resumable_local = False
        if video_path is not None and video_url is None:
            path = video_path.resolve()
            file_size = path.stat().st_size if path.is_file() else 0
            use_resumable_local = (
                prefer_resumable_upload
                or page_id is None
                or file_size <= INSTAGRAM_SINGLE_UPLOAD_MAX_BYTES
            )

        if use_resumable_local:
            try:
                container_id, upload_uri = self.create_instagram_resumable_reel_container(
                    instagram_account_id=instagram_account_id,
                    caption=caption,
                    publish_at=publish_at,
                    trial_reel=trial_reel,
                    cover_url=resolved_cover_url,
                )
                self.upload_instagram_resumable_video(
                    container_id=container_id,
                    video_path=video_path,
                    upload_uri=upload_uri,
                )
                self.wait_for_container(container_id)
                return self.publish_instagram_container(
                    instagram_account_id=instagram_account_id,
                    container_id=container_id,
                )
            finally:
                self.cleanup_hosting_assets(cover_asset)

        video_asset: FacebookHostingAsset | None = None
        hosted_url = video_url
        if hosted_url is None and video_path is not None and page_id:
            video_asset = self.upload_unpublished_video_url(page_id, video_path)
            hosted_url = video_asset.url
        if hosted_url is None:
            raise MetaError(
                "A public video URL, resumable local upload, or page_id for hosting "
                "is required to publish an Instagram Reel"
            )

        try:
            container_id = self.create_instagram_media_container(
                instagram_account_id=instagram_account_id,
                caption=caption,
                video_url=hosted_url,
                publish_at=publish_at,
                trial_reel=trial_reel,
                cover_url=resolved_cover_url,
            )
            self.wait_for_container(container_id)
            return self.publish_instagram_container(
                instagram_account_id=instagram_account_id,
                container_id=container_id,
            )
        finally:
            self.cleanup_hosting_assets(cover_asset, video_asset)

    def schedule_instagram_feed_video(
        self,
        *,
        instagram_account_id: str,
        caption: str,
        video_url: str,
        publish_at: datetime | None = None,
    ) -> str:
        return self.schedule_instagram_reel(
            instagram_account_id=instagram_account_id,
            caption=caption,
            video_url=video_url,
            publish_at=publish_at,
        )

    def upload_unpublished_photo(self, page_id: str, image_path: Path) -> FacebookHostingAsset:
        path = image_path.resolve()
        if not path.is_file():
            raise MetaError(f"Image file not found: {path}")

        content = path.read_bytes()
        response = self._multipart_request(
            f"{page_id}/photos",
            fields={"published": "false"},
            files={
                "source": (path.name, content, _guess_image_mime(path)),
            },
        )
        photo_id = response.get("id")
        if not isinstance(photo_id, str) or not photo_id:
            raise MetaError("Meta Facebook photo upload response is missing photo id")

        photo = self._request(
            "GET",
            photo_id,
            query={"fields": "images"},
        )
        images = photo.get("images") if isinstance(photo, dict) else None
        if not isinstance(images, list) or not images:
            raise MetaError(
                f"Meta Facebook photo {photo_id!r} response is missing image URLs"
            )

        best_url = ""
        best_width = -1
        for image in images:
            if not isinstance(image, dict):
                continue
            source = image.get("source")
            width = image.get("width")
            if not isinstance(source, str) or not source.strip():
                continue
            image_width = width if isinstance(width, int) else 0
            if image_width >= best_width:
                best_width = image_width
                best_url = source.strip()
        if not best_url:
            raise MetaError(
                f"Meta Facebook photo {photo_id!r} response has no usable image URL"
            )
        return FacebookHostingAsset(asset_id=photo_id, url=best_url, kind="photo")

    def upload_temporary_page_photo(self, page_id: str, image_path: Path) -> str:
        """Upload an unpublished temporary photo for use in a scheduled feed post."""
        path = image_path.resolve()
        if not path.is_file():
            raise MetaError(f"Image file not found: {path}")

        content = path.read_bytes()
        response = self._multipart_request(
            f"{page_id}/photos",
            fields={
                "published": "false",
                "temporary": "true",
            },
            files={
                "source": (path.name, content, _guess_image_mime(path)),
            },
        )
        photo_id = response.get("id")
        if not isinstance(photo_id, str) or not photo_id:
            raise MetaError("Meta Facebook photo upload response is missing photo id")
        return photo_id

    def create_facebook_photo_post(
        self,
        *,
        page_id: str,
        message: str,
        image_path: Path,
    ) -> str:
        """Publish an immediate Facebook Page photo post with caption.

        Returns the feed post id when Meta includes ``post_id``. Falls back to
        the photo id otherwise.
        """
        path = image_path.resolve()
        if not path.is_file():
            raise MetaError(f"Image file not found: {path}")
        text = message.strip()
        if not text:
            raise MetaError("Facebook photo post message is required")

        content = path.read_bytes()
        fields: dict[str, str] = {
            "published": "true",
            "caption": text,
        }
        response = self._multipart_request(
            f"{page_id}/photos",
            fields=fields,
            files={
                "source": (path.name, content, _guess_image_mime(path)),
            },
        )
        post_id = response.get("post_id")
        if isinstance(post_id, str) and post_id.strip():
            return post_id.strip()
        photo_id = response.get("id")
        if isinstance(photo_id, str) and photo_id.strip():
            return photo_id.strip()
        raise MetaError("Meta Facebook photo post response is missing post id")

    def create_facebook_feed_post(
        self,
        *,
        page_id: str,
        message: str,
    ) -> str:
        """Publish an immediate text-only Facebook Page feed post."""
        text = message.strip()
        if not text:
            raise MetaError("Facebook feed post message is required")
        response = self._request(
            "POST",
            f"{page_id}/feed",
            body={
                "message": text,
                "published": "true",
            },
        )
        post_id = response.get("id")
        if not isinstance(post_id, str) or not post_id:
            raise MetaError("Meta Facebook feed post response is missing post id")
        return post_id

    def create_facebook_comment(
        self,
        *,
        object_id: str,
        message: str,
    ) -> str:
        """Comment on a Page post (requires pages_manage_engagement)."""
        text = message.strip()
        if not text:
            raise MetaError("Facebook comment message is required")
        target = object_id.strip()
        if not target:
            raise MetaError("Facebook comment object_id is required")
        response = self._request(
            "POST",
            f"{target}/comments",
            body={"message": text},
        )
        comment_id = response.get("id")
        if not isinstance(comment_id, str) or not comment_id:
            raise MetaError("Meta Facebook comment response is missing comment id")
        return comment_id

    def create_facebook_draft_feed_post_with_photo(
        self,
        *,
        page_id: str,
        message: str,
        photo_id: str,
    ) -> str:
        body: dict[str, str] = {
            "published": "false",
            "unpublished_content_type": "DRAFT",
            "attached_media[0]": json.dumps({"media_fbid": photo_id}),
        }
        if message.strip():
            body["message"] = message.strip()

        response = self._request("POST", f"{page_id}/feed", body=body)
        post_id = response.get("id")
        if not isinstance(post_id, str) or not post_id:
            raise MetaError("Meta Facebook draft post response is missing post id")
        return post_id

    def schedule_facebook_feed_post_with_photo(
        self,
        *,
        page_id: str,
        message: str,
        photo_id: str,
        publish_at: datetime,
    ) -> str:
        publish_at = validate_publish_at(publish_at)
        body: dict[str, str] = {
            "published": "false",
            "scheduled_publish_time": str(unix_timestamp(publish_at)),
            "unpublished_content_type": "SCHEDULED",
            "attached_media[0]": json.dumps({"media_fbid": photo_id}),
        }
        if message.strip():
            body["message"] = message.strip()

        response = self._request("POST", f"{page_id}/feed", body=body)
        post_id = response.get("id")
        if not isinstance(post_id, str) or not post_id:
            raise MetaError("Meta Facebook feed post response is missing post id")
        return post_id

    def schedule_facebook_photo(
        self,
        *,
        page_id: str,
        caption: str = "",
        image_path: Path,
        publish_at: datetime | None = None,
        unpublished: bool = False,
    ) -> str:
        path = image_path.resolve()
        if not path.is_file():
            raise MetaError(f"Image file not found: {path}")

        if publish_at is None:
            if unpublished:
                photo_id = self.upload_temporary_page_photo(page_id, path)
                return self.create_facebook_draft_feed_post_with_photo(
                    page_id=page_id,
                    message=caption,
                    photo_id=photo_id,
                )

            fields: dict[str, str] = {"published": "true"}
            if caption.strip():
                fields["caption"] = caption.strip()
            content = path.read_bytes()
            response = self._multipart_request(
                f"{page_id}/photos",
                fields=fields,
                files={
                    "source": (path.name, content, _guess_image_mime(path)),
                },
            )
            post_id = response.get("post_id")
            photo_id = response.get("id")
            if isinstance(post_id, str) and post_id:
                return post_id
            if isinstance(photo_id, str) and photo_id:
                return f"{page_id}_{photo_id}"
            raise MetaError("Meta Facebook photo upload response is missing photo id")

        photo_id = self.upload_temporary_page_photo(page_id, path)
        return self.schedule_facebook_feed_post_with_photo(
            page_id=page_id,
            message=caption,
            photo_id=photo_id,
            publish_at=publish_at,
        )

    def get_facebook_post_permalink(self, post_id: str) -> str:
        response = self._request(
            "GET",
            post_id,
            query={"fields": "permalink_url"},
        )
        if not isinstance(response, dict):
            raise MetaError("Meta Facebook post permalink response is invalid")
        permalink = response.get("permalink_url")
        if isinstance(permalink, str) and permalink.strip():
            return normalize_facebook_permalink(permalink)
        return f"https://www.facebook.com/{post_id}"

    def get_facebook_photo_permalink(self, photo_id: str) -> str:
        if "_" in photo_id:
            return self.get_facebook_post_permalink(photo_id)
        response = self._request(
            "GET",
            photo_id,
            query={"fields": "link"},
        )
        if not isinstance(response, dict):
            raise MetaError("Meta Facebook photo permalink response is invalid")
        link = response.get("link")
        if isinstance(link, str) and link.strip():
            return link.strip()
        return f"https://www.facebook.com/photo/?fbid={photo_id}"

    def create_instagram_image_container(
        self,
        *,
        instagram_account_id: str,
        caption: str,
        image_url: str,
    ) -> str:
        body: dict[str, str] = {
            "image_url": image_url,
            "caption": caption,
        }

        response = self._request("POST", f"{instagram_account_id}/media", body=body)
        container_id = response.get("id")
        if not isinstance(container_id, str) or not container_id:
            raise MetaError("Meta Instagram image container response is missing container id")
        return container_id

    def schedule_instagram_image(
        self,
        *,
        instagram_account_id: str,
        caption: str,
        image_path: Path | None = None,
        image_url: str | None = None,
        page_id: str | None = None,
        publish_at: datetime | None = None,
    ) -> str:
        if publish_at is not None:
            current = datetime.now(timezone.utc)
            publish_time = publish_at
            if publish_time.tzinfo is None:
                publish_time = publish_time.replace(tzinfo=timezone.utc)
            if (publish_time - current).total_seconds() > 300:
                raise MetaError(
                    "Instagram does not support native scheduling via the Graph API. "
                    f"Run --quotes again near {publish_at.isoformat()} to publish on Instagram."
                )

        image_asset: FacebookHostingAsset | None = None
        hosted_url = image_url
        if hosted_url is None:
            if image_path is None:
                raise MetaError("An image file or public image URL is required")
            if not page_id:
                raise MetaError("page_id is required to host a local image for Instagram")
            image_asset = self.upload_unpublished_photo(page_id, image_path)
            hosted_url = image_asset.url

        try:
            container_id = self.create_instagram_image_container(
                instagram_account_id=instagram_account_id,
                caption=caption,
                image_url=hosted_url,
            )
            self.wait_for_container(container_id)
            return self.publish_instagram_container(
                instagram_account_id=instagram_account_id,
                container_id=container_id,
            )
        finally:
            self.cleanup_hosting_assets(image_asset)
