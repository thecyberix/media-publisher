from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from catalog_parser.__main__ import PROJECT_ROOT, load_env_file
from catalog_parser.airtable import (
    AirtableClient,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TRANSLATOR,
    FIELD_TYPE,
    normalize_title,
)
from catalog_parser.workflow.table_cache import TableCache

TODAY = [
    "Farm vs Supermarket: Ian Somerhalder & Sadhguru Guess",
    "Kalabhairava Sthapana by Sadhguru on Phalguna Purnima",
    "She Had a Perfect Life… And One Day Everything Changed",
    "Take This Simple Step for a Great 2026 | Sadhguru",
    "Sadhguru Speaks About His Mother",
    "This Meditation Opens Up Past Life Memories | Sadhguru",
    "Can Vastu Really Change Your Life? | Sadhguru",
    "Arjuna, Karna, Bhishma or Krishna – Who Was the Greatest of Mahabharat’s Warriors? | Sadhguru",
]


def main() -> int:
    load_env_file(PROJECT_ROOT / ".env")
    airtable = AirtableClient(
        token=os.environ["AIRTABLE_TOKEN"].strip(),
        base_id=os.environ["AIRTABLE_BASE_ID"].strip(),
        table_name=os.environ["AIRTABLE_TABLE_NAME"].strip(),
    )
    cache = TableCache.load(airtable, project_root=PROJECT_ROOT)
    by_title: dict[str, list[dict]] = {}
    for rec in cache.records:
        fields = rec.get("fields") or {}
        title = normalize_title(fields.get(FIELD_TITLE))
        if not title:
            continue
        by_title.setdefault(title, []).append(
            {
                "type": fields.get(FIELD_TYPE),
                "status": fields.get(FIELD_STATUS),
                "translator": fields.get(FIELD_TRANSLATOR),
                "title": fields.get(FIELD_TITLE),
                "id": rec.get("id"),
            }
        )

    need_video: list[str] = []
    for raw in TODAY:
        key = normalize_title(raw)
        rows = by_title.get(key or "", [])
        if not rows and key:
            for candidate, values in by_title.items():
                if key in candidate or candidate in key:
                    if abs(len(candidate) - len(key)) < 25:
                        rows = values
                        key = candidate
                        break
        types = sorted({str(row["type"]) for row in rows})
        print("=" * 60)
        print(raw)
        if not rows:
            print("  NOT IN AIRTABLE")
            need_video.append(raw)
            continue
        print(f"  matches={len(rows)} types={types}")
        for row in rows:
            print(
                f"  - type={row['type']!r} status={row['status']!r} "
                f"translator={row['translator']!r} id={row['id']}"
            )
        if "Video" not in types:
            need_video.append(raw)
            print("  => needs Video ingest (cross-type title only)")

    print("=" * 60)
    print(f"Need Video row: {len(need_video)}")
    for title in need_video:
        print(f"  - {title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
