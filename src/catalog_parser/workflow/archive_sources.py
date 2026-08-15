from __future__ import annotations

import re
from typing import Any

from catalog_parser.airtable import (
    AirtableArchiveSource,
    AirtableClient,
    FIELD_ORIGINAL_VIDEO_NAME,
    FIELD_STATUS,
    FIELD_TITLE,
)

STATUS_NOT_ASSIGNED = "7. Not Assigned"
ARCHIVE_POINTER_RE = re.compile(
    r"^(\d{4})\s+archive\s*:\s*(https://airtable\.com/\S+)",
    re.IGNORECASE,
)
CATALOG_TITLE_FIELDS = (FIELD_TITLE, FIELD_ORIGINAL_VIDEO_NAME)


def parse_archive_pointer_title(title: Any) -> tuple[str, str] | None:
    if not isinstance(title, str):
        return None
    match = ARCHIVE_POINTER_RE.match(title.strip())
    if not match:
        return None
    return match.group(1), match.group(2).rstrip(".,)")


def archive_pointers_from_records(records: list[dict[str, Any]]) -> list[tuple[str, str]]:
    pointers: list[tuple[str, str]] = []
    seen_years: set[str] = set()
    for record in records:
        fields = record.get("fields")
        if not isinstance(fields, dict):
            continue
        if fields.get(FIELD_STATUS) != STATUS_NOT_ASSIGNED:
            continue
        parsed = parse_archive_pointer_title(fields.get(FIELD_TITLE))
        if parsed is None:
            continue
        year, url = parsed
        if year in seen_years:
            continue
        seen_years.add(year)
        pointers.append((year, url))
    return sorted(pointers, key=lambda item: item[0])


def _find_base_for_year(bases: list[dict[str, Any]], year: str) -> dict[str, Any] | None:
    needle = f"archive {year}".casefold()
    for base in bases:
        name = str(base.get("name") or "").casefold()
        if needle in name:
            return base
    return None


def _pick_catalog_table(
    tables: list[dict[str, Any]],
    *,
    preferred_table_name: str,
) -> dict[str, Any] | None:
    for table in tables:
        if (
            table.get("name") == preferred_table_name
            or table.get("id") == preferred_table_name
        ):
            return table

    for table in tables:
        name = str(table.get("name") or "")
        if name.casefold() == "comments":
            continue
        field_names = {
            field.get("name")
            for field in table.get("fields", [])
            if isinstance(field, dict)
        }
        if FIELD_TITLE in field_names or FIELD_ORIGINAL_VIDEO_NAME in field_names:
            return table
    return None


def _pick_title_fields(table: dict[str, Any]) -> tuple[str, ...]:
    field_names = {
        str(field.get("name"))
        for field in table.get("fields", [])
        if isinstance(field, dict) and field.get("name")
    }
    selected = [field for field in CATALOG_TITLE_FIELDS if field in field_names]
    if selected:
        return tuple(selected)

    fields = table.get("fields")
    if isinstance(fields, list) and fields:
        primary = fields[0]
        if isinstance(primary, dict) and primary.get("name"):
            return (str(primary["name"]),)
    return (FIELD_ORIGINAL_VIDEO_NAME,)


def resolve_archive_sources(
    airtable: AirtableClient,
    *,
    records: list[dict[str, Any]] | None = None,
) -> list[AirtableArchiveSource]:
    if records is None:
        records = airtable.list_records(
            filter_formula=f'{{{FIELD_STATUS}}}="{STATUS_NOT_ASSIGNED}"',
        )

    pointers = archive_pointers_from_records(records)
    if not pointers:
        return []

    bases = airtable.list_accessible_bases()
    sources: list[AirtableArchiveSource] = []
    for year, _invite_url in pointers:
        base = _find_base_for_year(bases, year)
        if base is None:
            print(f"Warning: no accessible Airtable base found for {year} archive pointer")
            continue

        base_id = str(base.get("id") or "").strip()
        if not base_id:
            continue

        tables = airtable.list_base_tables(base_id)
        table = _pick_catalog_table(tables, preferred_table_name=airtable.table_name)
        if table is None:
            print(
                f"Warning: no catalog table found in archive base {base.get('name')!r} "
                f"for {year}"
            )
            continue

        table_name = str(table.get("name") or "").strip()
        if not table_name:
            continue

        title_fields = _pick_title_fields(table)
        sources.append(
            AirtableArchiveSource(
                base_id=base_id,
                table_name=table_name,
                title_fields=title_fields,
            )
        )
        print(
            f"Resolved {year} archive to base {base.get('name')!r} / "
            f"{table_name!r} using title field(s): {', '.join(title_fields)}"
        )

    return sources
