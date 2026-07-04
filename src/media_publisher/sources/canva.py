from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from media_publisher.models import PublishJob

DEFAULT_API_BASE = "https://api.canva.com/rest/v1"
AUTHORIZE_URL = "https://www.canva.com/api/oauth/authorize"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/callback"
DEFAULT_SCOPES = ("design:content:read",)
EXPORT_POLL_INTERVAL_SECONDS = 2.0
EXPORT_POLL_MAX_ATTEMPTS = 60

METADATA_CANVA_DESIGN_ID = "canva_design_id"
FIELD_CANVA_DESIGN = "Canva Design"

CANVA_DESIGN_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?canva\.com/design/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
CANVA_SHORTLINK_RE = re.compile(
    r"(?:https?://)?canva\.link/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; media-publisher/0.1)"


class CanvaError(RuntimeError):
    pass


@dataclass(frozen=True)
class CanvaToken:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str | None = None
    token_type: str = "Bearer"


@dataclass(frozen=True)
class CanvaExportJob:
    id: str
    status: str
    urls: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class CanvaPendingAuth:
    code_verifier: str
    state: str
    redirect_uri: str


def _field_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    text = value.strip()
    return text or None


def is_shortlink(value: str) -> bool:
    return bool(CANVA_SHORTLINK_RE.search(value.strip()))


def resolve_canva_url(url: str) -> str:
    text = url.strip()
    if not is_shortlink(text):
        return text

    request = urllib.request.Request(text, method="GET")
    request.add_header("User-Agent", DEFAULT_USER_AGENT)
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
    try:
        with opener.open(request, timeout=30) as response:
            return response.url
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise CanvaError(
            f"Failed to resolve Canva short link {text!r}: HTTP {exc.code}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise CanvaError(
            f"Failed to resolve Canva short link {text!r}: {exc.reason}"
        ) from exc


def parse_design_id(value: str) -> str:
    text = value.strip()
    if not text:
        raise CanvaError("Canva design ID is empty")

    if is_shortlink(text):
        text = resolve_canva_url(text)

    match = CANVA_DESIGN_URL_RE.search(text)
    if match:
        return match.group(1)

    return text


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", name).strip(" .")
    return cleaned or "thumbnail"


def generate_code_verifier() -> str:
    return secrets.token_urlsafe(96)[:128]


def generate_code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_state() -> str:
    return secrets.token_urlsafe(48)


def build_authorization_url(
    *,
    client_id: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    scopes: tuple[str, ...] = DEFAULT_SCOPES,
    code_verifier: str | None = None,
    state: str | None = None,
) -> tuple[str, CanvaPendingAuth]:
    verifier = code_verifier or generate_code_verifier()
    auth_state = state or generate_state()
    challenge = generate_code_challenge(verifier)
    query = urllib.parse.urlencode(
        {
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": " ".join(scopes),
            "response_type": "code",
            "client_id": client_id.strip(),
            "state": auth_state,
            "redirect_uri": redirect_uri,
        }
    )
    pending = CanvaPendingAuth(
        code_verifier=verifier,
        state=auth_state,
        redirect_uri=redirect_uri,
    )
    return f"{AUTHORIZE_URL}?{query}", pending


def save_pending_auth(path: Path, pending: CanvaPendingAuth) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "code_verifier": pending.code_verifier,
                "state": pending.state,
                "redirect_uri": pending.redirect_uri,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def load_pending_auth(path: Path) -> CanvaPendingAuth:
    if not path.exists():
        raise CanvaError(f"Pending Canva auth file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise CanvaError("Pending Canva auth file is invalid")
    verifier = payload.get("code_verifier")
    state = payload.get("state")
    redirect_uri = payload.get("redirect_uri")
    if not isinstance(verifier, str) or not isinstance(state, str):
        raise CanvaError("Pending Canva auth file is missing code_verifier or state")
    if not isinstance(redirect_uri, str) or not redirect_uri.strip():
        redirect_uri = DEFAULT_REDIRECT_URI
    return CanvaPendingAuth(
        code_verifier=verifier,
        state=state,
        redirect_uri=redirect_uri,
    )


def save_token(path: Path, token: CanvaToken) -> None:
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


def load_token(path: Path) -> CanvaToken:
    if not path.exists():
        raise CanvaError(f"Canva token file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise CanvaError("Canva token file is invalid")

    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    expires_at = payload.get("expires_at")
    if not isinstance(access_token, str) or not access_token:
        raise CanvaError("Canva token file is missing access_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise CanvaError("Canva token file is missing refresh_token")
    if not isinstance(expires_at, (int, float)):
        raise CanvaError("Canva token file is missing expires_at")

    scope = payload.get("scope")
    token_type = payload.get("token_type", "Bearer")
    return CanvaToken(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=float(expires_at),
        scope=scope if isinstance(scope, str) else None,
        token_type=token_type if isinstance(token_type, str) else "Bearer",
    )


def token_from_response(payload: dict[str, Any]) -> CanvaToken:
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    if not isinstance(access_token, str) or not access_token:
        raise CanvaError("Canva token response is missing access_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise CanvaError("Canva token response is missing refresh_token")
    if not isinstance(expires_in, (int, float)):
        raise CanvaError("Canva token response is missing expires_in")

    scope = payload.get("scope")
    token_type = payload.get("token_type", "Bearer")
    return CanvaToken(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=time.time() + float(expires_in),
        scope=scope if isinstance(scope, str) else None,
        token_type=token_type if isinstance(token_type, str) else "Bearer",
    )


def _parse_export_job(payload: dict[str, Any]) -> CanvaExportJob:
    job = payload.get("job")
    if not isinstance(job, dict):
        raise CanvaError("Canva export response is missing job")

    job_id = job.get("id")
    status = job.get("status")
    if not isinstance(job_id, str) or not isinstance(status, str):
        raise CanvaError("Canva export response has invalid job payload")

    urls_payload = job.get("urls", [])
    urls: list[str] = []
    if isinstance(urls_payload, list):
        urls = [url for url in urls_payload if isinstance(url, str)]

    error = job.get("error")
    error_code = None
    error_message = None
    if isinstance(error, dict):
        if isinstance(error.get("code"), str):
            error_code = error["code"]
        if isinstance(error.get("message"), str):
            error_message = error["message"]

    return CanvaExportJob(
        id=job_id,
        status=status,
        urls=tuple(urls),
        error_code=error_code,
        error_message=error_message,
    )


class CanvaClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_path: Path,
        *,
        api_base: str = DEFAULT_API_BASE,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        pending_auth_path: Path | None = None,
    ) -> None:
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.token_path = token_path
        self.api_base = api_base.rstrip("/")
        self.redirect_uri = redirect_uri.strip() or DEFAULT_REDIRECT_URI
        self.pending_auth_path = pending_auth_path or token_path.with_name(
            "canva-auth-pending.json"
        )
        if not self.client_id:
            raise CanvaError("CANVA_CLIENT_ID is required")
        if not self.client_secret:
            raise CanvaError("CANVA_CLIENT_SECRET is required")

    def _basic_auth_header(self) -> str:
        credentials = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("utf-8")
        ).decode("ascii")
        return f"Basic {credentials}"

    def _token_request(self, form: dict[str, str]) -> CanvaToken:
        data = urllib.parse.urlencode(form).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api_base}/oauth/token",
            data=data,
            method="POST",
        )
        request.add_header("Authorization", self._basic_auth_header())
        request.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise CanvaError(
                f"Canva token request failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise CanvaError(f"Canva token request failed: {exc.reason}") from exc

        if not isinstance(payload, dict):
            raise CanvaError("Canva token response is not a JSON object")
        return token_from_response(payload)

    def exchange_authorization_code(
        self,
        code: str,
        *,
        code_verifier: str,
        redirect_uri: str | None = None,
    ) -> CanvaToken:
        token = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code.strip(),
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri or self.redirect_uri,
            }
        )
        save_token(self.token_path, token)
        return token

    def refresh_access_token(self, refresh_token: str) -> CanvaToken:
        token = self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
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
            client_id=self.client_id,
            redirect_uri=self.redirect_uri,
        )
        save_pending_auth(self.pending_auth_path, pending)
        return url

    def complete_authorization(self, code: str, *, state: str | None = None) -> CanvaToken:
        pending = load_pending_auth(self.pending_auth_path)
        if state is not None and state != pending.state:
            raise CanvaError("Canva authorization state does not match")
        token = self.exchange_authorization_code(
            code,
            code_verifier=pending.code_verifier,
            redirect_uri=pending.redirect_uri,
        )
        if self.pending_auth_path.exists():
            self.pending_auth_path.unlink()
        return token

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> Any:
        access_token = self.ensure_access_token()
        url = f"{self.api_base}/{path.lstrip('/')}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {access_token}")
        request.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise CanvaError(
                f"Canva {method} {url} failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise CanvaError(f"Canva request failed: {exc.reason}") from exc

        if not payload:
            return {}
        return json.loads(payload.decode("utf-8"))

    def create_export_job(
        self,
        design_id: str,
        *,
        export_format: str = "png",
        pages: list[int] | None = None,
    ) -> CanvaExportJob:
        format_body: dict[str, Any] = {"type": export_format}
        if pages:
            format_body["pages"] = pages

        response = self._request(
            "POST",
            "exports",
            body={
                "design_id": design_id,
                "format": format_body,
            },
        )
        if not isinstance(response, dict):
            raise CanvaError("Canva export response is invalid")
        return _parse_export_job(response)

    def get_export_job(self, export_id: str) -> CanvaExportJob:
        response = self._request("GET", f"exports/{export_id}")
        if not isinstance(response, dict):
            raise CanvaError("Canva export response is invalid")
        return _parse_export_job(response)

    def wait_for_export_job(self, export_id: str) -> CanvaExportJob:
        for _ in range(EXPORT_POLL_MAX_ATTEMPTS):
            job = self.get_export_job(export_id)
            if job.status == "success":
                if not job.urls:
                    raise CanvaError(
                        f"Canva export job {export_id!r} succeeded without download URLs"
                    )
                return job
            if job.status == "failed":
                detail = job.error_message or job.error_code or "unknown error"
                raise CanvaError(f"Canva export job {export_id!r} failed: {detail}")
            time.sleep(EXPORT_POLL_INTERVAL_SECONDS)

        raise CanvaError(
            f"Canva export job {export_id!r} did not complete within the polling window"
        )

    def export_design(
        self,
        design_id: str,
        *,
        export_format: str = "png",
        pages: list[int] | None = None,
    ) -> CanvaExportJob:
        job = self.create_export_job(
            design_id,
            export_format=export_format,
            pages=pages,
        )
        if job.status == "success" and job.urls:
            return job
        return self.wait_for_export_job(job.id)

    def download_file(self, url: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, method="GET")

        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                destination.write_bytes(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise CanvaError(
                f"Canva download failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise CanvaError(f"Canva download failed: {exc.reason}") from exc

        return destination

    def download_design_image(
        self,
        design_id: str,
        destination: Path,
        *,
        export_format: str = "png",
        pages: list[int] | None = None,
    ) -> Path:
        job = self.export_design(
            design_id,
            export_format=export_format,
            pages=pages,
        )
        return self.download_file(job.urls[0], destination)

    def download_design_images(
        self,
        design_ref: str,
        download_dir: Path,
        *,
        export_format: str = "png",
        pages: list[int] | None = None,
    ) -> list[Path]:
        design_id = parse_design_id(design_ref)
        job = self.export_design(
            design_id,
            export_format=export_format,
            pages=pages,
        )
        download_dir.mkdir(parents=True, exist_ok=True)
        downloaded: list[Path] = []
        for index, url in enumerate(job.urls, start=1):
            suffix = f"_page{index}" if len(job.urls) > 1 else ""
            destination = download_dir / f"{design_id}{suffix}.{export_format}"
            downloaded.append(self.download_file(url, destination))
        return downloaded

    def test_connection(self) -> CanvaToken:
        token = load_token(self.token_path)
        refreshed = self.refresh_access_token(token.refresh_token)
        return refreshed


def design_id_from_job(job: PublishJob) -> str | None:
    metadata_id = _field_text(job.metadata.get(METADATA_CANVA_DESIGN_ID))
    if metadata_id:
        return parse_design_id(metadata_id)
    return None


def enrich_job_from_canva(
    job: PublishJob,
    *,
    client_id: str,
    client_secret: str,
    token_path: Path,
    download_dir: Path,
    api_base: str = DEFAULT_API_BASE,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    design_id: str | None = None,
    export_format: str = "png",
) -> PublishJob:
    """Export a Canva design and attach the downloaded image as the job thumbnail."""
    resolved_design_id = design_id or design_id_from_job(job)
    if not resolved_design_id:
        raise CanvaError(
            f"Publish job is missing {METADATA_CANVA_DESIGN_ID!r} metadata"
        )

    client = CanvaClient(
        client_id,
        client_secret,
        token_path,
        api_base=api_base,
        redirect_uri=redirect_uri,
    )
    filename_base = job.airtable_record_id or _safe_filename(job.title)
    destination = download_dir / f"{filename_base}_thumbnail.{export_format}"
    client.download_design_image(
        resolved_design_id,
        destination,
        export_format=export_format,
    )

    metadata = dict(job.metadata)
    metadata[METADATA_CANVA_DESIGN_ID] = resolved_design_id
    return replace(job, thumbnail_path=str(destination), metadata=metadata)


def download_images_from_canva_url(
    url: str,
    *,
    client_id: str,
    client_secret: str,
    token_path: Path,
    download_dir: Path,
    api_base: str = DEFAULT_API_BASE,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    export_format: str = "png",
    pages: list[int] | None = None,
) -> list[Path]:
    """Resolve a Canva URL (including canva.link short links) and download exported images."""
    client = CanvaClient(
        client_id,
        client_secret,
        token_path,
        api_base=api_base,
        redirect_uri=redirect_uri,
    )
    return client.download_design_images(
        url,
        download_dir,
        export_format=export_format,
        pages=pages,
    )
