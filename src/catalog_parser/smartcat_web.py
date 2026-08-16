from __future__ import annotations

import json
import re
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from catalog_parser.smartcat import (
    DEFAULT_TARGET_LANGUAGE,
    DEFAULT_UI_BASE,
    SmartcatError,
    build_smartcat_editor_link,
    bulgarian_segments_have_translation,
    bulgarian_target_is_fully_done,
    configured_language_aliases,
    configured_target_language_name,
    get_language_target,
    language_matches,
    parse_pkg_sm_link,
    resolve_language_id,
)
from catalog_parser.smartcat_cookie import find_document_via_web_api

DEFAULT_STORAGE_STATE = "smartcat-state.json"
PAGE_GOTO_TIMEOUT_MS = 90_000
LOGIN_URL_PATTERN = re.compile(r"login|sign[\s-]?in|auth", re.IGNORECASE)

SRT_TEXT_PATTERN = re.compile(r"\.srt\b", re.IGNORECASE)


@dataclass(frozen=True)
class AnchorCandidate:
    href: str
    text: str


def _looks_like_login_url(url: str) -> bool:
    return bool(LOGIN_URL_PATTERN.search(url))


def pick_bulgarian_srt_href(
    candidates: list[AnchorCandidate],
    *,
    search: str | None,
    title: str | None,
    language: str,
) -> str | None:
    search_norm = (search or "").lower()
    title_norm = (title or "").lower()

    best_href: str | None = None
    best_score = -1

    for candidate in candidates:
        haystack = f"{candidate.text} {candidate.href}".lower()
        if not SRT_TEXT_PATTERN.search(haystack):
            continue
        if not language_matches(haystack, language):
            continue

        score = 0
        if search_norm and search_norm in haystack:
            score += 40
        if title_norm and title_norm in haystack:
            score += 30
        if any(alias in haystack for alias in configured_language_aliases()):
            score += 20
        if score > best_score:
            best_score = score
            best_href = candidate.href

    return best_href


