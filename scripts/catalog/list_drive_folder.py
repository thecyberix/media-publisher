from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from catalog_parser.auth import get_drive_service
from catalog_parser.__main__ import DEFAULT_CREDENTIALS, DEFAULT_TOKEN, load_env_file

load_env_file(PROJECT_ROOT / ".env")

folder = "1BNJFpQpKiO_DibCNZASBEERVq6dRvhAd"
drive = get_drive_service(DEFAULT_CREDENTIALS, DEFAULT_TOKEN)
response = (
    drive.files()
    .list(
        q=f"'{folder}' in parents and trashed=false",
        fields="files(id,name,mimeType)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    )
    .execute()
)
for file_info in response.get("files", []):
    print(file_info.get("mimeType"), "|", file_info.get("name"), "|", file_info.get("id"))
