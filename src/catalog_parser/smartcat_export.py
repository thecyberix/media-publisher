from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Protocol

from catalog_parser.smartcat_cookie import SmartcatCookieClient
from catalog_parser.smartcat import (
    DEFAULT_UI_BASE,
    SmartcatError,
    build_pkg_sm_link,
    configured_target_language,
    find_document_by_id,
    find_matching_document,
    get_language_target,
    parse_pkg_sm_link,
    parse_smartcat_resource_link,
    resolve_language_id,
)
from catalog_parser.smartcat_api import SmartcatApiClient, resolve_target_language_id
from catalog_parser.smartcat_web import SmartcatWebClient

WEB_EXPORT_TYPE_SOURCE = 0
WEB_EXPORT_TYPE_TARGET = 1
# Confirmed/translated segments — mode 0 often returns English source text for type=1.
WEB_SEGMENT_EXPORT_MODE_SOURCE = 0
WEB_SEGMENT_EXPORT_MODE_TARGET = 1
WEB_EXPORT_POLL_INTERVAL_SECONDS = 1.0
WEB_EXPORT_MAX_WAIT_SECONDS = 120.0


@dataclass(frozen=True)
class SmartcatDocumentContext:
    project_id: str
    document_id: str
    document_name: str
    search: str | None
    source_language_id: str
    target_language_id: str


class SmartcatSrtExporter(Protocol):
    def export_bilingual_pair(
        self,
        context: SmartcatDocumentContext,
    ) -> tuple[str, str]: ...


class SmartcatWebRequestClient(Protocol):
    ui_base: str

    def web_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> tuple[int, bytes]: ...


def decode_srt_bytes(body: bytes) -> str:
    if not body:
        return ""
    if body.startswith((b"\xff\xfe", b"\xfe\xff")):
        return body.decode("utf-16")
    if body.startswith(b"\xef\xbb\xbf"):
        return body.decode("utf-8-sig")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("utf-16", errors="replace")


def export_document_srt_via_web_api(
    client: SmartcatWebRequestClient,
    document_id: str,
    language_id: int,
    *,
    export_type: int,
    segment_export_mode: int = 0,
    destination: int = 0,
) -> str:
    params = urllib.parse.urlencode(
        {
            "type": export_type,
            "destination": destination,
            "segmentExportMode": segment_export_mode,
            "withTags": "false",
            "documentExportRequestSource": 0,
        }
    )
    create_status, create_body = client.web_request(
        "POST",
        f"/api/Documents/ExportTasks?{params}",
        json_body=[{"documentId": document_id, "languageId": language_id}],
    )
    if create_status >= 400:
        detail = create_body.decode("utf-8", errors="replace")[:500]
        raise SmartcatError(
            "Smartcat web export request failed for "
            f"{document_id!r} (HTTP {create_status}): {detail}"
        )

    task_id = json.loads(create_body.decode("utf-8"))
    if not isinstance(task_id, str) or not task_id.strip():
        raise SmartcatError(
            f"Smartcat web export returned no task id: {create_body.decode('utf-8')!r}"
        )

    deadline = time.monotonic() + WEB_EXPORT_MAX_WAIT_SECONDS
    while time.monotonic() < deadline:
        download_status, body = client.web_request(
            "GET",
            f"/api/Documents/Download/{task_id}",
        )
        if download_status == 200 and body:
            return decode_srt_bytes(body)
        if download_status in {202, 204, 404}:
            time.sleep(WEB_EXPORT_POLL_INTERVAL_SECONDS)
            continue
        detail = body.decode("utf-8", errors="replace")[:500]
        raise SmartcatError(
            f"Smartcat web export download for task {task_id!r} "
            f"failed with HTTP {download_status}: {detail}"
        )

    raise SmartcatError(
        f"Timed out waiting for Smartcat web export task {task_id!r} "
        f"after {WEB_EXPORT_MAX_WAIT_SECONDS:.0f}s"
    )


def resolve_source_language_id(document: dict[str, Any], project: dict[str, Any]) -> str:
    for key in ("sourceLanguageId", "sourceLanguage"):
        raw = document.get(key)
        if isinstance(raw, int):
            return str(raw)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        if isinstance(raw, dict):
            for subkey in ("id", "languageId", "code"):
                value = raw.get(subkey)
                if value is not None and str(value).strip():
                    return str(value).strip()

    for key in ("sourceLanguages", "languages"):
        raw = project.get(key)
        if not isinstance(raw, list):
            continue
        for value in raw:
            if isinstance(value, dict):
                language_id = value.get("id") or value.get("languageId")
                if language_id is not None:
                    return str(language_id)
            elif isinstance(value, (str, int)):
                return str(value)

    return "9"