class SmartcatWebClient:
    """Resolve Bulgarian subtitle editor links through Smartcat's authenticated web APIs."""

    def __init__(
        self,
        *,
        ui_base: str = DEFAULT_UI_BASE,
        storage_state_path: Path,
        headless: bool = True,
        language: str = DEFAULT_TARGET_LANGUAGE,
    ) -> None:
        self.ui_base = ui_base.rstrip("/")
        self.storage_state_path = storage_state_path
        self.headless = headless
        self.language = language
        self._api_request: Any | None = None

    @contextmanager
    def _playwright_page(self) -> Iterator[Any]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SmartcatError(
                "Playwright is required for web-based Smartcat enrichment. "
                "Install it with: pip install playwright && playwright install chromium"
            ) from exc

        if not self.storage_state_path.exists():
            raise SmartcatError(
                f"Smartcat browser session not found at {self.storage_state_path}. "
                "Run: catalog-parser --smartcat-login"
            )

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self.headless)
            context = browser.new_context(storage_state=str(self.storage_state_path))
            self._api_request = context.request
            page = context.new_page()
            page.set_default_timeout(PAGE_GOTO_TIMEOUT_MS)
            try:
                yield page
            finally:
                self._api_request = None
                context.close()
                browser.close()

    def _require_api_request(self) -> Any:
        if self._api_request is None:
            raise SmartcatError("Smartcat web session is not open")
        return self._api_request

    def web_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> tuple[int, bytes]:
        request = self._require_api_request()
        url = f"{self.ui_base}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"

        kwargs: dict[str, Any] = {"method": method}
        if json_body is not None:
            kwargs["data"] = json.dumps(json_body)
            kwargs["headers"] = {"Content-Type": "application/json"}

        response = request.fetch(url, **kwargs)
        return response.status, response.body() or b""

    def _api_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any | None = None,
    ) -> Any:
        request = self._require_api_request()
        url = f"{self.ui_base}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"

        kwargs: dict[str, Any] = {}
        if body is not None:
            kwargs["data"] = json.dumps(body)
            kwargs["headers"] = {"Content-Type": "application/json"}

        response = request.fetch(url, method=method, **kwargs)
        if response.status >= 400:
            detail = response.text()[:500]
            raise SmartcatError(
                f"Smartcat API {method} {path} failed with HTTP {response.status}: {detail}"
            )
        if not response.body():
            return None
        return response.json()

    def _find_document(
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

    def resolve_bulgarian_srt_link(
        self,
        pkg_sm_link: str,
        *,
        title: str | None = None,
        language: str = DEFAULT_TARGET_LANGUAGE,
    ) -> str | None:
        parsed = parse_pkg_sm_link(pkg_sm_link)
        if parsed is None:
            raise SmartcatError(f"Could not parse Smartcat link: {pkg_sm_link!r}")

        with self._playwright_page() as page:
            return self._resolve_with_session(
                page,
                pkg_sm_link,
                parsed.project_id,
                search=parsed.search,
                title=title,
                language=language,
            )

    def _resolve_with_session(
        self,
        page: Any,
        pkg_sm_link: str,
        project_id: str,
        *,
        search: str | None,
        title: str | None,
        language: str,
    ) -> str | None:
        if _looks_like_login_url(page.url):
            raise SmartcatError(
                "Smartcat session expired. Run: catalog-parser --smartcat-login"
            )

        language_id = resolve_language_id(language)
        document = self._find_document(project_id, search=search, title=title)
        target = get_language_target(document, language_id)
        if target is None:
            raise SmartcatError(
                f"Document {document.get('name')!r} has no Smartcat target for language {language!r}"
            )

        # Skip when Bulgarian targets already have any translation text. Smartcat
        # stage progress often stays at 0% even then, so inspect segment text.
        if bulgarian_target_is_fully_done(target):
            return None

        document_id = document.get("id")
        if not isinstance(document_id, str) or not document_id:
            raise SmartcatError(f"Matched Smartcat document is missing an id: {document!r}")

        # Lazy import: smartcat_write → smartcat_export → smartcat_web.
        from catalog_parser.smartcat_write import list_document_segments

        segments = list_document_segments(self, document_id, language_id)
        if bulgarian_segments_have_translation(segments, language_id):
            return None

        return build_smartcat_editor_link(
            self.ui_base,
            document_id,
            language_id=language_id,
            pkg_sm_link=pkg_sm_link,
        )


def login_interactive(
    *,
    ui_base: str = DEFAULT_UI_BASE,
    storage_state_path: Path,
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SmartcatError(
            "Playwright is required for Smartcat login. "
            "Install it with: pip install playwright && playwright install chromium"
        ) from exc

    ui_base = ui_base.rstrip("/")
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(ui_base, wait_until="commit", timeout=PAGE_GOTO_TIMEOUT_MS)

        print("A browser window has opened.")
        print("1. Log in to Smartcat with your account.")
        print("2. Confirm you can open a project files page.")
        input("Press Enter here after you are logged in... ")

        context.storage_state(path=str(storage_state_path))
        context.close()
        browser.close()

    print(f"Saved Smartcat session to {storage_state_path}")


class SmartcatWebSession:
    """Reuse one browser session while enriching multiple catalog rows."""

    def __init__(self, client: SmartcatWebClient) -> None:
        self._client = client
        self._page: Any | None = None
        self._context_manager: Any | None = None

    def __enter__(self) -> SmartcatWebSession:
        self._context_manager = self._client._playwright_page()
        self._page = self._context_manager.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._context_manager is not None:
            self._context_manager.__exit__(exc_type, exc, tb)
        self._page = None
        self._context_manager = None

    def resolve_bulgarian_srt_link(
        self,
        pkg_sm_link: str,
        *,
        title: str | None = None,
        language: str = DEFAULT_TARGET_LANGUAGE,
    ) -> str | None:
        if self._page is None:
            raise SmartcatError("Smartcat web session is not open")

        parsed = parse_pkg_sm_link(pkg_sm_link)
        if parsed is None:
            raise SmartcatError(f"Could not parse Smartcat link: {pkg_sm_link!r}")

        return self._client._resolve_with_session(
            self._page,
            pkg_sm_link,
            parsed.project_id,
            search=parsed.search,
            title=title,
            language=language,
        )


def enrich_records_with_bulgarian_srt_links_web(
    records: list[dict[str, Any]],
    client: SmartcatWebClient,
    *,
    language: str = DEFAULT_TARGET_LANGUAGE,
    link_field: str = "pkgBgSrtLk",
    source_link_field: str = "pkgSmLk",
    title_field: str = "ctTitle",
) -> list[dict[str, Any]]:
    from catalog_parser.smartcat import enrich_records_with_bulgarian_srt_links

    with SmartcatWebSession(client) as session:
        enriched: list[dict[str, Any]] = []
        total = len(records)
        for index, record in enumerate(records, start=1):
            title = record.get(title_field)
            title_label = title if isinstance(title, str) and title else f"row {index}"
            print(f"Smartcat {index}/{total}: {title_label}")
            batch = enrich_records_with_bulgarian_srt_links(
                [record],
                session,
                language=language,
                link_field=link_field,
                source_link_field=source_link_field,
                title_field=title_field,
            )
            enriched_record = batch[0]
            if enriched_record.get(link_field):
                print("  -> editor link resolved")
            elif enriched_record.get(f"{link_field}SkipReason"):
                print(
                    f"  -> skipped ({configured_target_language_name()} "
                    "subtitles already completed)"
                )
            elif enriched_record.get(f"{link_field}Error"):
                print(f"  -> error: {enriched_record[f'{link_field}Error']}")
            enriched.append(enriched_record)
        return enriched
