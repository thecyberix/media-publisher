from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from media_publisher.models import PublishJob
from media_publisher.sources.canva import FIELD_CANVA_DESIGN, METADATA_CANVA_DESIGN_ID

DEFAULT_API_BASE = "https://api.airtable.com/v0"
MAX_BATCH_SIZE = 10

FIELD_ORIGINAL_VIDEO = "Original Video"
FIELD_DURATION = "Duration"
FIELD_TITLE = "Original Video Name"
FIELD_TYPE = "Type"
FIELD_VIDEO_FOLDER = "Video Folder"
FIELD_TRANSLATION_RESOURCES = "Translation resources"
FIELD_CANVA_DESIGN = "Canva Design"
FIELD_PUBLISH_AT = "Publish At"
FIELD_FACEBOOK_POST_ID = "Facebook Post ID"
FIELD_INSTAGRAM_MEDIA_ID = "Instagram Media ID"


class AirtableError(RuntimeError):
    pass


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


def _parse_publish_at(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)

    text = _field_text(value)
    if not text:
        return None

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def record_to_publish_job(record: AirtableRecord) -> PublishJob:
    fields = record.fields
    title = _field_text(fields.get(FIELD_TITLE)) or "Untitled"
    video_url = _field_text(fields.get(FIELD_ORIGINAL_VIDEO))
    folder_url = _field_text(fields.get(FIELD_VIDEO_FOLDER))

    metadata: dict[str, str] = {}
    canva_design = _field_text(fields.get(FIELD_CANVA_DESIGN))
    if canva_design:
        metadata[METADATA_CANVA_DESIGN_ID] = canva_design

    for key in (
        FIELD_TYPE,
        FIELD_DURATION,
        FIELD_VIDEO_FOLDER,
        FIELD_TRANSLATION_RESOURCES,
        FIELD_CANVA_DESIGN,
    ):
        value = fields.get(key)
        if value is None:
            continue
        metadata[key] = str(value)

    return PublishJob(
        title=title,
        video_url=video_url,
        airtable_record_id=record.id,
        metadata=metadata,
        tags=[metadata[FIELD_TYPE]] if FIELD_TYPE in metadata else [],
        publish_at=_parse_publish_at(fields.get(FIELD_PUBLISH_AT)),
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
