from __future__ import annotations

import json
import mimetypes
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_API_VERSION = "v21.0"
DEFAULT_GRAPH_BASE = "https://graph.facebook.com"
CONTAINER_POLL_INTERVAL_SECONDS = 5.0
CONTAINER_POLL_MAX_ATTEMPTS = 120
MIN_SCHEDULE_LEAD_SECONDS = 600
MAX_SCHEDULE_LEAD_SECONDS = 60 * 60 * 24 * 75


class MetaError(RuntimeError):
    pass


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


def normalize_facebook_page_username(value: str) -> str:
    text = value.strip().rstrip("/")
    if "facebook.com/" in text.lower():
        return text.rsplit("/", 1)[-1].split("?")[0]
    return text


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
        url = f"{self._graph_url(path)}?{urllib.parse.urlencode(params)}"
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
                "file_name": file_path.name,
                "file_length": str(file_path.stat().st_size),
                "file_type": mime_type,
            },
        )
        session_id = response.get("id")
        if not isinstance(session_id, str) or not session_id.startswith("upload:"):
            raise MetaError("Meta resumable upload session response is missing upload id")
        return MetaUploadSession(session_id=session_id)

    def upload_resumable_file(self, session: MetaUploadSession, file_path: Path) -> str:
        file_path = file_path.resolve()
        if not file_path.is_file():
            raise MetaError(f"Video file not found: {file_path}")

        upload_url = f"{self.api_base}/{self.api_version}/{session.session_id}"
        params = urllib.parse.urlencode({"access_token": self.access_token})
        url = f"{upload_url}?{params}"
        content = file_path.read_bytes()

        request = urllib.request.Request(url, data=content, method="POST")
        request.add_header("Authorization", f"OAuth {self.access_token}")
        request.add_header("file_offset", "0")

        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise MetaError(
                f"Meta resumable upload failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise MetaError(f"Meta resumable upload failed: {exc.reason}") from exc

        if not payload:
            raise MetaError("Meta resumable upload returned an empty response")
        response_data = json.loads(payload.decode("utf-8"))
        handle = response_data.get("h")
        if not isinstance(handle, str) or not handle:
            raise MetaError("Meta resumable upload response is missing upload handle")
        return handle

    def upload_video_handle(self, file_path: Path) -> str:
        session = self.create_resumable_upload_session(file_path=file_path)
        return self.upload_resumable_file(session, file_path)

    def schedule_facebook_video(
        self,
        *,
        page_id: str,
        title: str,
        description: str = "",
        video_path: Path | None = None,
        video_url: str | None = None,
        publish_at: datetime | None = None,
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
        else:
            fields["published"] = "true"

        if video_url:
            fields["file_url"] = video_url
            response = self._multipart_request(f"{page_id}/videos", fields=fields, files={})
        else:
            assert video_path is not None
            path = video_path.resolve()
            content = path.read_bytes()
            response = self._multipart_request(
                f"{page_id}/videos",
                fields=fields,
                files={
                    "source": (path.name, content, _guess_video_mime(path)),
                },
            )

        video_id = response.get("id")
        if not isinstance(video_id, str) or not video_id:
            raise MetaError("Meta Facebook video upload response is missing video id")
        return video_id

    def create_instagram_media_container(
        self,
        *,
        instagram_account_id: str,
        caption: str,
        video_url: str | None = None,
        video_handle: str | None = None,
        publish_at: datetime | None = None,
    ) -> str:
        if not video_url and not video_handle:
            raise MetaError("A public video URL or resumable upload handle is required")

        body: dict[str, str] = {
            "media_type": "REELS",
            "caption": caption,
        }
        if video_handle:
            body["upload_type"] = "resumable"
            body["video_id"] = video_handle
        else:
            assert video_url is not None
            body["video_url"] = video_url

        if publish_at is not None:
            publish_at = validate_publish_at(publish_at)
            body["publish_at"] = str(unix_timestamp(publish_at))

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

    def schedule_instagram_reel(
        self,
        *,
        instagram_account_id: str,
        caption: str,
        video_path: Path | None = None,
        video_url: str | None = None,
        publish_at: datetime | None = None,
    ) -> str:
        video_handle: str | None = None
        if video_path and not video_url:
            video_handle = self.upload_video_handle(video_path)

        container_id = self.create_instagram_media_container(
            instagram_account_id=instagram_account_id,
            caption=caption,
            video_url=video_url,
            video_handle=video_handle,
            publish_at=publish_at,
        )
        self.wait_for_container(container_id)
        return self.publish_instagram_container(
            instagram_account_id=instagram_account_id,
            container_id=container_id,
        )
