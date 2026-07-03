from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    airtable_token: str
    airtable_base_id: str
    airtable_table_name: str
    happyscribe_api_key: str | None = None
    canva_client_id: str | None = None
    canva_client_secret: str | None = None
    meta_access_token: str | None = None
    meta_page_id: str | None = None
    meta_instagram_account_id: str | None = None


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def load_settings(project_root: Path | None = None) -> Settings:
    root = project_root or Path(__file__).resolve().parents[2]
    load_env_file(root / ".env")

    def optional(name: str) -> str | None:
        value = os.getenv(name, "").strip()
        return value or None

    return Settings(
        airtable_token=os.getenv("AIRTABLE_TOKEN", "").strip(),
        airtable_base_id=os.getenv("AIRTABLE_BASE_ID", "").strip(),
        airtable_table_name=os.getenv("AIRTABLE_TABLE_NAME", "").strip(),
        happyscribe_api_key=optional("HAPPYSCRIBE_API_KEY"),
        canva_client_id=optional("CANVA_CLIENT_ID"),
        canva_client_secret=optional("CANVA_CLIENT_SECRET"),
        meta_access_token=optional("META_ACCESS_TOKEN"),
        meta_page_id=optional("META_PAGE_ID"),
        meta_instagram_account_id=optional("META_INSTAGRAM_ACCOUNT_ID"),
    )
