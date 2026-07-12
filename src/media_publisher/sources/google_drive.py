from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
IMAGE_MIME_PREFIX = "image/"


class GoogleDriveError(RuntimeError):
    pass


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str


@dataclass(frozen=True)
class QuoteBackgroundImage:
    day: int
    file_id: str
    name: str
    variant: str


DAY_FILENAME_RE = re.compile(
    r"^(?P<month>[A-Za-z]{3})-(?P<day>\d{1,2})-",
    re.IGNORECASE,
)


def english_month_abbr(month: int) -> str:
    if month < 1 or month > 12:
        raise GoogleDriveError(f"Invalid month number: {month}")
    return calendar.month_abbr[month]


def format_year_folder_name(pattern: str, *, year: int) -> str:
    return pattern.format(year=year)


def format_month_folder_name(pattern: str, *, year: int, month: int) -> str:
    return pattern.format(
        year=year,
        month=month,
        month_abbr=english_month_abbr(month),
        month_name_en=calendar.month_name[month],
    )


def parse_day_from_background_filename(name: str) -> int | None:
    match = DAY_FILENAME_RE.match(name.strip())
    if match is None:
        return None
    return int(match.group("day"))


class GoogleDriveClient:
    def __init__(self, drive_service: Any) -> None:
        self._drive = drive_service

    @classmethod
    def from_service_account(cls, path: Path) -> GoogleDriveClient:
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise GoogleDriveError(
                "Google Drive access requires google-auth and google-api-python-client. "
                'Install with: pip install -e ".[sheets]"'
            ) from exc

        credentials = service_account.Credentials.from_service_account_file(
            str(path),
            scopes=[DRIVE_SCOPE],
        )
        service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        return cls(service)

    def list_children(self, folder_id: str) -> list[DriveFile]:
        response = (
            self._drive.files()
            .list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="files(id,name,mimeType)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageSize=1000,
            )
            .execute()
        )
        files = response.get("files", [])
        if not isinstance(files, list):
            return []
        results: list[DriveFile] = []
        for item in files:
            if not isinstance(item, dict):
                continue
            file_id = item.get("id")
            name = item.get("name")
            mime_type = item.get("mimeType")
            if isinstance(file_id, str) and isinstance(name, str) and isinstance(mime_type, str):
                results.append(DriveFile(id=file_id, name=name, mime_type=mime_type))
        return results

    def find_child_folder(self, parent_id: str, folder_name: str) -> DriveFile | None:
        target = folder_name.casefold()
        for item in self.list_children(parent_id):
            if item.mime_type == FOLDER_MIME_TYPE and item.name.casefold() == target:
                return item
        return None

    def resolve_month_background_folder(
        self,
        *,
        root_folder_id: str,
        year: int,
        month: int,
        year_folder_pattern: str,
        month_folder_pattern: str,
    ) -> DriveFile:
        year_folder_name = format_year_folder_name(year_folder_pattern, year=year)
        year_folder = self.find_child_folder(root_folder_id, year_folder_name)
        if year_folder is None:
            raise GoogleDriveError(
                f"Year folder {year_folder_name!r} not found under Drive root {root_folder_id!r}"
            )

        month_folder_name = format_month_folder_name(
            month_folder_pattern,
            year=year,
            month=month,
        )
        month_folder = self.find_child_folder(year_folder.id, month_folder_name)
        if month_folder is None:
            raise GoogleDriveError(
                f"Month folder {month_folder_name!r} not found under {year_folder_name!r}"
            )
        return month_folder

    def list_quote_backgrounds(
        self,
        *,
        month_folder_id: str,
        variant: str,
        subdir: str | None = None,
        month: int | None = None,
    ) -> list[QuoteBackgroundImage]:
        folder_id = month_folder_id
        if subdir:
            subfolder = self.find_child_folder(month_folder_id, subdir)
            if subfolder is None:
                raise GoogleDriveError(
                    f"Background subfolder {subdir!r} not found for variant {variant!r}"
                )
            folder_id = subfolder.id

        backgrounds: list[QuoteBackgroundImage] = []
        for item in self.list_children(folder_id):
            if not item.mime_type.startswith(IMAGE_MIME_PREFIX):
                continue
            day = parse_day_from_background_filename(item.name)
            if day is None:
                continue
            if month is not None:
                expected_prefix = f"{english_month_abbr(month)}-{day}-".casefold()
                if not item.name.casefold().startswith(expected_prefix):
                    continue
            backgrounds.append(
                QuoteBackgroundImage(
                    day=day,
                    file_id=item.id,
                    name=item.name,
                    variant=variant,
                )
            )
        return sorted(backgrounds, key=lambda image: image.day)

    def download_file(self, file_id: str, destination: Path) -> Path:
        from googleapiclient.http import MediaIoBaseDownload
        import io

        destination.parent.mkdir(parents=True, exist_ok=True)
        request = self._drive.files().get_media(fileId=file_id, supportsAllDrives=True)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        destination.write_bytes(buffer.getvalue())
        return destination
