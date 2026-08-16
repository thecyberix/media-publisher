from __future__ import annotations

import calendar
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleSheetsError(RuntimeError):
    pass


def _format_sheets_http_error(status_code: int, detail: str) -> str:
    message = f"Google Sheets request failed with HTTP {status_code}: {detail}"
    if "protected cell" in detail.casefold():
        message += (
            "\n\nThis spreadsheet uses sheet-wide protection with specific unprotected "
            "holes. The Views Actual rows may fall outside those holes even when the "
            "service account is listed on another protection rule. Run:\n"
            "  python -m media_publisher --fix-channel-report-protection\n"
            "or ask the spreadsheet owner to add unprotected ranges for the KPI "
            "dashboard metric rows (YouTube 43-55, Instagram 76-88, Facebook 142-154; "
            "columns G through AF) on the Bulgarian tab."
        )
    return message


def _is_protected_cell_error(exc: GoogleSheetsError) -> bool:
    return "protected cell" in str(exc).casefold()


@dataclass(frozen=True)
class SheetTab:
    sheet_id: int
    title: str


def format_sheet_tab_title(year: int, month: int) -> str:
    """Format a quotes spreadsheet tab title such as 'Jul 2026'."""
    if month < 1 or month > 12:
        raise GoogleSheetsError(f"Invalid month number: {month}")
    return f"{calendar.month_abbr[month]} {year}"


def list_sheet_tabs(payload: dict[str, Any]) -> list[SheetTab]:
    sheets = payload.get("sheets", [])
    if not isinstance(sheets, list):
        raise GoogleSheetsError("Spreadsheet response is missing sheets")

    tabs: list[SheetTab] = []
    for sheet in sheets:
        if not isinstance(sheet, dict):
            continue
        properties = sheet.get("properties")
        if not isinstance(properties, dict):
            continue
        sheet_id = properties.get("sheetId")
        title = properties.get("title")
        if isinstance(sheet_id, int) and isinstance(title, str):
            tabs.append(SheetTab(sheet_id=sheet_id, title=title))
    return tabs


