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
from typing import Any, Literal

from media_publisher.models import PublishJob, VideoFormat

DEFAULT_API_BASE = "https://api.canva.com/rest/v1"
AUTHORIZE_URL = "https://www.canva.com/api/oauth/authorize"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/callback"
DEFAULT_SCOPES = (
    "design:content:read",
    "design:meta:read",
    "folder:read",
    "folder:write",
)
QUOTES_LOOKUP_SCOPES = DEFAULT_SCOPES


def missing_canva_scopes(
    access_token: str,
    required: tuple[str, ...] = DEFAULT_SCOPES,
) -> list[str]:
    granted = set(decode_access_token_scopes(access_token))
    return [scope for scope in required if scope not in granted]


def canva_scope_help(access_token: str) -> str:
    missing = missing_canva_scopes(access_token)
    if not missing:
        return ""
    return (
        f"Missing Canva token scopes: {', '.join(missing)} "
        f"(granted: {format_access_token_scopes(access_token)}). "
        "Enable them in the Canva Developer Portal, then run "
        "`python -m media_publisher --canva-auth` and "
        "`python -m media_publisher --canva-auth-code <code>`."
    )


def _format_canva_http_error(method: str, path: str, code: int, detail: str) -> str:
    message = f"Canva {method} {path} failed with HTTP {code}: {detail}"
    try:
        payload = json.loads(detail)
    except json.JSONDecodeError:
        return message
    if not isinstance(payload, dict) or payload.get("code") != "missing_scope":
        return message
    return (
        f"{message} Re-authorize Canva with scopes: {' '.join(DEFAULT_SCOPES)} "
        "(run `python -m media_publisher --canva-auth`)."
    )
CANVA_LONG_VIDEO_THUMBNAILS_URL = "https://www.canva.com/folder/FAHOgLx_jAw"
CANVA_SHORT_VIDEO_THUMBNAILS_URL = "https://www.canva.com/folder/FAHOgF-NT8Q"
CANVA_QUOTES_FOLDER_URL = "https://www.canva.com/folder/FAF9ECD0M-k"
ORIGINAL_VIDEO_NAME_KEY = "Title"
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
CANVA_FOLDER_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?canva\.com/folder/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def decode_access_token_scopes(access_token: str) -> tuple[str, ...]:
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return ()
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        scopes = data.get("scopes")
        if isinstance(scopes, list):
            return tuple(scope for scope in scopes if isinstance(scope, str))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return ()
    return ()


def resolve_token_scope(
    access_token: str,
    response_scope: str | None = None,
) -> str | None:
    if isinstance(response_scope, str) and response_scope.strip():
        return response_scope.strip()
    decoded = decode_access_token_scopes(access_token)
    return " ".join(decoded) if decoded else None


def access_token_has_scope(access_token: str, scope: str) -> bool:
    return scope in decode_access_token_scopes(access_token)


def format_access_token_scopes(access_token: str) -> str:
    scopes = decode_access_token_scopes(access_token)
    return ", ".join(scopes) if scopes else "(none)"


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
class CanvaFolderSummary:
    id: str
    name: str


@dataclass(frozen=True)
class CanvaDesignSummary:
    id: str
    title: str
    page_count: int | None = None


@dataclass(frozen=True)
class CanvaDesignPageInfo:
    page_number: int
    title: str | None = None


@dataclass(frozen=True)
class CanvaThumbnailTarget:
    design_id: str
    page_number: int | None = None


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


def _normalize_resolved_canva_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.path.rstrip("/") != "/login":
        return url

    redirect = urllib.parse.parse_qs(parsed.query).get("redirect", [""])[0]
    if not redirect:
        return url

    redirect_path = urllib.parse.unquote(redirect)
    if redirect_path.startswith("/"):
        return f"https://www.canva.com{redirect_path}"
    return redirect_path


