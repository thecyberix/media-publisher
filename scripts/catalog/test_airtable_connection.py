from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from catalog_parser.__main__ import load_env_file

load_env_file(PROJECT_ROOT / ".env")

token = os.environ["AIRTABLE_TOKEN"]
base = os.environ["AIRTABLE_BASE_ID"]
table_name = os.environ["AIRTABLE_TABLE_NAME"]
table_id = "tblji1RaFztkeDn04"


def try_get(label: str, table_ref: str) -> None:
    encoded = urllib.parse.quote(table_ref, safe="")
    url = f"https://api.airtable.com/v0/{base}/{encoded}?maxRecords=1"
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode())
            print(f"OK {label}: {len(data.get('records', []))} record(s) returned")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"FAIL {label}: HTTP {exc.code} {detail[:300]}")


print(f"Table name from .env: {table_name!r}")
try_get("by name", table_name)
try_get("by id", table_id)
