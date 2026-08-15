from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from catalog_parser.parser import duration_to_type, parse_duration
from catalog_parser.drive_docs import extract_drive_folder_id

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
FIELD_TIMING_EDITOR = "Timing Editor"
FIELD_VIDEO_DESCRIPTION_TRANSLATED = "Video description translated"
FIELD_VIDEO_NAME_TRANSLATED = "Video name translated"
FIELD_VIDEO_CAPTION_TRANSLATED = "Video caption translated"

STATUS_TODO = "1. To do"
STATUS_TRANSLATION_DONE = "2. Translation done"
STATUS_EDITING_DONE = "3. Editing done"
STATUS_SYNC_DONE = "5. Synchronization done"
STATUS_NOT_ASSIGNED = "7. Not Assigned"
STATUS_DONE_PUBLISHED = "Done & Published"

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


def normalize_original_video_name_key(value: Any) -> str | None:
    """Casefolded Original Video Name / ytTitle key for ingest dedup."""
    original = normalize_original_video_name(value)
    if not original:
        return None
    collapsed = original.replace("\u2019", "'").replace("\u2018", "'")
    return normalize_title(collapsed)


def normalize_original_video_key(value: Any) -> str | None:
    """Platform id key from Original Video / ctLink (yt:… / ig:…)."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    url = value.strip()
    if not url:
        return None
    from media_publisher.sources.source_thumbnail import (
        parse_instagram_shortcode,
        parse_youtube_video_id,
    )

    youtube_id = parse_youtube_video_id(url)
    if youtube_id:
        return f"yt:{youtube_id}"
    shortcode = parse_instagram_shortcode(url)
    if shortcode:
        return f"ig:{shortcode}"
    return None


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


def normalize_title_variants(value: Any) -> set[str]:
    variants: set[str] = set()
    direct = normalize_title(value)
    if direct:
        variants.add(direct)
    original = normalize_original_video_name_key(value)
    if original:
        variants.add(original)
    return variants


# Title dedup is scoped by Type. Archive titles without a type use ANY.
TITLE_KEY_ANY_TYPE = "*"


def normalize_type_key(video_type: Any) -> str:
    if not isinstance(video_type, str) or not video_type.strip():
        return TITLE_KEY_ANY_TYPE
    return video_type.strip().casefold()


def make_title_identity_key(title: str, video_type: Any = None) -> str:
    return f"{normalize_type_key(video_type)}\t{title}"


def resolve_record_type_key(
    record: dict[str, Any],
    *,
    video_type: str | None = None,
) -> str:
    if isinstance(video_type, str) and video_type.strip():
        return normalize_type_key(video_type)
    duration = parse_duration(record.get("ctDuration"))
    if duration is not None:
        return normalize_type_key(duration_to_type(duration))
    return TITLE_KEY_ANY_TYPE


def title_identity_keys(
    title_value: Any,
    video_type: Any = None,
) -> set[str]:
    """Typed title keys for one catalog/Airtable title value."""
    keys: set[str] = set()
    type_key = normalize_type_key(video_type)
    for variant in normalize_title_variants(title_value):
        keys.add(make_title_identity_key(variant, type_key))
    return keys


def title_identity_collides(
    existing_title_keys: set[str],
    title_value: Any,
    video_type: Any = None,
) -> bool:
    """True when title is taken for this type (or as an any-type archive key)."""
    for variant in normalize_title_variants(title_value):
        if make_title_identity_key(variant, video_type) in existing_title_keys:
            return True
        if make_title_identity_key(variant, TITLE_KEY_ANY_TYPE) in existing_title_keys:
            return True
    return False


@dataclass(frozen=True)
class AirtableArchiveSource:
    base_id: str
    table_name: str
    title_fields: tuple[str, ...]

    @property
    def title_field(self) -> str:
        return self.title_fields[0]


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
    bg_title = record.get("bgTitle")
    if isinstance(bg_title, str) and bg_title.strip():
        fields[FIELD_VIDEO_NAME_TRANSLATED] = bg_title.strip()
    bg_description = record.get("bgDescription")
    if isinstance(bg_description, str) and bg_description.strip():
        fields[FIELD_VIDEO_DESCRIPTION_TRANSLATED] = bg_description.strip()
    bg_caption = record.get("bgCaption")
    if isinstance(bg_caption, str) and bg_caption.strip():
        fields[FIELD_VIDEO_CAPTION_TRANSLATED] = bg_caption.strip()
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
            raise AirtableError("AIRTABLE_URL is required (missing base id)")
        if not self.table_name:
            raise AirtableError("AIRTABLE_URL is required (missing table id)")

    def _table_url(
        self,
        *,
        table_name: str | None = None,
        base_id: str | None = None,
    ) -> str:
        encoded_table = urllib.parse.quote((table_name or self.table_name).strip(), safe="")
        return f"{self.api_base}/{(base_id or self.base_id).strip()}/{encoded_table}"

    def _record_comments_url(self, record_id: str) -> str:
        encoded_record_id = urllib.parse.quote(record_id, safe="")
        return f"{self._table_url()}/{encoded_record_id}/comments"

    def _request(
        self,
        method: str,
        url: str,
        *,
        query: dict[str, Any] | None = None,
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
        """Return typed title identity keys (``{type}\\t{title}``)."""
        keys: set[str] = set()
        offset: str | None = None
        while True:
            query: dict[str, Any] = {
                "pageSize": "100",
                "fields[]": [FIELD_TITLE, FIELD_TYPE],
            }
            if offset:
                query["offset"] = offset
            response = self._request("GET", self._table_url(), query=query)
            for item in response.get("records", []):
                if not isinstance(item, dict):
                    continue
                fields = item.get("fields")
                if not isinstance(fields, dict):
                    continue
                keys.update(
                    title_identity_keys(
                        fields.get(FIELD_TITLE),
                        fields.get(FIELD_TYPE),
                    )
                )
            offset = response.get("offset")
            if not offset:
                break
        return keys

    def list_existing_video_folder_ids(self) -> set[str]:
        folder_ids: set[str] = set()
        offset: str | None = None
        while True:
            query: dict[str, Any] = {
                "pageSize": "100",
                "fields[]": [FIELD_VIDEO_FOLDER],
            }
            if offset:
                query["offset"] = offset
            response = self._request("GET", self._table_url(), query=query)
            for item in response.get("records", []):
                if not isinstance(item, dict):
                    continue
                fields = item.get("fields")
                if not isinstance(fields, dict):
                    continue
                link = fields.get(FIELD_VIDEO_FOLDER)
                if not isinstance(link, str) or not link.strip():
                    continue
                folder_id = extract_drive_folder_id(link)
                if folder_id:
                    folder_ids.add(folder_id)
            offset = response.get("offset")
            if not offset:
                break
        return folder_ids

    def list_existing_original_video_names(self) -> set[str]:
        names: set[str] = set()
        offset: str | None = None
        while True:
            query: dict[str, Any] = {
                "pageSize": "100",
                "fields[]": [FIELD_ORIGINAL_VIDEO_NAME],
            }
            if offset:
                query["offset"] = offset
            response = self._request("GET", self._table_url(), query=query)
            for item in response.get("records", []):
                if not isinstance(item, dict):
                    continue
                fields = item.get("fields")
                if not isinstance(fields, dict):
                    continue
                key = normalize_original_video_name_key(
                    fields.get(FIELD_ORIGINAL_VIDEO_NAME)
                )
                if key:
                    names.add(key)
            offset = response.get("offset")
            if not offset:
                break
        return names

    def list_existing_original_video_keys(self) -> set[str]:
        keys: set[str] = set()
        offset: str | None = None
        while True:
            query: dict[str, Any] = {
                "pageSize": "100",
                "fields[]": [FIELD_ORIGINAL_VIDEO],
            }
            if offset:
                query["offset"] = offset
            response = self._request("GET", self._table_url(), query=query)
            for item in response.get("records", []):
                if not isinstance(item, dict):
                    continue
                fields = item.get("fields")
                if not isinstance(fields, dict):
                    continue
                key = normalize_original_video_key(fields.get(FIELD_ORIGINAL_VIDEO))
                if key:
                    keys.add(key)
            offset = response.get("offset")
            if not offset:
                break
        return keys

    def list_accessible_bases(self) -> list[dict[str, Any]]:
        response = self._request("GET", f"{self.api_base}/meta/bases")
        bases = response.get("bases", [])
        return [base for base in bases if isinstance(base, dict)]

    def list_base_tables(self, base_id: str) -> list[dict[str, Any]]:
        encoded_base_id = urllib.parse.quote(base_id.strip(), safe="")
        response = self._request("GET", f"{self.api_base}/meta/bases/{encoded_base_id}/tables")
        tables = response.get("tables", [])
        return [table for table in tables if isinstance(table, dict)]

    def list_title_variants(
        self,
        *,
        title_field: str | None = None,
        title_fields: tuple[str, ...] | None = None,
        table_name: str | None = None,
        base_id: str | None = None,
    ) -> set[str]:
        fields = title_fields or ((title_field,) if title_field else ())
        fields = tuple(field for field in fields if field)
        if not fields:
            raise AirtableError("At least one title field is required")

        titles: set[str] = set()
        offset: str | None = None

        while True:
            query: dict[str, Any] = {"fields[]": list(fields)}
            if offset:
                query["offset"] = offset

            response = self._request(
                "GET",
                self._table_url(table_name=table_name, base_id=base_id),
                query=query,
            )
            for record in response.get("records", []):
                record_fields = record.get("fields", {})
                if not isinstance(record_fields, dict):
                    continue
                for field_name in fields:
                    titles.update(normalize_title_variants(record_fields.get(field_name)))

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
        base_id: str | None = None,
        table_name: str | None = None,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        offset: str | None = None
        while True:
            query: dict[str, str] = {}
            if filter_formula:
                query["filterByFormula"] = filter_formula
            if offset:
                query["offset"] = offset
            response = self._request(
                "GET",
                self._table_url(base_id=base_id, table_name=table_name),
                query=query or None,
            )
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
        existing_titles = load_existing_titles_for_ingest(self)
        existing_folder_ids = load_existing_video_folder_ids_for_ingest(self)
        existing_original_video_names = load_existing_original_video_names_for_ingest(
            self
        )
        existing_original_video_keys = load_existing_original_video_keys_for_ingest(
            self
        )
        to_create: list[dict[str, Any]] = []
        skipped = 0

        for record in records:
            title = normalize_title(record.get("ctTitle"))
            if not title:
                skipped += 1
                continue
            type_key = resolve_record_type_key(record)
            if title_identity_collides(existing_titles, record.get("ctTitle"), type_key):
                skipped += 1
                continue
            folder_id = extract_drive_folder_id(str(record.get("pkgLink") or ""))
            if folder_id and folder_id in existing_folder_ids:
                skipped += 1
                continue
            yt_title_key = normalize_original_video_name_key(record.get("ytTitle"))
            if yt_title_key and yt_title_key in existing_original_video_names:
                skipped += 1
                continue
            original_video_key = normalize_original_video_key(record.get("ctLink"))
            if (
                original_video_key
                and original_video_key in existing_original_video_keys
            ):
                skipped += 1
                continue
            to_create.append(record)
            existing_titles.update(title_identity_keys(record.get("ctTitle"), type_key))
            if folder_id:
                existing_folder_ids.add(folder_id)
            if yt_title_key:
                existing_original_video_names.add(yt_title_key)
            if original_video_key:
                existing_original_video_keys.add(original_video_key)

        created = len(self.create_records(to_create))
        return created, skipped


def load_existing_titles_for_ingest(
    airtable: AirtableClient,
    *,
    table_cache: Any | None = None,
    archive_sources: list[AirtableArchiveSource] | None = None,
    project_root: Path | None = None,
) -> set[str]:
    from catalog_parser.workflow.archive_sources import resolve_archive_sources
    from catalog_parser.workflow.archive_title_cache import load_archive_titles

    if table_cache is not None:
        titles = table_cache.existing_title_keys()
    else:
        titles = airtable.list_existing_titles()

    if archive_sources is None:
        records = table_cache.records if table_cache is not None else None
        archive_sources = resolve_archive_sources(airtable, records=records)

    filtered_sources: list[AirtableArchiveSource] = []
    for source in archive_sources:
        if (
            source.base_id == airtable.base_id
            and source.table_name == airtable.table_name
            and source.title_fields == (FIELD_TITLE,)
        ):
            continue
        filtered_sources.append(source)

    if filtered_sources:
        root = project_root or Path(__file__).resolve().parents[2]
        # Archive rows are type-unknown: block that title for every ingest type.
        for archive_title in load_archive_titles(
            airtable,
            filtered_sources,
            project_root=root,
        ):
            titles.update(title_identity_keys(archive_title, TITLE_KEY_ANY_TYPE))
    return titles


def load_existing_video_folder_ids_for_ingest(
    airtable: AirtableClient,
    *,
    table_cache: Any | None = None,
) -> set[str]:
    """Live-table Drive folder ids already present as Video Folder."""
    if table_cache is not None:
        return table_cache.existing_video_folder_ids()
    return airtable.list_existing_video_folder_ids()


def load_existing_original_video_names_for_ingest(
    airtable: AirtableClient,
    *,
    table_cache: Any | None = None,
) -> set[str]:
    """Live-table Original Video Name keys already present (ytTitle dedup)."""
    if table_cache is not None:
        return table_cache.existing_original_video_names()
    return airtable.list_existing_original_video_names()


def load_existing_original_video_keys_for_ingest(
    airtable: AirtableClient,
    *,
    table_cache: Any | None = None,
) -> set[str]:
    """Live-table Original Video platform keys already present (ctLink dedup)."""
    if table_cache is not None:
        return table_cache.existing_original_video_keys()
    return airtable.list_existing_original_video_keys()