def resolve_canva_url(url: str) -> str:
    text = url.strip()
    if not is_shortlink(text):
        return text

    last_error: CanvaError | None = None
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(text, method=method)
        request.add_header("User-Agent", DEFAULT_USER_AGENT)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return _normalize_resolved_canva_url(response.url)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            last_error = CanvaError(
                f"Failed to resolve Canva short link {text!r}: HTTP {exc.code}: {detail}"
            )
        except urllib.error.URLError as exc:
            last_error = CanvaError(
                f"Failed to resolve Canva short link {text!r}: {exc.reason}"
            )

    if last_error is not None:
        raise last_error
    raise CanvaError(f"Failed to resolve Canva short link {text!r}")


def titles_match(expected: str, actual: str | None) -> bool:
    if not actual:
        return False
    return expected.casefold().strip() == actual.casefold().strip()


def thumbnail_catalog_url_for_format(
    video_format: VideoFormat,
    *,
    long_url: str | None = None,
    short_url: str | None = None,
) -> str:
    if video_format == "short_form":
        return short_url or CANVA_SHORT_VIDEO_THUMBNAILS_URL
    return long_url or CANVA_LONG_VIDEO_THUMBNAILS_URL


def catalog_video_name_from_job(job: PublishJob) -> str:
    return _field_text(job.metadata.get(ORIGINAL_VIDEO_NAME_KEY)) or job.title


def thumbnail_destination_path(download_dir: Path, video_name: str) -> Path:
    return download_dir / f"{_safe_filename(video_name)}.png"


_CACHED_THUMBNAIL_EXTENSIONS = (".png", ".jpg", ".jpeg", ".youtube-thumb.jpg")


def _thumbnail_lookup_names(video_name: str) -> list[str]:
    text = video_name.strip()
    if not text:
        return []

    names = [text]
    stripped = text.rstrip(" ,.")
    if stripped and stripped not in names:
        names.append(stripped)
    return names


def find_cached_thumbnail_path(download_dir: Path, video_name: str) -> Path | None:
    """Return a manually placed thumbnail if one exists under common naming variants."""
    if not download_dir.is_dir():
        return None

    for name in _thumbnail_lookup_names(video_name):
        base = _safe_filename(name)
        bases = [base]
        if not base.endswith(","):
            bases.append(f"{base},")
        for stem in bases:
            for extension in _CACHED_THUMBNAIL_EXTENSIONS:
                candidate = download_dir / f"{stem}{extension}"
                if candidate.is_file():
                    return candidate
    return None