def resolve_document_context(
    api_client: SmartcatApiClient,
    *,
    project_id: str,
    document_id: str | None = None,
    search: str | None = None,
    title: str | None = None,
    target_language: str = DEFAULT_TARGET_LANGUAGE,
) -> SmartcatDocumentContext:
    project = api_client.get_project(project_id)
    documents = project.get("documents") or []
    if not isinstance(documents, list):
        raise SmartcatError(f"Project {project_id!r} returned an unexpected documents payload")

    document: dict[str, Any] | None = None
    if document_id:
        document = find_document_by_id(documents, document_id)
    if document is None:
        document = find_matching_document(documents, search=search, title=title)
    if document is None:
        raise SmartcatError(
            "Could not find a Smartcat document for "
            f"project={project_id!r} document_id={document_id!r} "
            f"search={search!r} title={title!r}"
        )

    resolved_document_id = document.get("id")
    if not isinstance(resolved_document_id, str) or not resolved_document_id:
        raise SmartcatError(f"Matched Smartcat document is missing an id: {document!r}")

    target_language_id = resolve_target_language_id(project, target_language)
    source_language_id = resolve_source_language_id(document, project)
    target = get_language_target(document, resolve_language_id(target_language))
    if target is not None and target.get("languageId") is not None:
        target_language_id = str(target["languageId"])

    document_name = str(document.get("name") or document.get("fileName") or title or search or "")
    return SmartcatDocumentContext(
        project_id=project_id,
        document_id=resolved_document_id,
        document_name=document_name,
        search=search,
        source_language_id=source_language_id,
        target_language_id=target_language_id,
    )


def resolve_context_from_smartcat_link(
    api_client: SmartcatApiClient,
    smartcat_link: str,
    *,
    title: str | None = None,
    target_language: str = DEFAULT_TARGET_LANGUAGE,
) -> SmartcatDocumentContext:
    parsed_project = parse_pkg_sm_link(smartcat_link)
    if parsed_project is not None:
        return resolve_document_context(
            api_client,
            project_id=parsed_project.project_id,
            search=parsed_project.search,
            title=title,
            target_language=target_language,
        )

    parsed_editor = parse_smartcat_resource_link(smartcat_link)
    if parsed_editor is None:
        raise SmartcatError(f"Could not parse Smartcat link: {smartcat_link!r}")
    if not parsed_editor.project_id:
        raise SmartcatError(
            f"Smartcat link is missing project context: {smartcat_link!r}"
        )

    return resolve_document_context(
        api_client,
        project_id=parsed_editor.project_id,
        document_id=parsed_editor.document_id,
        search=parsed_editor.search or title,
        title=title,
        target_language=target_language,
    )


class SmartcatApiSrtExporter:
    def __init__(self, client: SmartcatApiClient) -> None:
        self._client = client

    def export_bilingual_pair(
        self,
        context: SmartcatDocumentContext,
    ) -> tuple[str, str]:
        source_srt = self._client.export_document_srt(
            context.document_id,
            context.source_language_id,
            export_type="source",
        )
        target_srt = self._client.export_document_srt(
            context.document_id,
            context.target_language_id,
            export_type="target",
        )
        return source_srt, target_srt


class SmartcatWebSrtExporter:
    def __init__(self, client: SmartcatWebRequestClient) -> None:
        self._client = client

    def export_bilingual_pair(
        self,
        context: SmartcatDocumentContext,
    ) -> tuple[str, str]:
        # Smartcat web ExportTasks keys off the target language id.
        # type=0 + segmentExportMode=0 → English source text
        # type=1 + segmentExportMode=1 → confirmed Bulgarian translations
        # (segmentExportMode=0 for type=1 often returns English again)
        language_id = int(context.target_language_id)
        source_srt = export_document_srt_via_web_api(
            self._client,
            context.document_id,
            language_id,
            export_type=WEB_EXPORT_TYPE_SOURCE,
            segment_export_mode=WEB_SEGMENT_EXPORT_MODE_SOURCE,
        )
        target_srt = export_document_srt_via_web_api(
            self._client,
            context.document_id,
            language_id,
            export_type=WEB_EXPORT_TYPE_TARGET,
            segment_export_mode=WEB_SEGMENT_EXPORT_MODE_TARGET,
        )
        return source_srt, target_srt


def build_api_client_from_env() -> SmartcatApiClient:
    import os

    account_id = os.getenv("SMARTCAT_ACCOUNT_ID", "").strip()
    api_key = os.getenv("SMARTCAT_API_KEY", "").strip()
    if not account_id or not api_key:
        raise SmartcatError(
            "Smartcat API mode requires SMARTCAT_ACCOUNT_ID and SMARTCAT_API_KEY in .env"
        )
    return SmartcatApiClient(
        account_id=account_id,
        api_key=api_key,
        api_base=os.getenv("SMARTCAT_API_BASE", "https://ea.smartcat.ai").strip()
        or "https://ea.smartcat.ai",
    )


def build_web_client_from_env(*, project_root: Path | None = None) -> SmartcatWebClient:
    import os
    from pathlib import Path

    from catalog_parser.smartcat_web import DEFAULT_STORAGE_STATE

    storage_state = Path(
        os.getenv("SMARTCAT_STORAGE_STATE", DEFAULT_STORAGE_STATE)
    ).expanduser()
    if not storage_state.is_absolute() and project_root is not None:
        storage_state = project_root / storage_state
    return SmartcatWebClient(
        ui_base=os.getenv("SMARTCAT_UI_BASE", DEFAULT_UI_BASE).strip() or DEFAULT_UI_BASE,
        storage_state_path=storage_state,
        language=configured_target_language(),
    )


def build_cookie_client_from_env(*, project_root: Path | None = None) -> SmartcatCookieClient:
    from catalog_parser.smartcat_cookie import build_cookie_client_from_env as _build

    return _build(project_root=project_root)


def pkg_sm_link_from_context(
    context: SmartcatDocumentContext,
    *,
    ui_base: str = DEFAULT_UI_BASE,
) -> str:
    return build_pkg_sm_link(
        ui_base,
        context.project_id,
        search=context.search,
    )
