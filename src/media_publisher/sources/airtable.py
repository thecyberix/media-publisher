from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from media_publisher.timezones import get_timezone

from media_publisher.models import PlatformName, PlatformScheduleTask, PublishJob, VideoFormat
from media_publisher.sources.canva import FIELD_CANVA_DESIGN, METADATA_CANVA_DESIGN_ID

DEFAULT_API_BASE = "https://api.airtable.com/v0"
MAX_BATCH_SIZE = 10

FIELD_ORIGINAL_VIDEO = "Original Video"
FIELD_DURATION = "Duration"
FIELD_TITLE = "Title"
FIELD_VIDEO_NAME_TRANSLATED = "Video name translated"
FIELD_VIDEO_DESCRIPTION_TRANSLATED = "Video description translated"
FIELD_TYPE = "Type"
TYPE_VIDEO = "Video"
TYPE_SHORT = "Short"
TYPE_REEL = "Reel"
TYPE_QUOTE = "Quote"
DEFAULT_PUBLISH_TIMEZONE = "Europe/Sofia"
DEFAULT_PUBLISH_HOUR = 18
FIELD_VIDEO_FOLDER = "Video Folder"
FIELD_TRANSLATION_RESOURCES = "Translation resources"
FIELD_STATUS = "Status"
STATUS_SYNC_DONE = "Synchronization done"
FIELD_SG_YT_DATE = "SG-YT-Date published"
FIELD_SG_FB_DATE = "SG-FB-Date published"
FIELD_SG_IG_DATE = "SG-IG-Date published"
FIELD_SG_YT_PUBLISHED = "SG-YT-Published video"
FIELD_SG_FB_PUBLISHED = "SG-FB-Published video"
FIELD_SG_IG_PUBLISHED = "SG-IG-Published video"
FIELD_SMEDIA_UPLOADED = "SMedia Uploaded"
SMEDIA_OPTION_YOUTUBE = "SG YouTube"
SMEDIA_OPTION_FACEBOOK = "SG Facebook"
SMEDIA_OPTION_INSTAGRAM = "SG Instagram"


@dataclass(frozen=True)
class PlatformFieldConfig:
    platform: PlatformName
    date_field: str
    published_field: str
    smedia_option: str


PLATFORM_FIELD_CONFIGS: tuple[PlatformFieldConfig, ...] = (
    PlatformFieldConfig(
        "youtube",
        FIELD_SG_YT_DATE,
        FIELD_SG_YT_PUBLISHED,
        SMEDIA_OPTION_YOUTUBE,
    ),
    PlatformFieldConfig(
        "facebook",
        FIELD_SG_FB_DATE,
        FIELD_SG_FB_PUBLISHED,
        SMEDIA_OPTION_FACEBOOK,
    ),
    PlatformFieldConfig(
        "instagram",
        FIELD_SG_IG_DATE,
        FIELD_SG_IG_PUBLISHED,
        SMEDIA_OPTION_INSTAGRAM,
    ),
)

PLATFORM_FIELD_CONFIG_BY_NAME = {config.platform: config for config in PLATFORM_FIELD_CONFIGS}


@dataclass(frozen=True)
class AirtableRecord:
    id: str
    fields: dict[str, Any]
    created_time: str | None = None


def _field_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    text = value.strip()
    return text or None


class AirtableError(RuntimeError):
    pass