def parse_canva_resource(value: str) -> tuple[Literal["folder", "design"], str]:
    text = value.strip()
    if not text:
        raise CanvaError("Canva resource URL is empty")
    if is_shortlink(text):
        text = resolve_canva_url(text)

    folder_match = CANVA_FOLDER_URL_RE.search(text)
    if folder_match:
        return "folder", folder_match.group(1)
    return "design", parse_design_id(text)


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

    stored_scope = payload.get("scope")
    scope = resolve_token_scope(
        access_token,
        stored_scope if isinstance(stored_scope, str) else None,
    )
    token_type = payload.get("token_type", "Bearer")
    return CanvaToken(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=float(expires_at),
        scope=scope,
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

    scope = resolve_token_scope(
        access_token,
        payload.get("scope") if isinstance(payload.get("scope"), str) else None,
    )
    token_type = payload.get("token_type", "Bearer")
    return CanvaToken(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=time.time() + float(expires_in),
        scope=scope,
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
            message = f"Canva token request failed with HTTP {exc.code}: {detail}"
            if "invalid_grant" in detail or "Token lineage has been revoked" in detail:
                message += (
                    " The stored refresh token is no longer valid. Re-authorize locally "
                    "(`python -m media_publisher --canva-auth` then `--canva-auth-code`), "
                    "then update the GitHub secret CANVA_TOKEN_JSON with the full contents "
                    "of credentials/canva-token.json. Canva refresh tokens are single-use: "
                    "if CI refreshed the token without CANVA_TOKEN_SYNC_PAT configured, "
                    "any older copy in GitHub Secrets is revoked."
                )
            raise CanvaError(message) from exc
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
        query: dict[str, str | int] | None = None,
    ) -> Any:
        access_token = self.ensure_access_token()
        url = f"{self.api_base}/{path.lstrip('/')}"
        if query:
            params = {key: str(value) for key, value in query.items()}
            url = f"{url}?{urllib.parse.urlencode(params)}"
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
                _format_canva_http_error(method, path, exc.code, detail)
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
        quality: int = 90,
    ) -> CanvaExportJob:
        format_body: dict[str, Any] = {"type": export_format}
        if export_format.casefold() in {"jpg", "jpeg"}:
            format_body["quality"] = quality
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

    def list_designs(
        self,
        *,
        query: str | None = None,
        continuation: str | None = None,
        limit: int = 25,
    ) -> tuple[list[CanvaDesignSummary], str | None]:
        params: dict[str, str | int] = {"limit": limit}
        if query:
            params["query"] = query
        if continuation:
            params["continuation"] = continuation

        response = self._request("GET", "designs", query=params)
        if not isinstance(response, dict):
            raise CanvaError("Canva designs response is invalid")

        designs: list[CanvaDesignSummary] = []
        for item in response.get("items", []):
            if not isinstance(item, dict):
                continue
            design_id = item.get("id")
            title = item.get("title")
            if not isinstance(design_id, str) or not isinstance(title, str):
                continue
            page_count = item.get("page_count")
            designs.append(
                CanvaDesignSummary(
                    id=design_id,
                    title=title,
                    page_count=page_count if isinstance(page_count, int) else None,
                )
            )

        next_continuation = response.get("continuation")
        if isinstance(next_continuation, str) and next_continuation.strip():
            return designs, next_continuation
        return designs, None

    def find_design_by_title(self, title: str) -> CanvaDesignSummary:
        target = title.casefold().strip()
        if not target:
            raise CanvaError("Canva design title is empty")

        continuation: str | None = None
        while True:
            designs, continuation = self.list_designs(
                query=title,
                continuation=continuation,
                limit=100,
            )
            for design in designs:
                if design.title.casefold().strip() == target:
                    return design
            if not continuation:
                break

        raise CanvaError(f"No Canva design found with title {title!r}")

    def get_design(self, design_id: str) -> CanvaDesignSummary:
        response = self._request("GET", f"designs/{design_id}")
        if not isinstance(response, dict):
            raise CanvaError("Canva design response is invalid")

        payload = response.get("design")
        if not isinstance(payload, dict):
            payload = response

        design_id_value = payload.get("id")
        title = payload.get("title")
        if not isinstance(design_id_value, str) or not isinstance(title, str):
            raise CanvaError(f"Canva design {design_id!r} response is missing id/title")

        page_count = payload.get("page_count")
        return CanvaDesignSummary(
            id=design_id_value,
            title=title,
            page_count=page_count if isinstance(page_count, int) else None,
        )

    def list_folder_designs(
        self,
        folder_id: str,
        *,
        continuation: str | None = None,
        limit: int = 100,
    ) -> tuple[list[CanvaDesignSummary], str | None]:
        params: dict[str, str | int] = {
            "limit": limit,
            "item_types": "design",
        }
        if continuation:
            params["continuation"] = continuation

        response = self._request("GET", f"folders/{folder_id}/items", query=params)
        if not isinstance(response, dict):
            raise CanvaError("Canva folder items response is invalid")

        designs: list[CanvaDesignSummary] = []
        for item in response.get("items", []):
            if not isinstance(item, dict) or item.get("type") != "design":
                continue
            design = item.get("design")
            if not isinstance(design, dict):
                continue
            design_id = design.get("id")
            title = design.get("title")
            if not isinstance(design_id, str) or not isinstance(title, str):
                continue
            page_count = design.get("page_count")
            designs.append(
                CanvaDesignSummary(
                    id=design_id,
                    title=title,
                    page_count=page_count if isinstance(page_count, int) else None,
                )
            )

        next_continuation = response.get("continuation")
        if isinstance(next_continuation, str) and next_continuation.strip():
            return designs, next_continuation
        return designs, None

    def find_design_in_folder(self, folder_id: str, title: str) -> CanvaDesignSummary:
        target = title.casefold().strip()
        if not target:
            raise CanvaError("Canva design title is empty")

        continuation: str | None = None
        while True:
            designs, continuation = self.list_folder_designs(
                folder_id,
                continuation=continuation,
            )
            for design in designs:
                if design.title.casefold().strip() == target:
                    return design
            if not continuation:
                break

        raise CanvaError(
            f"No Canva design found with title {title!r} in folder {folder_id!r}"
        )

    def list_folder_items(
        self,
        folder_id: str,
        *,
        item_types: str = "design,folder",
        continuation: str | None = None,
        limit: int = 100,
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: dict[str, str | int] = {
            "limit": limit,
            "item_types": item_types,
        }
        if continuation:
            params["continuation"] = continuation

        response = self._request("GET", f"folders/{folder_id}/items", query=params)
        if not isinstance(response, dict):
            raise CanvaError("Canva folder items response is invalid")

        items = response.get("items", [])
        if not isinstance(items, list):
            items = []
        next_continuation = response.get("continuation")
        if isinstance(next_continuation, str) and next_continuation.strip():
            return items, next_continuation
        return items, None

    def find_subfolder(self, folder_id: str, folder_name: str) -> CanvaFolderSummary | None:
        target = folder_name.casefold().strip()
        if not target:
            return None

        continuation: str | None = None
        while True:
            items, continuation = self.list_folder_items(
                folder_id,
                item_types="folder",
                continuation=continuation,
            )
            for item in items:
                if not isinstance(item, dict) or item.get("type") != "folder":
                    continue
                folder = item.get("folder")
                if not isinstance(folder, dict):
                    continue
                sub_id = folder.get("id")
                name = folder.get("name")
                if (
                    isinstance(sub_id, str)
                    and isinstance(name, str)
                    and name.casefold().strip() == target
                ):
                    return CanvaFolderSummary(id=sub_id, name=name)
            if not continuation:
                break
        return None

    def move_folder_item(self, *, item_id: str, to_folder_id: str) -> None:
        self._request(
            "POST",
            "folders/move",
            body={
                "item_id": item_id,
                "to_folder_id": to_folder_id,
            },
        )

    def list_design_pages_info(self, design_id: str) -> list[CanvaDesignPageInfo]:
        pages: list[CanvaDesignPageInfo] = []
        offset = 1
        while True:
            response = self._request(
                "GET",
                f"designs/{design_id}/pages",
                query={"offset": offset, "limit": 200},
            )
            if not isinstance(response, dict):
                raise CanvaError("Canva design pages response is invalid")

            items = response.get("items", [])
            if not isinstance(items, list) or not items:
                break

            for item in items:
                if not isinstance(item, dict):
                    continue
                page_number = item.get("page_number")
                if not isinstance(page_number, int):
                    page_number = item.get("index")
                if not isinstance(page_number, int):
                    continue
                page_title = item.get("title")
                if not isinstance(page_title, str):
                    page_title = item.get("name")
                if not isinstance(page_title, str):
                    page_title = item.get("page_name")
                pages.append(
                    CanvaDesignPageInfo(
                        page_number=page_number,
                        title=page_title if isinstance(page_title, str) else None,
                    )
                )

            if len(items) < 200:
                break
            offset += len(items)

        return pages

    def resolve_thumbnail_target(
        self,
        catalog_ref: str,
        video_title: str,
    ) -> CanvaThumbnailTarget:
        return resolve_thumbnail_target(self, catalog_ref, video_title)

    def download_thumbnail_target(
        self,
        target: CanvaThumbnailTarget,
        destination: Path,
        *,
        export_format: str = "png",
    ) -> Path:
        pages = [target.page_number] if target.page_number is not None else None
        return self.download_design_image(
            target.design_id,
            destination,
            export_format=export_format,
            pages=pages,
        )

    def download_design_pdf(self, design_id: str, destination: Path) -> Path:
        job = self.export_design(design_id, export_format="pdf")
        return self.download_file(job.urls[0], destination)

    def list_design_pages(self, design_id: str) -> list[int]:
        return [page.page_number for page in self.list_design_pages_info(design_id)]

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
        quality: int = 90,
    ) -> CanvaExportJob:
        job = self.create_export_job(
            design_id,
            export_format=export_format,
            pages=pages,
            quality=quality,
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
        split_pages: bool = False,
    ) -> list[Path]:
        design_id = parse_design_id(design_ref)
        download_dir.mkdir(parents=True, exist_ok=True)

        if split_pages and pages:
            raise CanvaError("Use either split_pages or pages, not both")

        if split_pages:
            page_numbers = self.list_design_pages(design_id)
            downloaded: list[Path] = []
            for page_number in page_numbers:
                job = self.export_design(
                    design_id,
                    export_format=export_format,
                    pages=[page_number],
                )
                destination = download_dir / f"{design_id}_page{page_number}.{export_format}"
                downloaded.append(self.download_file(job.urls[0], destination))
            return downloaded

        job = self.export_design(
            design_id,
            export_format=export_format,
            pages=pages,
        )
        downloaded = []
        for index, url in enumerate(job.urls, start=1):
            suffix = f"_page{index}" if len(job.urls) > 1 else ""
            destination = download_dir / f"{design_id}{suffix}.{export_format}"
            downloaded.append(self.download_file(url, destination))
        return downloaded

    def test_connection(self) -> CanvaToken:
        self.ensure_access_token()
        return load_token(self.token_path)


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


