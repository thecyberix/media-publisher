"""Restore Airtable table fields from a local JSON backup snapshot."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from catalog_parser.__main__ import load_env_file
from catalog_parser.airtable import AirtableClient
from catalog_parser.workflow.restore import RestorePlan, build_restore_plan
from catalog_parser.workflow.table_cache import DEFAULT_BACKUP_DIR, TableCache


def apply_restore_plan(
    plan: RestorePlan,
    airtable: AirtableClient,
    *,
    apply: bool,
    create_missing: bool,
    delete_missing: bool,
) -> dict[str, int]:
    stats = {
        "updated": 0,
        "created": 0,
        "deleted": 0,
        "skipped_updates": 0,
        "skipped_creates": 0,
        "skipped_deletes": 0,
    }

    for update in plan.updates:
        field_names = ", ".join(sorted(update.fields))
        if apply:
            airtable.update_record_fields(update.record_id, update.fields)
            stats["updated"] += 1
            print(f"UPDATED: {update.title!r} ({update.record_id}) -> {field_names}")
        else:
            stats["skipped_updates"] += 1
            print(f"WOULD UPDATE: {update.title!r} ({update.record_id}) -> {field_names}")

    if create_missing:
        create_fields = [item.fields for item in plan.creates]
        if apply and create_fields:
            created_ids = airtable.create_field_records(create_fields)
            stats["created"] = len(created_ids)
            for item, record_id in zip(plan.creates, created_ids, strict=False):
                print(f"CREATED: {item.title!r} ({record_id})")
        else:
            stats["skipped_creates"] = len(plan.creates)
            for item in plan.creates:
                print(f"WOULD CREATE: {item.title!r}")
    elif plan.creates:
        stats["skipped_creates"] = len(plan.creates)
        for item in plan.creates:
            print(f"SKIP CREATE (disabled): {item.title!r}")

    if delete_missing:
        if apply:
            for orphan in plan.orphans:
                airtable.delete_record(orphan.record_id)
                stats["deleted"] += 1
                print(f"DELETED: {orphan.title!r} ({orphan.record_id})")
        else:
            stats["skipped_deletes"] = len(plan.orphans)
            for orphan in plan.orphans:
                print(f"WOULD DELETE: {orphan.title!r} ({orphan.record_id})")
    elif plan.orphans:
        stats["skipped_deletes"] = len(plan.orphans)
        for orphan in plan.orphans:
            print(f"ORPHAN (live only): {orphan.title!r} ({orphan.record_id})")

    return stats


def print_plan_summary(plan: RestorePlan) -> None:
    print("Restore plan summary:")
    if plan.backup_fetched_at:
        print(f"  Backup fetched at: {plan.backup_fetched_at}")
    print(f"  Backup records: {plan.backup_count}")
    print(f"  Live records:   {plan.live_count}")
    print(f"  Field updates:  {len(plan.updates)}")
    print(f"  Creates:        {len(plan.creates)}")
    print(f"  Orphans:        {len(plan.orphans)}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    default_backup = PROJECT_ROOT / DEFAULT_BACKUP_DIR / "airtable-latest.json"
    parser = argparse.ArgumentParser(
        description="Restore Airtable table fields from a local JSON backup.",
    )
    parser.add_argument(
        "--backup",
        type=Path,
        default=default_backup,
        help=f"Path to backup JSON (default: {default_backup})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes to Airtable. Default is dry-run only.",
    )
    parser.add_argument(
        "--no-create-missing",
        action="store_true",
        help="Do not recreate records that exist in the backup but not in Airtable.",
    )
    parser.add_argument(
        "--delete-missing",
        action="store_true",
        help="Delete live records that are not present in the backup.",
    )
    parser.add_argument(
        "--confirm-delete",
        action="store_true",
        help="Required with --delete-missing before any delete is planned or applied.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_env_file(PROJECT_ROOT / ".env")

    token = os.getenv("AIRTABLE_TOKEN", "").strip()
    base_id = os.getenv("AIRTABLE_BASE_ID", "").strip()
    table_name = os.getenv("AIRTABLE_TABLE_NAME", "").strip()
    if not token or not base_id or not table_name:
        print("Missing AIRTABLE_TOKEN, AIRTABLE_BASE_ID, or AIRTABLE_TABLE_NAME in .env")
        return 1

    backup_path = args.backup
    if not backup_path.is_file():
        print(f"Backup file not found: {backup_path}")
        return 1

    if args.delete_missing and not args.confirm_delete:
        print("Refusing to delete records without --confirm-delete")
        return 1

    backup_cache = TableCache.from_backup_file(backup_path)
    airtable = AirtableClient(token, base_id, table_name)
    live_records = airtable.list_records()
    plan = build_restore_plan(
        backup_cache.records,
        live_records,
        backup_fetched_at=backup_cache.backup_metadata.get("fetched_at"),
    )

    print_plan_summary(plan)
    if plan.changed_record_count == 0:
        print("\nNo differences found. Nothing to restore.")
        return 0

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"\n{mode}:")
    stats = apply_restore_plan(
        plan,
        airtable,
        apply=args.apply,
        create_missing=not args.no_create_missing,
        delete_missing=args.delete_missing and args.confirm_delete,
    )

    print(
        "\nDone: "
        f"updated={stats['updated']}, "
        f"created={stats['created']}, "
        f"deleted={stats['deleted']}, "
        f"skipped_updates={stats['skipped_updates']}, "
        f"skipped_creates={stats['skipped_creates']}, "
        f"skipped_deletes={stats['skipped_deletes']}"
    )
    if not args.apply:
        print("No changes were written. Re-run with --apply to restore.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