def _field_multi_select(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = _field_text(value)
    return [text] if text else []


def video_format_from_type(type_value: Any) -> VideoFormat:
    text = _field_text(type_value)
    if text == TYPE_VIDEO:
        return "post"
    if text in (TYPE_SHORT, TYPE_REEL):
        return "short_form"
    return "post"


def _publish_datetime(
    year: int,
    month: int,
    day: int,
    *,
    publish_timezone: str = DEFAULT_PUBLISH_TIMEZONE,
    publish_hour: int = DEFAULT_PUBLISH_HOUR,
) -> datetime:
    return datetime(
        year,
        month,
        day,
        publish_hour,
        0,
        tzinfo=get_timezone(publish_timezone),
    )


def _parse_publish_at(
    value: Any,
    *,
    publish_timezone: str = DEFAULT_PUBLISH_TIMEZONE,
    publish_hour: int = DEFAULT_PUBLISH_HOUR,
) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(value, tz=timezone.utc)
        local = parsed.astimezone(get_timezone(publish_timezone))
        return _publish_datetime(
            local.year,
            local.month,
            local.day,
            publish_timezone=publish_timezone,
            publish_hour=publish_hour,
        )

    text = _field_text(value)
    if not text:
        return None

    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        try:
            year, month, day = (int(part) for part in text.split("-"))
            return _publish_datetime(
                year,
                month,
                day,
                publish_timezone=publish_timezone,
                publish_hour=publish_hour,
            )
        except ValueError:
            return None

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone(get_timezone(publish_timezone))
    return _publish_datetime(
        local.year,
        local.month,
        local.day,
        publish_timezone=publish_timezone,
        publish_hour=publish_hour,
    )


def has_video_name_translated(fields: dict[str, Any]) -> bool:
    return _field_text(fields.get(FIELD_VIDEO_NAME_TRANSLATED)) is not None


def is_quote_record(fields: dict[str, Any]) -> bool:
    return _field_text(fields.get(FIELD_TYPE)) == TYPE_QUOTE


def record_to_quote_job(record: AirtableRecord) -> PublishJob:
    fields = record.fields
    original_title = _field_text(fields.get(FIELD_TITLE)) or "Untitled"
    title = _field_text(fields.get(FIELD_VIDEO_NAME_TRANSLATED)) or original_title
    description = _field_text(fields.get(FIELD_VIDEO_DESCRIPTION_TRANSLATED)) or ""

    metadata: dict[str, str] = {FIELD_TITLE: original_title}
    canva_design = _field_text(fields.get(FIELD_CANVA_DESIGN))
    if canva_design:
        metadata[METADATA_CANVA_DESIGN_ID] = canva_design

    for key in (
        FIELD_TYPE,
        FIELD_CANVA_DESIGN,
        FIELD_VIDEO_NAME_TRANSLATED,
        FIELD_VIDEO_DESCRIPTION_TRANSLATED,
    ):
        value = fields.get(key)
        if value is None:
            continue
        metadata[key] = str(value)

    return PublishJob(
        title=title,
        description=description,
        airtable_record_id=record.id,
        metadata=metadata,
        tags=[],
        content_kind="image",
    )


def record_to_publish_job(record: AirtableRecord) -> PublishJob:
    fields = record.fields
    original_title = _field_text(fields.get(FIELD_TITLE)) or "Untitled"
    title = _field_text(fields.get(FIELD_VIDEO_NAME_TRANSLATED)) or ""
    description = _field_text(fields.get(FIELD_VIDEO_DESCRIPTION_TRANSLATED)) or ""
    video_url = _field_text(fields.get(FIELD_ORIGINAL_VIDEO))

    metadata: dict[str, str] = {FIELD_TITLE: original_title}
    canva_design = _field_text(fields.get(FIELD_CANVA_DESIGN))
    if canva_design:
        metadata[METADATA_CANVA_DESIGN_ID] = canva_design

    for key in (
        FIELD_TYPE,
        FIELD_DURATION,
        FIELD_VIDEO_FOLDER,
        FIELD_TRANSLATION_RESOURCES,
        FIELD_CANVA_DESIGN,
        FIELD_VIDEO_NAME_TRANSLATED,
        FIELD_VIDEO_DESCRIPTION_TRANSLATED,
    ):
        value = fields.get(key)
        if value is None:
            continue
        metadata[key] = str(value)

    return PublishJob(
        title=title,
        description=description,
        video_url=video_url,
        airtable_record_id=record.id,
        metadata=metadata,
        tags=[],
        video_format=video_format_from_type(fields.get(FIELD_TYPE)),
    )


def is_sync_done_status(value: Any) -> bool:
    text = _field_text(value)
    return bool(text and STATUS_SYNC_DONE in text)


def sync_done_filter_formula() -> str:
    return f'FIND("{STATUS_SYNC_DONE}", {{Status}} & "")'


def pending_schedule_filter_formula(*, content_type: str | None = None) -> str:
    pending_platforms = [
        f"AND({{{config.date_field}}}, NOT({{{config.published_field}}}))"
        for config in PLATFORM_FIELD_CONFIGS
    ]
    type_clause = ""
    if content_type == TYPE_QUOTE:
        type_clause = f', {{{FIELD_TYPE}}} = "{TYPE_QUOTE}"'
    elif content_type == "video":
        type_clause = f', {{{FIELD_TYPE}}} != "{TYPE_QUOTE}"'
    return f'AND({sync_done_filter_formula()}, OR({", ".join(pending_platforms)}){type_clause})'


def quotes_pending_filter_formula() -> str:
    return pending_schedule_filter_formula(content_type=TYPE_QUOTE)


@dataclass(frozen=True)
class MissingTranslationReport:
    record_id: str
    original_title: str
    platforms: tuple[PlatformName, ...]


def missing_translation_report(
    record: AirtableRecord,
    *,
    publish_timezone: str = DEFAULT_PUBLISH_TIMEZONE,
    publish_hour: int = DEFAULT_PUBLISH_HOUR,
) -> MissingTranslationReport | None:
    if is_quote_record(record.fields):
        return None
    if has_video_name_translated(record.fields):
        return None

    platforms: list[PlatformName] = []
    for config in PLATFORM_FIELD_CONFIGS:
        publish_at = _parse_publish_at(
            record.fields.get(config.date_field),
            publish_timezone=publish_timezone,
            publish_hour=publish_hour,
        )
        if publish_at is None:
            continue
        if _field_text(record.fields.get(config.published_field)):
            continue
        platforms.append(config.platform)

    if not platforms:
        return None

    return MissingTranslationReport(
        record_id=record.id,
        original_title=_field_text(record.fields.get(FIELD_TITLE)) or "Untitled",
        platforms=tuple(platforms),
    )


def fetch_missing_translation_reports(
    client: "AirtableClient",
    *,
    max_records: int | None = None,
    publish_timezone: str = DEFAULT_PUBLISH_TIMEZONE,
    publish_hour: int = DEFAULT_PUBLISH_HOUR,
) -> list[MissingTranslationReport]:
    reports: list[MissingTranslationReport] = []
    for record in client.list_records(
        filter_formula=pending_schedule_filter_formula(),
        max_records=max_records,
    ):
        report = missing_translation_report(
            record,
            publish_timezone=publish_timezone,
            publish_hour=publish_hour,
        )
        if report is not None:
            reports.append(report)
    return reports


def record_schedule_tasks(
    record: AirtableRecord,
    *,
    platforms: tuple[PlatformName, ...] | None = None,
    publish_timezone: str = DEFAULT_PUBLISH_TIMEZONE,
    publish_hour: int = DEFAULT_PUBLISH_HOUR,
    quotes_only: bool = False,
    videos_only: bool = False,
) -> list[PlatformScheduleTask]:
    if not is_sync_done_status(record.fields.get(FIELD_STATUS)):
        return []

    is_quote = is_quote_record(record.fields)
    if quotes_only and not is_quote:
        return []
    if videos_only and is_quote:
        return []
    if is_quote:
        if not _field_text(record.fields.get(FIELD_CANVA_DESIGN)):
            return []
    elif not has_video_name_translated(record.fields):
        return []

    tasks: list[PlatformScheduleTask] = []
    for config in PLATFORM_FIELD_CONFIGS:
        if platforms is not None and config.platform not in platforms:
            continue
        publish_at = _parse_publish_at(
            record.fields.get(config.date_field),
            publish_timezone=publish_timezone,
            publish_hour=publish_hour,
        )
        if publish_at is None:
            continue
        if _field_text(record.fields.get(config.published_field)):
            continue

        job = record_to_quote_job(record) if is_quote else record_to_publish_job(record)
        job.publish_at = publish_at
        if is_quote:
            job.video_format = "short_form"
        tasks.append(
            PlatformScheduleTask(
                platform=config.platform,
                publish_at=publish_at,
                job=job,
                record_id=record.id,
                record_fields=dict(record.fields),
            )
        )
    return tasks


def build_platform_published_update(
    record_fields: dict[str, Any],
    platform: PlatformName,
    permalink: str,
) -> dict[str, Any]:
    config = PLATFORM_FIELD_CONFIG_BY_NAME[platform]
    update: dict[str, Any] = {config.published_field: permalink}
    uploaded = _field_multi_select(record_fields.get(FIELD_SMEDIA_UPLOADED))
    if config.smedia_option not in uploaded:
        update[FIELD_SMEDIA_UPLOADED] = uploaded + [config.smedia_option]
    return update


def mark_platform_scheduled(
    client: AirtableClient,
    *,
    record_id: str,
    record_fields: dict[str, Any],
    platform: PlatformName,
    permalink: str,
) -> AirtableRecord:
    return client.update_record(
        record_id,
        build_platform_published_update(record_fields, platform, permalink),
    )


def fetch_pending_schedule_tasks(
    client: AirtableClient,
    *,
    max_records: int | None = None,
    platforms: tuple[PlatformName, ...] | None = None,
    publish_timezone: str = DEFAULT_PUBLISH_TIMEZONE,
    publish_hour: int = DEFAULT_PUBLISH_HOUR,
    quotes_only: bool = False,
    videos_only: bool = False,
) -> list[PlatformScheduleTask]:
    """Load catalog rows ready to schedule on one or more social platforms."""
    filter_formula = (
        quotes_pending_filter_formula()
        if quotes_only
        else pending_schedule_filter_formula(content_type="video" if videos_only else None)
    )
    tasks: list[PlatformScheduleTask] = []
    for record in client.list_records(
        filter_formula=filter_formula,
        max_records=max_records,
    ):
        tasks.extend(
            record_schedule_tasks(
                record,
                platforms=platforms,
                publish_timezone=publish_timezone,
                publish_hour=publish_hour,
                quotes_only=quotes_only,
                videos_only=videos_only,
            )
        )
    return tasks


def fetch_pending_quote_tasks(
    client: AirtableClient,
    *,
    max_records: int | None = None,
    publish_timezone: str = DEFAULT_PUBLISH_TIMEZONE,
    publish_hour: int = DEFAULT_PUBLISH_HOUR,
) -> list[PlatformScheduleTask]:
    return fetch_pending_schedule_tasks(
        client,
        max_records=max_records,
        publish_timezone=publish_timezone,
        publish_hour=publish_hour,
        quotes_only=True,
    )


class AirtableClient:
    def __init__(
        self,
        token: str,
        base_id: str,
        table_name: str,
        *,
        api_base: str = DEFAULT_API_BASE,
        view: str | None = None,
    ) -> None:
        self.token = token.strip()
        self.base_id = base_id.strip()
        self.table_name = table_name.strip()
        self.api_base = api_base.rstrip("/")
        self.view = view.strip() if view else None
        if not self.token:
            raise AirtableError("AIRTABLE_TOKEN is required")
        if not self.base_id:
            raise AirtableError("AIRTABLE_BASE_ID is required")
        if not self.table_name:
            raise AirtableError("AIRTABLE_TABLE_NAME is required")

    def _table_url(self) -> str:
        encoded_table = urllib.parse.quote(self.table_name, safe="")
        return f"{self.api_base}/{self.base_id}/{encoded_table}"

    def _record_url(self, record_id: str) -> str:
        encoded_record_id = urllib.parse.quote(record_id, safe="")
        return f"{self._table_url()}/{encoded_record_id}"

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

    @staticmethod
    def _parse_record(payload: dict[str, Any]) -> AirtableRecord:
        record_id = payload.get("id")
        if not isinstance(record_id, str):
            raise AirtableError("Airtable response is missing record id")
        fields = payload.get("fields", {})
        if not isinstance(fields, dict):
            raise AirtableError("Airtable response has invalid fields payload")
        created_time = payload.get("createdTime")
        return AirtableRecord(
            id=record_id,
            fields=fields,
            created_time=created_time if isinstance(created_time, str) else None,
        )

    def _list_query(
        self,
        *,
        offset: str | None = None,
        fields: list[str] | None = None,
        max_records: int | None = None,
        filter_formula: str | None = None,
    ) -> dict[str, str | list[str]]:
        query: dict[str, str | list[str]] = {}
        if offset:
            query["offset"] = offset
        if self.view:
            query["view"] = self.view
        if max_records is not None:
            query["maxRecords"] = str(max_records)
        if filter_formula:
            query["filterByFormula"] = filter_formula
        if fields:
            query["fields[]"] = fields
        return query

    def iter_records(
        self,
        *,
        fields: list[str] | None = None,
        max_records: int | None = None,
        filter_formula: str | None = None,
    ) -> Iterator[AirtableRecord]:
        offset: str | None = None
        yielded = 0

        while True:
            query = self._list_query(
                offset=offset,
                fields=fields,
                max_records=max_records,
                filter_formula=filter_formula,
            )
            response = self._request("GET", self._table_url(), query=query)
            for payload in response.get("records", []):
                if not isinstance(payload, dict):
                    continue
                yield self._parse_record(payload)
                yielded += 1
                if max_records is not None and yielded >= max_records:
                    return

            offset = response.get("offset")
            if not offset:
                break

    def list_records(
        self,
        *,
        fields: list[str] | None = None,
        max_records: int | None = None,
        filter_formula: str | None = None,
    ) -> list[AirtableRecord]:
        return list(
            self.iter_records(
                fields=fields,
                max_records=max_records,
                filter_formula=filter_formula,
            )
        )

    def get_record(self, record_id: str) -> AirtableRecord:
        response = self._request("GET", self._record_url(record_id))
        if not isinstance(response, dict):
            raise AirtableError("Airtable response is not a record object")
        return self._parse_record(response)

    def update_record(self, record_id: str, fields: dict[str, Any]) -> AirtableRecord:
        response = self._request(
            "PATCH",
            self._record_url(record_id),
            body={"fields": fields},
        )
        if not isinstance(response, dict):
            raise AirtableError("Airtable response is not a record object")
        return self._parse_record(response)

    def update_records(
        self,
        updates: list[tuple[str, dict[str, Any]]],
    ) -> list[AirtableRecord]:
        if not updates:
            return []

        updated: list[AirtableRecord] = []
        for start in range(0, len(updates), MAX_BATCH_SIZE):
            batch = updates[start : start + MAX_BATCH_SIZE]
            payload = {
                "records": [
                    {"id": record_id, "fields": fields}
                    for record_id, fields in batch
                ]
            }
            response = self._request("PATCH", self._table_url(), body=payload)
            for record_payload in response.get("records", []):
                if isinstance(record_payload, dict):
                    updated.append(self._parse_record(record_payload))
        return updated

    def test_connection(self, *, max_records: int = 1) -> int:
        return len(self.list_records(max_records=max_records))


def fetch_publish_jobs(
    *,
    token: str,
    base_id: str,
    table_name: str,
    api_base: str = DEFAULT_API_BASE,
    view: str | None = None,
    max_records: int | None = None,
    filter_formula: str | None = None,
) -> list[PublishJob]:
    """Load records from Airtable and map them to publish jobs."""
    client = AirtableClient(
        token=token,
        base_id=base_id,
        table_name=table_name,
        api_base=api_base,
        view=view,
    )
    return [
        record_to_publish_job(record)
        for record in client.list_records(
            max_records=max_records,
            filter_formula=filter_formula,
        )
    ]