def monthly_quotes_pdf_path(
    download_dir: Path,
    *,
    year: int,
    month: int,
    variant: str | None = None,
) -> Path:
    suffix = f"-{variant}" if variant else ""
    return download_dir / f"quotes-{year:04d}-{month:02d}{suffix}.pdf"


QUOTES_DESIGN_MANIFEST_FILENAME = ".quotes-designs.json"


def quotes_design_manifest_path(download_dir: Path) -> Path:
    return download_dir / QUOTES_DESIGN_MANIFEST_FILENAME


def quotes_design_month_key(year: int, month: int, *, variant: str | None = None) -> str:
    key = f"{year:04d}-{month:02d}"
    if variant:
        return f"{key}-{variant}"
    return key


def load_quotes_design_manifest(download_dir: Path) -> dict[str, str]:
    path = quotes_design_manifest_path(download_dir)
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise CanvaError(f"Invalid quotes design manifest: {path}")
    return {
        key: value
        for key, value in payload.items()
        if isinstance(key, str) and isinstance(value, str) and value.strip()
    }


def save_quotes_design_manifest(download_dir: Path, manifest: dict[str, str]) -> None:
    path = quotes_design_manifest_path(download_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _missing_design_meta_scope(exc: CanvaError) -> bool:
    message = str(exc).casefold()
    return "missing_scope" in message and "design:meta:read" in message


def resolve_quotes_design(
    client: CanvaClient,
    *,
    design_title: str,
    quotes_folder_id: str | None = None,
) -> CanvaDesignSummary:
    access_token = client.ensure_access_token()
    has_meta = access_token_has_scope(access_token, "design:meta:read")
    has_folder = access_token_has_scope(access_token, "folder:read")

    if quotes_folder_id and has_folder:
        return client.find_design_in_folder(quotes_folder_id, design_title)
    if has_meta:
        return client.find_design_by_title(design_title)

    raise CanvaError(
        f"Cannot search Canva for {design_title!r}: saved token scopes are "
        f"{format_access_token_scopes(access_token)}. "
        "Enable design:meta:read in the Canva Developer Portal, then re-run "
        "--canva-auth and --canva-auth-code so the new scopes are granted."
    )


def ensure_monthly_quotes_pdf(
    client: CanvaClient,
    download_dir: Path,
    *,
    year: int,
    month: int,
    design_title: str,
    design_id: str | None = None,
    quotes_folder_id: str | None = None,
    variant: str | None = None,
) -> Path:
    """Download the monthly quotes PDF from Canva if it is not cached locally."""
    download_dir.mkdir(parents=True, exist_ok=True)
    destination = monthly_quotes_pdf_path(
        download_dir,
        year=year,
        month=month,
        variant=variant,
    )
    if destination.is_file():
        return destination

    month_key = quotes_design_month_key(year, month, variant=variant)
    manifest = load_quotes_design_manifest(download_dir)
    resolved_design_id = ""

    try:
        design = resolve_quotes_design(
            client,
            design_title=design_title,
            quotes_folder_id=quotes_folder_id,
        )
        resolved_design_id = design.id
    except CanvaError as search_exc:
        resolved_design_id = (design_id or manifest.get(month_key) or "").strip()
        if not resolved_design_id:
            access_token = client.ensure_access_token()
            manifest_path = quotes_design_manifest_path(download_dir)
            raise CanvaError(
                f"Cannot resolve Canva design {design_title!r}. "
                f"Saved token scopes: {format_access_token_scopes(access_token)}. "
                f"{search_exc} "
                f"Alternatively, add the design id to {manifest_path.name} "
                f"under key {month_key!r}."
            ) from search_exc

    client.download_design_pdf(resolved_design_id, destination)
    manifest[month_key] = resolved_design_id
    save_quotes_design_manifest(download_dir, manifest)
    return destination


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
    split_pages: bool = False,
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
        split_pages=split_pages,
    )