def _load_service_account(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise GoogleSheetsError(f"Google service account file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise GoogleSheetsError("Google service account file is invalid")
    return payload


def _service_account_access_token(credentials: dict[str, Any]) -> str:
    try:
        from google.oauth2 import service_account
    except ImportError as exc:
        raise GoogleSheetsError(
            "google-auth is required for Google Sheets access. "
            'Install with: pip install -e ".[sheets]"'
        ) from exc

    client_email = credentials.get("client_email")
    if not isinstance(client_email, str) or not client_email:
        if "installed" in credentials or "web" in credentials:
            raise GoogleSheetsError(
                "The credentials file is an OAuth client (Desktop/Web app), not a "
                "service account key. Download a service account JSON key from "
                "Google Cloud Console (IAM & Admin → Service Accounts → Keys) and "
                "save it as credentials/google-sheets-service-account.json."
            )
        raise GoogleSheetsError(
            "Service account file is missing client_email. Expected JSON with "
            '"type": "service_account".'
        )

    creds = service_account.Credentials.from_service_account_info(
        credentials,
        scopes=[SHEETS_SCOPE],
    )
    try:
        from google.auth.transport.requests import Request

        creds.refresh(Request())
    except Exception as exc:
        raise GoogleSheetsError(
            f"Failed to refresh service account token: {exc}"
        ) from exc
    if not creds.token:
        raise GoogleSheetsError("Failed to obtain Google Sheets access token")
    return creds.token


class GoogleSheetsClient:
    def __init__(self, *, access_token: str) -> None:
        self.access_token = access_token

    @classmethod
    def from_service_account(cls, path: Path) -> GoogleSheetsClient:
        token = _service_account_access_token(_load_service_account(path))
        return cls(access_token=token)

    def _request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.access_token}")
        if headers:
            for key, value in headers.items():
                request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise GoogleSheetsError(
                _format_sheets_http_error(exc.code, detail)
            ) from exc
        except urllib.error.URLError as exc:
            raise GoogleSheetsError(f"Google Sheets request failed: {exc.reason}") from exc

        if not body:
            return {}
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise GoogleSheetsError("Google Sheets response is not a JSON object")
        return payload

    def get_spreadsheet(self, spreadsheet_id: str) -> dict[str, Any]:
        return self._request("GET", f"{SHEETS_API_BASE}/{spreadsheet_id}")

    def list_tabs(self, spreadsheet_id: str) -> list[SheetTab]:
        return list_sheet_tabs(self.get_spreadsheet(spreadsheet_id))

    def resolve_sheet_tab(
        self,
        spreadsheet_id: str,
        *,
        sheet_gid: int | None = None,
        sheet_title: str | None = None,
    ) -> SheetTab:
        tabs = self.list_tabs(spreadsheet_id)
        if sheet_title and sheet_title.strip():
            target = sheet_title.strip().casefold()
            for tab in tabs:
                if tab.title.casefold() == target:
                    return tab
            names = ", ".join(tab.title for tab in tabs)
            raise GoogleSheetsError(
                f"Sheet tab {sheet_title.strip()!r} was not found in spreadsheet "
                f"{spreadsheet_id!r}. Available tabs: {names}"
            )
        if sheet_gid is not None:
            for tab in tabs:
                if tab.sheet_id == sheet_gid:
                    return tab
            raise GoogleSheetsError(
                f"Sheet gid {sheet_gid} was not found in spreadsheet {spreadsheet_id!r}"
            )
        if len(tabs) == 1:
            return tabs[0]
        names = ", ".join(tab.title for tab in tabs)
        raise GoogleSheetsError(
            f"Sheet title is required; available tabs: {names}"
        )

    def resolve_sheet_tab_for_month(
        self,
        spreadsheet_id: str,
        *,
        year: int,
        month: int,
    ) -> SheetTab:
        expected_title = format_sheet_tab_title(year, month)
        tabs = self.list_tabs(spreadsheet_id)
        for tab in tabs:
            if tab.title == expected_title:
                return tab
        available = ", ".join(tab.title for tab in tabs)
        raise GoogleSheetsError(
            f"Quotes sheet tab {expected_title!r} was not found in spreadsheet "
            f"{spreadsheet_id!r}. Available tabs: {available}"
        )

    def resolve_sheet_title(
        self,
        spreadsheet_id: str,
        *,
        sheet_gid: int | None = None,
        sheet_title: str | None = None,
    ) -> str:
        return self.resolve_sheet_tab(
            spreadsheet_id,
            sheet_gid=sheet_gid,
            sheet_title=sheet_title,
        ).title

    def get_values(self, spreadsheet_id: str, range_a1: str) -> list[list[str]]:
        encoded_range = urllib.parse.quote(range_a1, safe="")
        url = f"{SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded_range}"
        payload = self._request("GET", url)
        values = payload.get("values", [])
        if not isinstance(values, list):
            return []
        rows: list[list[str]] = []
        for row in values:
            if isinstance(row, list):
                rows.append([str(cell) for cell in row])
        return rows

    def batch_update_values(
        self,
        spreadsheet_id: str,
        updates: list[tuple[str, list[list[Any]]]],
        *,
        value_input_option: str = "USER_ENTERED",
    ) -> None:
        if not updates:
            return
        body = {
            "valueInputOption": value_input_option,
            "data": [
                {"range": range_a1, "values": values}
                for range_a1, values in updates
            ],
        }
        query = urllib.parse.urlencode({"valueInputOption": value_input_option})
        url = f"{SHEETS_API_BASE}/{spreadsheet_id}/values:batchUpdate?{query}"
        self._request(
            "POST",
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    def batch_update_values_resilient(
        self,
        spreadsheet_id: str,
        updates: list[tuple[str, list[list[Any]]]],
        *,
        value_input_option: str = "USER_ENTERED",
    ) -> list[str]:
        """Write values, falling back to per-cell updates when batch hits protection."""
        if not updates:
            return []
        try:
            self.batch_update_values(
                spreadsheet_id,
                updates,
                value_input_option=value_input_option,
            )
            return []
        except GoogleSheetsError as exc:
            if not _is_protected_cell_error(exc):
                raise

        failed_ranges: list[str] = []
        for range_a1, values in updates:
            try:
                self.batch_update_values(
                    spreadsheet_id,
                    [(range_a1, values)],
                    value_input_option=value_input_option,
                )
            except GoogleSheetsError:
                failed_ranges.append(range_a1)
        return failed_ranges

    def batch_update_spreadsheet(
        self,
        spreadsheet_id: str,
        requests: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not requests:
            return {}
        url = f"{SHEETS_API_BASE}/{spreadsheet_id}:batchUpdate"
        body = {"requests": requests}
        payload = self._request(
            "POST",
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        if not isinstance(payload, dict):
            raise GoogleSheetsError("Google Sheets batchUpdate response is invalid")
        return payload


def column_index_to_a1(column_index: int) -> str:
    if column_index < 0:
        raise ValueError("column_index must be non-negative")
    label = ""
    value = column_index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        label = chr(ord("A") + remainder) + label
    return label


def a1_cell(sheet_title: str, row: int, column_index: int) -> str:
    column = column_index_to_a1(column_index)
    escaped = sheet_title.replace("'", "''")
    return f"'{escaped}'!{column}{row}"


def sleep_before_quota_retry(attempt: int) -> None:
    time.sleep(min(2**attempt, 30))
