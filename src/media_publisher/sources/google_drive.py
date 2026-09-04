from __future__ import annotations

import calendar
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from media_publisher.transient_retry import call_with_transient_retry

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
GOOGLE_SHEETS_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
EXCEL_SHEETS_MIME_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    }
)
SPREADSHEET_MIME_TYPES = frozenset({GOOGLE_SHEETS_MIME_TYPE, *EXCEL_SHEETS_MIME_TYPES})
DOCX_MIME_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
IMAGE_MIME_PREFIX = "image/"
VIDEO_MIME_PREFIX = "video/"

UploadAction = Literal["added", "updated", "unchanged"]


class GoogleDriveError(RuntimeError):
    pass


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    md5_checksum: str | None = None
    modified_time: str | None = None


@dataclass(frozen=True)
class QuoteBackgroundImage:
    day: int
    file_id: str
    name: str
    variant: str
    md5_checksum: str | None = None
    modified_time: str | None = None


@dataclass(frozen=True)
class DriveUploadResult:
    action: UploadAction
    file: DriveFile


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


def local_file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class GoogleDriveClient:
    def __init__(self, drive_service: Any) -> None:
        self._drive = drive_service

    @property
    def drive_service(self) -> Any:
        return self._drive

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

    def _execute(self, request: Any) -> Any:
        """Run a Drive API request with retries for transient failures."""
        return call_with_transient_retry(request.execute)

    def list_children(self, folder_id: str) -> list[DriveFile]:
        response = self._execute(
            self._drive.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="files(id,name,mimeType,md5Checksum,modifiedTime)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageSize=1000,
            )
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
                md5 = item.get("md5Checksum")
                modified = item.get("modifiedTime")
                results.append(
                    DriveFile(
                        id=file_id,
                        name=name,
                        mime_type=mime_type,
                        md5_checksum=md5 if isinstance(md5, str) else None,
                        modified_time=modified if isinstance(modified, str) else None,
                    )
                )
        return results

    def find_child_by_name(self, parent_id: str, name: str) -> DriveFile | None:
        target = name.casefold().strip()
        for item in self.list_children(parent_id):
            if item.name.casefold().strip() == target:
                return item
        return None

    def list_spreadsheets(self, folder_id: str) -> list[DriveFile]:
        """List Google Sheets and Excel workbooks in a folder."""
        return [
            item
            for item in self.list_children(folder_id)
            if item.mime_type in SPREADSHEET_MIME_TYPES
        ]

    def create_google_spreadsheet(self, parent_id: str, name: str) -> DriveFile:
        """Create an empty Google Sheets file in a Drive folder."""
        existing = self.find_child_by_name(parent_id, name)
        if existing is not None and existing.mime_type == GOOGLE_SHEETS_MIME_TYPE:
            return existing
        try:
            created = self._execute(
                self._drive.files().create(
                    body={
                        "name": name,
                        "mimeType": GOOGLE_SHEETS_MIME_TYPE,
                        "parents": [parent_id],
                    },
                    fields="id,name,mimeType,md5Checksum,modifiedTime",
                    supportsAllDrives=True,
                )
            )
        except Exception as exc:
            raise GoogleDriveError(
                f"Failed to create spreadsheet {name!r} under {parent_id}: {exc}"
            ) from exc
        file_id = created.get("id")
        file_name = created.get("name")
        mime_type = created.get("mimeType")
        if not (
            isinstance(file_id, str)
            and isinstance(file_name, str)
            and isinstance(mime_type, str)
        ):
            raise GoogleDriveError(
                f"Drive spreadsheet create for {name!r} returned an invalid response"
            )
        return DriveFile(
            id=file_id,
            name=file_name,
            mime_type=mime_type,
            md5_checksum=(
                created.get("md5Checksum")
                if isinstance(created.get("md5Checksum"), str)
                else None
            ),
            modified_time=(
                created.get("modifiedTime")
                if isinstance(created.get("modifiedTime"), str)
                else None
            ),
        )

    def find_child_folder(self, parent_id: str, folder_name: str) -> DriveFile | None:
        target = folder_name.casefold()
        for item in self.list_children(parent_id):
            if item.mime_type == FOLDER_MIME_TYPE and item.name.casefold() == target:
                return item
        return None

    def ensure_folder(self, parent_id: str, folder_name: str) -> DriveFile:
        existing = self.find_child_folder(parent_id, folder_name)
        if existing is not None:
            return existing
        try:
            created = self._execute(
                self._drive.files().create(
                    body={
                        "name": folder_name,
                        "mimeType": FOLDER_MIME_TYPE,
                        "parents": [parent_id],
                    },
                    fields="id,name,mimeType",
                    supportsAllDrives=True,
                )
            )
        except Exception as exc:
            raise GoogleDriveError(
                f"Failed to create Drive folder {folder_name!r} under {parent_id}: {exc}"
            ) from exc
        file_id = created.get("id")
        name = created.get("name")
        mime_type = created.get("mimeType")
        if not isinstance(file_id, str) or not isinstance(name, str) or not isinstance(mime_type, str):
            raise GoogleDriveError(
                f"Drive folder create for {folder_name!r} returned an invalid response"
            )
        return DriveFile(id=file_id, name=name, mime_type=mime_type)

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
                    md5_checksum=item.md5_checksum,
                    modified_time=item.modified_time,
                )
            )
        return sorted(backgrounds, key=lambda image: image.day)

    def upload_or_update_file(
        self,
        parent_id: str,
        source_path: Path,
        *,
        name: str,
        mime_type: str = "image/jpeg",
    ) -> DriveUploadResult:
        if not source_path.is_file():
            raise GoogleDriveError(f"Local file not found for Drive upload: {source_path}")

        local_md5 = local_file_md5(source_path)
        existing = self.find_child_by_name(parent_id, name)
        if existing is not None:
            if existing.md5_checksum and existing.md5_checksum.casefold() == local_md5.casefold():
                return DriveUploadResult(action="unchanged", file=existing)

        from googleapiclient.http import MediaFileUpload

        media = MediaFileUpload(str(source_path), mimetype=mime_type, resumable=True)

        if existing is not None:
            try:
                updated = self._execute(
                    self._drive.files().update(
                        fileId=existing.id,
                        media_body=media,
                        fields="id,name,mimeType,md5Checksum,modifiedTime",
                        supportsAllDrives=True,
                    )
                )
            except Exception as exc:
                raise GoogleDriveError(
                    f"Failed to update Drive file {name!r} ({existing.id}): {exc}"
                ) from exc
            return DriveUploadResult(
                action="updated",
                file=DriveFile(
                    id=str(updated.get("id") or existing.id),
                    name=str(updated.get("name") or name),
                    mime_type=str(updated.get("mimeType") or mime_type),
                    md5_checksum=(
                        updated.get("md5Checksum")
                        if isinstance(updated.get("md5Checksum"), str)
                        else local_md5
                    ),
                    modified_time=(
                        updated.get("modifiedTime")
                        if isinstance(updated.get("modifiedTime"), str)
                        else None
                    ),
                ),
            )

        try:
            created = self._execute(
                self._drive.files().create(
                    body={"name": name, "parents": [parent_id]},
                    media_body=media,
                    fields="id,name,mimeType,md5Checksum,modifiedTime",
                    supportsAllDrives=True,
                )
            )
        except Exception as exc:
            raise GoogleDriveError(
                f"Failed to upload Drive file {name!r} under {parent_id}: {exc}"
            ) from exc
        file_id = created.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise GoogleDriveError(f"Drive upload of {name!r} did not return a file id")
        return DriveUploadResult(
            action="added",
            file=DriveFile(
                id=file_id,
                name=str(created.get("name") or name),
                mime_type=str(created.get("mimeType") or mime_type),
                md5_checksum=(
                    created.get("md5Checksum")
                    if isinstance(created.get("md5Checksum"), str)
                    else local_md5
                ),
                modified_time=(
                    created.get("modifiedTime")
                    if isinstance(created.get("modifiedTime"), str)
                    else None
                ),
            ),
        )

    def get_file(self, file_id: str) -> DriveFile:
        try:
            metadata = self._execute(
                self._drive.files().get(
                    fileId=file_id,
                    fields="id,name,mimeType,md5Checksum,modifiedTime",
                    supportsAllDrives=True,
                )
            )
        except Exception as exc:
            raise GoogleDriveError(
                f"Failed to read Drive file {file_id}: {exc}"
            ) from exc
        if not isinstance(metadata, dict):
            raise GoogleDriveError(f"Drive file {file_id} returned an invalid response")
        file_id_value = metadata.get("id")
        name = metadata.get("name")
        mime_type = metadata.get("mimeType")
        if not (
            isinstance(file_id_value, str)
            and isinstance(name, str)
            and isinstance(mime_type, str)
        ):
            raise GoogleDriveError(f"Drive file {file_id} is missing required metadata")
        md5 = metadata.get("md5Checksum")
        modified = metadata.get("modifiedTime")
        return DriveFile(
            id=file_id_value,
            name=name,
            mime_type=mime_type,
            md5_checksum=md5 if isinstance(md5, str) else None,
            modified_time=modified if isinstance(modified, str) else None,
        )

    def download_file(self, file_id: str, destination: Path) -> Path:
        from googleapiclient.http import MediaIoBaseDownload
        import io

        destination.parent.mkdir(parents=True, exist_ok=True)
        request = self._drive.files().get_media(fileId=file_id, supportsAllDrives=True)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = call_with_transient_retry(downloader.next_chunk)
        destination.write_bytes(buffer.getvalue())
        return destination

    def download_document(self, file_id: str, destination: Path) -> Path:
        """Download a Drive file, exporting Google Docs to .docx."""
        from googleapiclient.http import MediaIoBaseDownload
        import io

        meta = self.get_file(file_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if meta.mime_type == GOOGLE_DOC_MIME_TYPE:
            request = self._drive.files().export_media(
                fileId=file_id,
                mimeType=DOCX_MIME_TYPE,
            )
        else:
            request = self._drive.files().get_media(
                fileId=file_id,
                supportsAllDrives=True,
            )
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = call_with_transient_retry(downloader.next_chunk)
        destination.write_bytes(buffer.getvalue())
        return destination

    def _file_capabilities(self, file_id: str) -> dict[str, bool]:
        try:
            metadata = self._execute(
                self._drive.files().get(
                    fileId=file_id,
                    fields="capabilities/canDelete,capabilities/canTrash",
                    supportsAllDrives=True,
                )
            )
        except Exception as exc:
            raise GoogleDriveError(
                f"Failed to read Drive file capabilities for {file_id}: {exc}"
            ) from exc
        capabilities = metadata.get("capabilities", {})
        if not isinstance(capabilities, dict):
            return {}
        result: dict[str, bool] = {}
        for name in ("canDelete", "canTrash"):
            value = capabilities.get(name)
            if isinstance(value, bool):
                result[name] = value
        return result

    def delete_file(self, file_id: str) -> None:
        try:
            self._execute(
                self._drive.files().delete(
                    fileId=file_id,
                    supportsAllDrives=True,
                )
            )
        except Exception as exc:
            raise GoogleDriveError(
                f"Failed to delete Drive file {file_id}: {exc}"
            ) from exc

    def trash_file(self, file_id: str) -> None:
        try:
            self._execute(
                self._drive.files().update(
                    fileId=file_id,
                    body={"trashed": True},
                    supportsAllDrives=True,
                )
            )
        except Exception as exc:
            raise GoogleDriveError(
                f"Failed to trash Drive file {file_id}: {exc}"
            ) from exc

    def remove_file(self, file_id: str) -> str:
        """Delete a Drive file, or trash it when delete is not permitted."""
        capabilities = self._file_capabilities(file_id)
        if capabilities.get("canDelete"):
            self.delete_file(file_id)
            return "deleted"
        if capabilities.get("canTrash"):
            self.trash_file(file_id)
            return "trashed"
        raise GoogleDriveError(
            f"Drive file {file_id} cannot be deleted or trashed with current permissions"
        )

    def move_file(self, file_id: str, destination_folder_id: str) -> None:
        try:
            metadata = self._execute(
                self._drive.files().get(
                    fileId=file_id,
                    fields="parents",
                    supportsAllDrives=True,
                )
            )
            parents = metadata.get("parents", [])
            previous_parents = ",".join(parents) if isinstance(parents, list) else None
            self._execute(
                self._drive.files().update(
                    fileId=file_id,
                    addParents=destination_folder_id,
                    removeParents=previous_parents,
                    supportsAllDrives=True,
                    fields="id, parents",
                )
            )
        except Exception as exc:
            raise GoogleDriveError(
                f"Failed to move Drive file {file_id} to folder {destination_folder_id}: {exc}"
            ) from exc

    def find_file_by_title(
        self,
        folder_id: str,
        title: str,
        *,
        mime_prefix: str | None = None,
        extensions: tuple[str, ...] | None = None,
    ) -> DriveFile | None:
        target = title.casefold().strip()
        if not target:
            return None
        for item in self.list_children(folder_id):
            if item.mime_type == FOLDER_MIME_TYPE:
                continue
            if Path(item.name).stem.casefold().strip() != target:
                continue
            if mime_prefix and not item.mime_type.startswith(mime_prefix):
                continue
            if extensions and Path(item.name).suffix.casefold() not in extensions:
                continue
            return item
        return None
