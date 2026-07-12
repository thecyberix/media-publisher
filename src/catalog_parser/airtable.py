from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from catalog_parser.parser import duration_to_type, parse_duration

DEFAULT_API_BASE = "https://api.airtable.com/v0"
DEFAULT_CONTENT_API_BASE = "https://content.airtable.com/v0"
MAX_CREATE_BATCH_SIZE = 10

FIELD_ORIGINAL_VIDEO = "Original Video"
FIELD_DURATION = "Duration"
FIELD_TITLE = "Title"
FIELD_ORIGINAL_VIDEO_NAME = "Original Video Name"
FIELD_ORIGINAL_VIDEO_DESCRIPTION = "Original Video Description"
FIELD_ORIGINAL_VIDEO_THUMBNAIL = "Original Video Thumbnail"
FIELD_TYPE = "Type"
FIELD_VIDEO_FOLDER = "Video Folder"
FIELD_TRANSLATION_RESOURCES = "Translation resources"
FIELD_COMBINED_MEDIA_FILE = "Combined Media File"
FIELD_STATUS = "Status"
FIELD_TRANSLATOR = "Translator"
FIELD_EDITOR = "Editor"
FIELD_VIDEO_DESCRIPTION_TRANSLATED = "Video description translated"
FIELD_VIDEO_NAME_TRANSLATED = "Video name translated"
FIELD_VIDEO_CAPTION_TRANSLATED = "Video caption translated"

STATUS_TODO = "1. To do"
STATUS_TRANSLATION_DONE = "2. Translation done"
STATUS_EDITING_DONE = "3. Editing done"
STATUS_SYNC_DONE = "5. Synchronization done"

WORKFLOW_STATUSES = (
    STATUS_TODO,
    STATUS_TRANSLATION_DONE,
    STATUS_EDITING_DONE,
    STATUS_SYNC_DONE,
)

CATALOG_TO_AIRTABLE = {
    "ctLink": FIELD_ORIGINAL_VIDEO,
    "ctDuration": FIELD_DURATION,
    "ctTitle": FIELD_TITLE,
    "pkgLink": FIELD_VIDEO_FOLDER,
    "pkgBgSrtLk": FIELD_TRANSLATION_RESOURCES,
}

YT_TITLE_COMMENT_PREFIX = "Заглавие:"
YT_DESCRIPTION_COMMENT_PREFIX = "Описание:"
ORIGINAL_VIDEO_NAME_SUFFIX = " | Sadhguru"


class AirtableError(Exception):
    pass


