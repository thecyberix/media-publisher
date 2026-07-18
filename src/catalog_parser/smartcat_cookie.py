from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from catalog_parser.smartcat import (
    DEFAULT_UI_BASE,
    SmartcatError,
    find_matching_document,
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def load_storage_state_cookies(storage_state_path: Path) -> list[dict[str, Any]]:
    if not storage_state_path.exists():
        raise SmartcatError(
            f"Smartcat browser session not found at {storage_state_path}. "
            "Run: python -m catalog_parser --smartcat-import-session COOKIES_JSON"
        )

    payload = json.loads(storage_state_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SmartcatError(
            f"Invalid Smartcat session file {storage_state_path}: expected a JSON object"
        )
    cookies = payload.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        raise SmartcatError(
            f"Invalid Smartcat session file {storage_state_path}: "
            "expected a top-level 'cookies' array"
        )
    return cookies


def cookies_header(cookies: list[dict[str, Any]], *, host: str) -> str:
    host = host.lower().removeprefix("www.")
    relevant: list[str] = []
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        domain = str(cookie.get("domain", "")).lower().removeprefix(".")
        if domain and host not in domain and not host.endswith(f".{domain}"):
            continue
        relevant.append(f"{name}={value}")
    if not relevant:
        raise SmartcatError(
            "Smartcat session file has no cookies for "
            f"{host!r}. Re-run: python -m catalog_parser --smartcat-login"
        )
    return "; ".join(relevant)


def ensure_storage_state_file(
    storage_state_path: Path,
    *,
    project_root: Path | None = None,
) -> Path | None:
    if storage_state_path.is_file():
        return storage_state_path

    import os

    json_blob = os.getenv("SMARTCAT_STORAGE_STATE_JSON", "").strip()
    if not json_blob:
        return None

    if not storage_state_path.is_absolute() and project_root is not None:
        storage_state_path = project_root / storage_state_path
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    storage_state_path.write_text(json_blob, encoding="utf-8")
    return storage_state_path


def find_document_via_web_api(
    api_json: Callable[..., Any],
    project_id: str,
    *,
    search: str | None,
    title: str | None,
) -> dict[str, Any]:
    file_items = api_json(
        "POST",
        f"/api/Projects/{project_id}/FileItemIds",
        body={
            "isFolderMode": True,
            "orderBy": 0,
            "desc": False,
            "filter": {
                "searchName": search or title or "",
                "createdByAccountUserIds": [],
                "targetLanguageIds": [],
                "documentTargetStatuses": [],
                "stageNumbersWithNoAssignments": [],
                "stageNumbersWithIncompleteState": [],
                "creationDateFrom": None,
                "creationDateTo": None,
            },
        },
    )
    if not isinstance(file_items, list) or not file_items:
        raise SmartcatError(
            "Could not find a Smartcat document for "
            f"search={search!r} title={title!r}"
        )

    page_content = api_json(
        "POST",
        f"/api/Projects/{project_id}/PageContent",
        body={
            "filter": None,
            "fileItems": file_items,
            "loadPreviews": False,
        },
    )
    documents_payload = page_content.get("documents") if isinstance(page_content, dict) else None
    if not isinstance(documents_payload, dict) or not documents_payload:
        raise SmartcatError(
            "Smartcat returned no document details for "
            f"search={search!r} title={title!r}"
        )

    documents = list(documents_payload.values())
    document = find_matching_document(documents, search=search, title=title)
    if document is None:
        raise SmartcatError(
            "Could not find a Smartcat document for "
            f"search={search!r} title={title!r}"
        )
    return document


class SmartcatCookieClient:
    """Call Smartcat web APIs using cookies from a Playwright storage-state file."""

    def __init__(
        self,
        *,
        ui_base: str = DEFAULT_UI_BASE,
        storage_state_path: Path,
    ) -> None:
        self.ui_base = ui_base.rstrip("/")
        self.storage_state_path = storage_state_path
        self._cookies = load_storage_state_cookies(storage_state_path)
        self._host = urllib.parse.urlparse(self.ui_base).netloc

    def web_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> tuple[int, bytes]:
        url = f"{self.ui_base}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"

        data = None
        headers = {
            "Cookie": cookies_header(self._cookies, host=self._host),
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        }
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()
            return exc.code, detail

    def _api_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any | None = None,
    ) -> Any:
        status, payload = self.web_request(method, path, params=params, json_body=body)
        if status >= 400:
            detail = payload.decode("utf-8", errors="replace")[:500]
            raise SmartcatError(
                f"Smartcat API {method} {path} failed with HTTP {status}: {detail}"
            )
        if not payload:
            return None
        return json.loads(payload.decode("utf-8"))

    def find_document(
        self,
        project_id: str,
        *,
        search: str | None,
        title: str | None,
    ) -> dict[str, Any]:
        return find_document_via_web_api(
            self._api_json,
            project_id,
            search=search,
            title=title,
        )

    def verify_session(self, *, probe_project_id: str | None = None) -> None:
        status, _ = self.web_request("GET", "/projects")
        if status in {401, 403}:
            raise SmartcatError(
                "Smartcat session rejected (HTTP "
                f"{status}). Run: python -m catalog_parser --smartcat-login"
            )
        if status >= 400:
            raise SmartcatError(
                f"Smartcat session check failed with HTTP {status}. "
                "Run: python -m catalog_parser --smartcat-login"
            )

        if probe_project_id:
            status, body = self.web_request(
                "POST",
                f"/api/Projects/{probe_project_id}/FileItemIds",
                json_body={
                    "isFolderMode": True,
                    "orderBy": 0,
                    "desc": False,
                    "filter": {
                        "searchName": "",
                        "createdByAccountUserIds": [],
                        "targetLanguageIds": [],
                        "documentTargetStatuses": [],
                        "stageNumbersWithNoAssignments": [],
                        "stageNumbersWithIncompleteState": [],
                        "creationDateFrom": None,
                        "creationDateTo": None,
                    },
                },
            )
            if status in {401, 403}:
                raise SmartcatError(
                    "Smartcat session rejected by project API (HTTP "
                    f"{status}). Run: python -m catalog_parser --smartcat-login"
                )
            if status >= 400:
                detail = body.decode("utf-8", errors="replace")[:500]
                raise SmartcatError(
                    f"Smartcat project API probe failed with HTTP {status}: {detail}"
                )


def build_cookie_client_from_env(*, project_root: Path | None = None) -> SmartcatCookieClient:
    import os

    from catalog_parser.smartcat_web import DEFAULT_STORAGE_STATE

    storage_state = Path(
        os.getenv("SMARTCAT_STORAGE_STATE", DEFAULT_STORAGE_STATE)
    ).expanduser()
    if not storage_state.is_absolute() and project_root is not None:
        storage_state = project_root / storage_state

    ensure_storage_state_file(storage_state, project_root=project_root)
    return SmartcatCookieClient(
        ui_base=os.getenv("SMARTCAT_UI_BASE", DEFAULT_UI_BASE).strip() or DEFAULT_UI_BASE,
        storage_state_path=storage_state,
    )


def _normalize_same_site(value: Any) -> str:
    if not isinstance(value, str):
        return "Lax"
    normalized = value.strip().lower().replace("_", " ")
    if normalized in {"none", "no restriction", "no_restriction"}:
        return "None"
    if normalized == "strict":
        return "Strict"
    return "Lax"


def normalize_browser_cookie(raw: dict[str, Any]) -> dict[str, Any] | None:
    name = raw.get("name")
    value = raw.get("value")
    if not isinstance(name, str) or not isinstance(value, str) or not name:
        return None

    domain = raw.get("domain")
    if not isinstance(domain, str) or not domain.strip():
        host_only = raw.get("hostOnly")
        if host_only is True and isinstance(raw.get("host"), str):
            domain = raw["host"]
        else:
            return None

    path = raw.get("path")
    if not isinstance(path, str) or not path:
        path = "/"

    expires = raw.get("expires")
    if expires is None and raw.get("expirationDate") is not None:
        expiration = raw.get("expirationDate")
        if isinstance(expiration, (int, float)):
            expires = float(expiration)
    if isinstance(expires, (int, float)) and expires > 0:
        expires_value: float = float(expires)
    else:
        expires_value = -1.0

    return {
        "name": name,
        "value": value,
        "domain": domain.strip(),
        "path": path,
        "expires": expires_value,
        "httpOnly": bool(raw.get("httpOnly", False)),
        "secure": bool(raw.get("secure", True)),
        "sameSite": _normalize_same_site(raw.get("sameSite")),
    }


def cookies_from_import_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        if (
            payload.get("version") == 2
            and isinstance(payload.get("data"), str)
            and "cookies" not in payload
        ):
            raise SmartcatError(
                "This Hot Cleaner export is password-encrypted and cannot be imported directly.\n"
                "In Cookie Editor: Import → choose your file → enter your password → Import.\n"
                "Then Export → JSON again, leave the encryption password blank, save the file,\n"
                "and run: python -m catalog_parser --smartcat-import-session PLAIN.json"
            )
        if isinstance(payload.get("cookies"), list):
            raw_cookies = payload["cookies"]
        else:
            raise SmartcatError(
                "Expected a JSON array of cookies or a Playwright storage-state object "
                "with a top-level 'cookies' array."
            )
    elif isinstance(payload, list):
        raw_cookies = payload
    else:
        raise SmartcatError(
            "Expected a JSON array of cookies or a Playwright storage-state object."
        )

    cookies: list[dict[str, Any]] = []
    for raw in raw_cookies:
        if not isinstance(raw, dict):
            continue
        normalized = normalize_browser_cookie(raw)
        if normalized is not None:
            cookies.append(normalized)
    if not cookies:
        raise SmartcatError("No usable cookies found in the import file.")
    return cookies


def build_storage_state(cookies: list[dict[str, Any]]) -> dict[str, Any]:
    return {"cookies": cookies, "origins": []}


def write_storage_state(storage_state_path: Path, cookies: list[dict[str, Any]]) -> None:
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    storage_state_path.write_text(
        json.dumps(build_storage_state(cookies), indent=2) + "\n",
        encoding="utf-8",
    )


def import_browser_session_file(
    source_path: Path,
    destination_path: Path,
    *,
    ui_base: str = DEFAULT_UI_BASE,
    probe_project_id: str | None = None,
) -> None:
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    cookies = cookies_from_import_payload(payload)
    write_storage_state(destination_path, cookies)

    client = SmartcatCookieClient(
        ui_base=ui_base,
        storage_state_path=destination_path,
    )
    client.verify_session(probe_project_id=probe_project_id)


def print_smartcat_import_instructions(*, ui_base: str = DEFAULT_UI_BASE) -> None:
    print(
        "Import Smartcat session from your normal browser (no Playwright, no GitHub secret):\n"
        f"  1. Log in to {ui_base.rstrip('/')}/projects in Chrome or Edge.\n"
        "  2. Install Cookie-Editor (Moustachauve) — https://cookie-editor.com/\n"
        "     Chrome: https://chromewebstore.google.com/detail/cookie-editor/"
        "hlkenndednhckejemloddpdbaiedlgil\n"
        "     Do not use Hot Cleaner Cookie Editor (exports are password-encrypted only).\n"
        "  3. On a Smartcat page, open Cookie-Editor → Export → JSON.\n"
        "     It copies plain JSON to the clipboard (no password).\n"
        "  4. Paste into a file, e.g. smartcat-cookies.json\n"
        "  5. Run:\n"
        "       python -m catalog_parser --smartcat-import-session smartcat-cookies.json\n"
        "     This writes smartcat-state.json and verifies the session.\n"
        "\n"
        "To refresh GitHub Actions later, copy the new smartcat-state.json into the\n"
        "SMARTCAT_STORAGE_STATE_JSON secret (write-only — you never need to read it)."
    )