def resolve_thumbnail_target(
    client: CanvaClient,
    catalog_ref: str,
    video_title: str,
) -> CanvaThumbnailTarget:
    """Locate a thumbnail design/page in Canva for a catalog video title."""
    access_token = client.ensure_access_token()
    resource_type, resource_id = parse_canva_resource(catalog_ref)

    if resource_type == "folder":
        if "folder:read" not in decode_access_token_scopes(access_token):
            raise CanvaError(
                f"Cannot search Canva folder for {video_title!r}. "
                f"{canva_scope_help(access_token)}"
            )
        design = client.find_design_in_folder(resource_id, video_title)
        return CanvaThumbnailTarget(design_id=design.id)

    pages = client.list_design_pages_info(resource_id)
    for page in pages:
        if titles_match(video_title, page.title):
            return CanvaThumbnailTarget(
                design_id=resource_id,
                page_number=page.page_number,
            )

    if "design:meta:read" not in decode_access_token_scopes(access_token):
        raise CanvaError(
            f"Cannot search Canva for thumbnail {video_title!r}. "
            f"{canva_scope_help(access_token)}"
        )

    try:
        design = client.find_design_by_title(video_title)
        return CanvaThumbnailTarget(design_id=design.id)
    except CanvaError:
        catalog_design = client.get_design(resource_id)
        if titles_match(video_title, catalog_design.title):
            return CanvaThumbnailTarget(design_id=resource_id)

    raise CanvaError(
        f"No Canva design matching {video_title!r} found for catalog {catalog_ref!r}"
    )


def ensure_catalog_thumbnail_from_canva(
    job: PublishJob,
    *,
    client: CanvaClient,
    download_dir: Path,
    long_catalog_url: str | None = None,
    short_catalog_url: str | None = None,
) -> PublishJob:
    """Download a catalog thumbnail PNG from Canva and attach it to the publish job."""
    lookup_name = catalog_video_name_from_job(job)
    cached = find_cached_thumbnail_path(download_dir, lookup_name)
    if cached is not None:
        return replace(job, thumbnail_path=str(cached))

    destination = thumbnail_destination_path(download_dir, lookup_name)

    catalog_ref = thumbnail_catalog_url_for_format(
        job.video_format,
        long_url=long_catalog_url,
        short_url=short_catalog_url,
    )
    target = client.resolve_thumbnail_target(catalog_ref, lookup_name)
    client.download_thumbnail_target(target, destination)
    return replace(job, thumbnail_path=str(destination))