def normalize_original_video_name(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith(ORIGINAL_VIDEO_NAME_SUFFIX):
        normalized = normalized[: -len(ORIGINAL_VIDEO_NAME_SUFFIX)].rstrip()
    return normalized or None


def resolve_original_video_name(
    *,
    yt_title: Any = None,
    title: Any = None,
) -> str | None:
    original = normalize_original_video_name(yt_title)
    if original:
        return original
    return normalize_original_video_name(title)


def normalize_title(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    normalized = value.strip()
    return normalized.casefold() if normalized else None


def build_yt_title_comment(yt_title: Any) -> str | None:
    if yt_title is None:
        return None
    if not isinstance(yt_title, str):
        yt_title = str(yt_title)
    yt_title = yt_title.strip()
    if not yt_title:
        return None
    return f"{YT_TITLE_COMMENT_PREFIX}\n{yt_title}"


def build_yt_description_comment(record: dict[str, Any]) -> str | None:
    yt_description = record.get("ytDescription")
    if yt_description is None:
        return None
    if not isinstance(yt_description, str):
        yt_description = str(yt_description)
    yt_description = yt_description.strip()
    if not yt_description:
        return None
    return f"{YT_DESCRIPTION_COMMENT_PREFIX}\n{yt_description}"


def catalog_record_comments(record: dict[str, Any]) -> list[str]:
    comments: list[str] = []
    title_comment = build_yt_title_comment(record.get("ytTitle"))
    if not title_comment:
        title_comment = build_yt_title_comment(record.get("ctTitle"))
    if title_comment:
        comments.append(title_comment)
    description_comment = build_yt_description_comment(record)
    if description_comment:
        comments.append(description_comment)
    return comments


def catalog_record_to_airtable_fields(record: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    duration: int | None = None
    for source_field, airtable_field in CATALOG_TO_AIRTABLE.items():
        value = record.get(source_field)
        if value is None:
            continue
        if source_field == "ctDuration":
            duration = parse_duration(value)
            if duration is None:
                continue
            value = duration
        fields[airtable_field] = value
    if duration is not None:
        fields[FIELD_TYPE] = duration_to_type(duration)
    original_video_name = resolve_original_video_name(
        yt_title=record.get("ytTitle"),
        title=record.get("ctTitle"),
    )
    if original_video_name:
        fields[FIELD_ORIGINAL_VIDEO_NAME] = original_video_name
    yt_description = record.get("ytDescription")
    if isinstance(yt_description, str) and yt_description.strip():
        fields[FIELD_ORIGINAL_VIDEO_DESCRIPTION] = yt_description.strip()
    yt_thumbnail = record.get("ytThumbnail")
    if isinstance(yt_thumbnail, list) and yt_thumbnail and not record.get("_originalThumbnailPath"):
        fields[FIELD_ORIGINAL_VIDEO_THUMBNAIL] = yt_thumbnail
    return fields


class AirtableClient:
    def __init__(
        self,
        token: str,
        base_id: str,
        table_name: str,
        api_base: str = DEFAULT_API_BASE,
    ) -> None:
        self.token = token.strip()
        self.base_id = base_id.strip()
        self.table_name = table_name.strip()
        self.api_base = api_base.rstrip("/")
        if not self.token:
            raise AirtableError("AIRTABLE_TOKEN is required")
        if not self.base_id:
            raise AirtableError("AIRTABLE_BASE_ID is required")
        if not self.table_name:
            raise AirtableError("AIRTABLE_TABLE_NAME is required")

    def _table_url(self) -> str:
        encoded_table = urllib.parse.quote(self.table_name, safe="")
        return f"{self.api_base}/{self.base_id}/{encoded_table}"

    def _record_comments_url(self, record_id: str) -> str:
        encoded_record_id = urllib.parse.quote(record_id, safe="")
        return f"{self._table_url()}/{encoded_record_id}/comments"

    def _request(
        self,
        method: str,
        url: str,
        *,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        if query:
            url = f"{url}?{urllib.parse.urlencode(query, doseq=True)}"

        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise AirtableError(
                f"Airtable {method} {url} failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise AirtableError(f"Airtable request failed: {exc.reason}") from exc

        if not payload:
            return {}
        return json.loads(payload.decode("utf-8"))

    def list_existing_titles(self) -> set[str]:
        titles: set[str] = set[str]()
        offset: str | None = None

        while True:
            query: dict[str, str] = {"fields[]": FIELD_TITLE}
            if offset:
                query["offset"] = offset

            response = self._request("GET", self._table_url(), query=query)
            for record in response.get("records", []):
                title = normalize_title(record.get("fields", {}).get(FIELD_TITLE))
                if title:
                    titles.add(title)

            offset = response.get("offset")
            if not offset:
                break

        return titles

    def find_record_id_by_exact_field(
        self,
        *,
        field_name: str,
        value: str,
    ) -> str | None:
        field_name = str(field_name).strip()
        value = str(value).strip()
        if not field_name or not value:
            return None

        # Airtable formula: {Field}="Value"
        # Escape quotes inside the value.
        escaped = value.replace('"', '\\"')
        formula = f'{{{field_name}}}="{escaped}"'
        response = self._request(
            "GET",
            self._table_url(),
            query={
                "maxRecords": "2",
                "filterByFormula": formula,
                "fields[]": field_name,
            },
        )
        records = response.get("records", [])
        if not isinstance(records, list) or not records:
            return None
        if len(records) > 1:
            raise AirtableError(
                f"Multiple Airtable records match {field_name!r}={value!r}; "
                "use --record-id instead."
            )
        record_id = records[0].get("id")
        return record_id if isinstance(record_id, str) and record_id else None

    def list_records(
        self,
        *,
        filter_formula: str | None = None,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        offset: str | None = None
        while True:
            query: dict[str, str] = {}
            if filter_formula:
                query["filterByFormula"] = filter_formula
            if offset:
                query["offset"] = offset
            response = self._request("GET", self._table_url(), query=query or None)
            batch = response.get("records", [])
            if isinstance(batch, list):
                records.extend(item for item in batch if isinstance(item, dict))
            offset = response.get("offset")
            if not offset:
                break
        return records

    def list_comments(self, record_id: str) -> list[dict[str, Any]]:
        record_id = record_id.strip()
        if not record_id:
            raise AirtableError("record_id is required")
        url = f"{self._table_url()}/{urllib.parse.quote(record_id, safe='')}/comments"
        comments: list[dict[str, Any]] = []
        offset: str | None = None
        while True:
            query: dict[str, str] = {}
            if offset:
                query["offset"] = offset
            response = self._request("GET", url, query=query or None)
            batch = response.get("comments", [])
            if isinstance(batch, list):
                comments.extend(item for item in batch if isinstance(item, dict))
            offset = response.get("offset")
            if not offset:
                break
        return comments

    def get_record(self, record_id: str) -> dict[str, Any]:
        record_id = record_id.strip()
        if not record_id:
            raise AirtableError("record_id is required")
        response = self._request("GET", f"{self._table_url()}/{urllib.parse.quote(record_id, safe='')}")
        if not isinstance(response, dict):
            raise AirtableError("Unexpected Airtable response while reading record")
        return response

    def update_record_fields(self, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        record_id = record_id.strip()
        if not record_id:
            raise AirtableError("record_id is required")
        if not isinstance(fields, dict) or not fields:
            raise AirtableError("fields must be a non-empty dict")
        response = self._request(
            "PATCH",
            f"{self._table_url()}/{urllib.parse.quote(record_id, safe='')}",
            body={"fields": fields},
        )
        if not isinstance(response, dict):
            raise AirtableError("Unexpected Airtable response while updating record")
        return response

    def upload_attachment(
        self,
        record_id: str,
        field_name: str,
        file_path: Path,
        *,
        content_type: str = "image/jpeg",
        replace: bool = True,
    ) -> dict[str, Any]:
        """Upload a local file directly to an attachment field (max 5 MB)."""
        path = Path(file_path)
        if replace:
            self.update_record_fields(record_id, {field_name: []})

        encoded_field = urllib.parse.quote(field_name, safe="")
        url = (
            f"{DEFAULT_CONTENT_API_BASE}/{self.base_id}/"
            f"{urllib.parse.quote(record_id, safe='')}/{encoded_field}/uploadAttachment"
        )
        file_bytes = path.read_bytes()
        if len(file_bytes) > 5 * 1024 * 1024:
            raise AirtableError(
                f"Attachment {path.name!r} exceeds Airtable's 5 MB upload limit"
            )
        response = self._request(
            "POST",
            url,
            body={
                "contentType": content_type,
                "file": base64.b64encode(file_bytes).decode("ascii"),
                "filename": path.name,
            },
        )
        if not isinstance(response, dict):
            raise AirtableError("Unexpected Airtable response while uploading attachment")
        return response

    def create_record_comment(self, record_id: str, text: str) -> None:
        self._request("POST", self._record_comments_url(record_id), body={"text": text})

    def delete_record(self, record_id: str) -> None:
        record_id = record_id.strip()
        if not record_id:
            raise AirtableError("record_id is required")
        self._request(
            "DELETE",
            f"{self._table_url()}/{urllib.parse.quote(record_id, safe='')}",
        )

    def create_field_records(self, field_sets: list[dict[str, Any]]) -> list[str]:
        if not field_sets:
            return []

        created_ids: list[str] = []
        for start in range(0, len(field_sets), MAX_CREATE_BATCH_SIZE):
            batch = field_sets[start : start + MAX_CREATE_BATCH_SIZE]
            payload = {"records": [{"fields": dict(fields)} for fields in batch]}
            response = self._request("POST", self._table_url(), body=payload)
            created_records = response.get("records", [])
            if not isinstance(created_records, list):
                continue
            for created_record in created_records:
                record_id = created_record.get("id")
                if isinstance(record_id, str) and record_id:
                    created_ids.append(record_id)
        return created_ids

    def create_records(
        self,
        records: list[dict[str, Any]],
        *,
        extra_fields: dict[str, Any] | None = None,
        write_comments: bool = False,
    ) -> list[str]:
        if not records:
            return []

        created_ids: list[str] = []
        for start in range(0, len(records), MAX_CREATE_BATCH_SIZE):
            batch = records[start : start + MAX_CREATE_BATCH_SIZE]
            airtable_records: list[dict[str, Any]] = []
            for record in batch:
                fields = catalog_record_to_airtable_fields(record)
                per_record_extra = record.get("_airtable_fields")
                if isinstance(per_record_extra, dict):
                    fields.update(per_record_extra)
                if extra_fields:
                    fields.update(extra_fields)
                airtable_records.append({"fields": fields})
            payload = {"records": airtable_records}
            response = self._request("POST", self._table_url(), body=payload)
            created_records = response.get("records", [])
            for catalog_record, created_record in zip(batch, created_records, strict=True):
                record_id = created_record.get("id")
                if not isinstance(record_id, str):
                    continue
                created_ids.append(record_id)
                if write_comments:
                    for comment in catalog_record_comments(catalog_record):
                        self.create_record_comment(record_id, comment)
        return created_ids

    def sync_catalog_records(self, records: list[dict[str, Any]]) -> tuple[int, int]:
        existing_titles = self.list_existing_titles()
        to_create: list[dict[str, Any]] = []
        skipped = 0

        for record in records:
            title = normalize_title(record.get("ctTitle"))
            if not title:
                skipped += 1
                continue
            if title in existing_titles:
                skipped += 1
                continue
            to_create.append(record)
            existing_titles.add(title)

        created = len(self.create_records(to_create))
        return created, skipped
