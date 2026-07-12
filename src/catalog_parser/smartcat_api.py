from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from catalog_parser.smartcat import (
    DEFAULT_TARGET_LANGUAGE,
    SmartcatError,
    find_bulgarian_srt_document,
    find_matching_document,
    parse_pkg_sm_link,
    pick_document_download_url,
)

DEFAULT_API_BASE = "https://ea.smartcat.ai"
DEFAULT_EXPORT_MODE = "current"
DEFAULT_EXPORT_TYPE = "target"
EXPORT_POLL_INTERVAL_SECONDS = 1.0
EXPORT_MAX_WAIT_SECONDS = 120.0


def build_document_language_id(document_id: str, language_id: str) -> str:
    return f"{document_id}_{language_id}"


def build_export_download_link(api_base: str, task_id: str) -> str:
    return (
        f"{api_base.rstrip('/')}/api/integration/v1/document/export/"
        f"{urllib.parse.quote(task_id, safe='')}"
    )


class SmartcatApiClient:
    """Company-account integration API. Requires Settings > API credentials."""

    def __init__(self, account_id: str, api_key: str, api_base: str = DEFAULT_API_BASE) -> None:
        self.api_base = api_base.rstrip("/")
        self._auth_header = (
            "Basic "
            + base64.b64encode(f"{account_id}:{api_key}".encode("utf-8")).decode("ascii")
        )
        self._project_cache: dict[str, dict[str, Any]] = {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        accept: str = "application/json",
    ) -> tuple[int, bytes, dict[str, str]]:
        url = f"{self.api_base}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query, doseq=True)}"

        request = urllib.request.Request(url, method=method)
        request.add_header("Authorization", self._auth_header)
        request.add_header("Accept", accept)

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                headers = {key.lower(): value for key, value in response.headers.items()}
                return response.status, response.read(), headers
        except urllib.error.HTTPError as exc:
            body = exc.read()
            headers = {key.lower(): value for key, value in exc.headers.items()}
            return exc.code, body, headers

    def _request_json(self, method: str, path: str, *, query: dict[str, str] | None = None) -> Any:
        status, body, _headers = self._request(method, path, query=query)
        if status >= 400:
            detail = body.decode("utf-8", errors="replace").strip()
            raise SmartcatError(
                f"Smartcat API {method} {path} failed with HTTP {status}: {detail}"
            )
        if not body:
            return None
        return json.loads(body.decode("utf-8"))

    def get_project(self, project_id: str) -> dict[str, Any]:
        if project_id not in self._project_cache:
            project = self._request_json("GET", f"/api/integration/v1/project/{project_id}")
            if not isinstance(project, dict):
                raise SmartcatError(f"Unexpected project response for {project_id!r}")
            self._project_cache[project_id] = project
        return self._project_cache[project_id]

    def request_document_export(self, composite_document_id: str) -> str:
        query = {
            "documentIds": composite_document_id,
            "type": DEFAULT_EXPORT_TYPE,
            "mode": DEFAULT_EXPORT_MODE,
        }
        status, body, headers = self._request(
            "POST",
            "/api/integration/v1/document/export",
            query=query,
        )
        if status >= 400:
            detail = body.decode("utf-8", errors="replace").strip()
            raise SmartcatError(
                "Smartcat export request failed for "
                f"{composite_document_id!r} (HTTP {status}): {detail}"
            )
        if not body:
            raise SmartcatError(
                f"Smartcat export request for {composite_document_id!r} returned no task id"
            )

        content_type = headers.get("content-type", "")
        if "json" in content_type:
            payload = json.loads(body.decode("utf-8"))
            task_id = _extract_task_id(payload)
            if task_id:
                return task_id

        text = body.decode("utf-8", errors="replace").strip().strip('"')
        if text:
            return text

        raise SmartcatError(
            f"Could not parse export task id for {composite_document_id!r}: {body!r}"
        )

    def wait_for_export(self, task_id: str) -> None:
        deadline = time.monotonic() + EXPORT_MAX_WAIT_SECONDS
        path = f"/api/integration/v1/document/export/{urllib.parse.quote(task_id, safe='')}"

        while time.monotonic() < deadline:
            status, _body, _headers = self._request("GET", path, accept="*/*")
            if status == 200:
                return
            if status in {202, 204, 404}:
                time.sleep(EXPORT_POLL_INTERVAL_SECONDS)
                continue
            raise SmartcatError(f"Smartcat export task {task_id!r} failed with HTTP {status}")

        raise SmartcatError(
            f"Timed out waiting for Smartcat export task {task_id!r} "
            f"after {EXPORT_MAX_WAIT_SECONDS:.0f}s"
        )

    def resolve_bulgarian_srt_link(
        self,
        pkg_sm_link: str,
        *,
        title: str | None = None,
        language: str = DEFAULT_TARGET_LANGUAGE,
    ) -> str:
        parsed = parse_pkg_sm_link(pkg_sm_link)
        if parsed is None:
            raise SmartcatError(f"Could not parse Smartcat link: {pkg_sm_link!r}")

        project = self.get_project(parsed.project_id)
        documents = project.get("documents") or []
        if not isinstance(documents, list):
            raise SmartcatError(
                f"Project {parsed.project_id!r} returned an unexpected documents payload"
            )

        srt_document = find_bulgarian_srt_document(
            documents,
            search=parsed.search,
            title=title,
        )
        if srt_document is not None:
            download_url = pick_document_download_url(srt_document)
            if download_url:
                return download_url

            document_id = srt_document.get("id")
            if isinstance(document_id, str) and document_id:
                language_id = resolve_target_language_id(project, language)
                composite_id = build_document_language_id(document_id, language_id)
                task_id = self.request_document_export(composite_id)
                self.wait_for_export(task_id)
                return build_export_download_link(self.api_base, task_id)

        document = find_matching_document(
            documents,
            search=parsed.search,
            title=title,
        )
        if document is None:
            raise SmartcatError(
                "Could not find a Smartcat document for "
                f"search={parsed.search!r} title={title!r}"
            )

        document_id = document.get("id")
        if not isinstance(document_id, str) or not document_id:
            raise SmartcatError(f"Matched Smartcat document is missing an id: {document!r}")

        language_id = resolve_target_language_id(project, language)
        composite_id = build_document_language_id(document_id, language_id)
        task_id = self.request_document_export(composite_id)
        self.wait_for_export(task_id)
        return build_export_download_link(self.api_base, task_id)


def _extract_task_id(payload: Any) -> str | None:
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    if isinstance(payload, dict):
        for key in ("id", "taskId", "exportTaskId"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def resolve_target_language_id(project: dict[str, Any], language: str) -> str:
    from catalog_parser.smartcat import BULGARIAN_LANGUAGE_ALIASES

    language_norm = language.strip().lower()
    aliases = {language_norm}
    if language_norm in BULGARIAN_LANGUAGE_ALIASES:
        aliases |= {code.lower() for code in BULGARIAN_LANGUAGE_ALIASES}

    for key in ("targetLanguages", "languages"):
        raw = project.get(key)
        if not isinstance(raw, list):
            continue
        for value in raw:
            code = _language_code(value)
            if code and code.lower() in aliases:
                return code

    documents = project.get("documents") or []
    for document in documents:
        for key in ("targetLanguages", "languages"):
            raw = document.get(key)
            if not isinstance(raw, list):
                continue
            for value in raw:
                code = _language_code(value)
                if code and code.lower() in aliases:
                    return code

    return language


def _language_code(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("id", "languageId", "code", "name", "language"):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw
    return None
