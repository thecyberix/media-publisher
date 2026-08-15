from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class QuotesConfigError(RuntimeError):
    pass


SPREADSHEET_ID_PATTERN = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


def extract_spreadsheet_id(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise QuotesConfigError(
            "TRANSLATED_QUOTES_URL is required (Google Sheet URL or spreadsheet id)"
        )
    match = SPREADSHEET_ID_PATTERN.search(text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9-_]+", text):
        return text
    raise QuotesConfigError(
        "Invalid TRANSLATED_QUOTES_URL. Expected "
        "https://docs.google.com/spreadsheets/d/<id>/..."
    )


def translated_quotes_url_from_env() -> str:
    return os.getenv("TRANSLATED_QUOTES_URL", "").strip()


def apply_translated_quotes_url(payload: dict[str, Any]) -> dict[str, Any]:
    url = translated_quotes_url_from_env()
    if not url:
        return payload
    sheet_id = extract_spreadsheet_id(url)
    updated = dict(payload)
    sheet = dict(updated.get("quotes_sheet") or {})
    sheet["spreadsheet_id"] = sheet_id
    sheet["spreadsheet_url"] = url
    updated["quotes_sheet"] = sheet
    return updated


@dataclass(frozen=True)
class QuotesSourcesConfig:
    path: Path
    payload: dict[str, Any]

    @property
    def spreadsheet_id(self) -> str:
        value = self.payload.get("quotes_sheet", {}).get("spreadsheet_id")
        if not isinstance(value, str) or not value.strip():
            raise QuotesConfigError(
                "TRANSLATED_QUOTES_URL is required (Google Sheet URL or spreadsheet id)"
            )
        return value.strip()

    @property
    def quotes_sheet(self) -> dict[str, Any]:
        sheet = self.payload.get("quotes_sheet")
        if not isinstance(sheet, dict):
            raise QuotesConfigError("quotes_sheet config is required")
        return sheet

    @property
    def backgrounds_drive(self) -> dict[str, Any]:
        drive = self.payload.get("backgrounds_drive")
        if not isinstance(drive, dict):
            raise QuotesConfigError("backgrounds_drive config is required")
        return drive

    @property
    def canva_templates(self) -> dict[str, Any]:
        templates = self.payload.get("canva_templates")
        if not isinstance(templates, dict):
            raise QuotesConfigError("canva_templates config is required")
        return templates

    @property
    def renders(self) -> dict[str, Any]:
        renders = self.payload.get("renders")
        if not isinstance(renders, dict):
            raise QuotesConfigError("renders config is required")
        return renders

    def variant_template_dir(self, variant: str) -> Path:
        template = self.canva_templates.get(variant)
        if not isinstance(template, dict):
            raise QuotesConfigError(f"Unknown template variant: {variant!r}")
        local_dir = template.get("local_dir")
        if not isinstance(local_dir, str) or not local_dir.strip():
            raise QuotesConfigError(f"canva_templates.{variant}.local_dir is required")
        return self.path.parent.parent / local_dir

    def variant_layouts_config(self, variant: str) -> Path:
        template = self.canva_templates.get(variant)
        if not isinstance(template, dict):
            raise QuotesConfigError(f"Unknown template variant: {variant!r}")
        layouts_config = template.get("layouts_config")
        if not isinstance(layouts_config, str) or not layouts_config.strip():
            raise QuotesConfigError(
                f"canva_templates.{variant}.layouts_config is required"
            )
        return self.path.parent.parent / layouts_config

    def variant_render_dir(self, variant: str) -> Path:
        key = f"{variant}_dir"
        render_dir = self.renders.get(key)
        if not isinstance(render_dir, str) or not render_dir.strip():
            raise QuotesConfigError(f"renders.{key} is required")
        return self.path.parent.parent / render_dir

    def variant_background_dir(self, variant: str) -> Path:
        variants = self.backgrounds_drive.get("variants")
        if not isinstance(variants, dict):
            raise QuotesConfigError("backgrounds_drive.variants is required")
        variant_config = variants.get(variant)
        if not isinstance(variant_config, dict):
            raise QuotesConfigError(f"Unknown background variant: {variant!r}")
        download_dir = variant_config.get("download_dir")
        if not isinstance(download_dir, str) or not download_dir.strip():
            raise QuotesConfigError(
                f"backgrounds_drive.variants.{variant}.download_dir is required"
            )
        return self.path.parent.parent / download_dir


def load_quotes_sources_config(path: Path) -> QuotesSourcesConfig:
    if not path.is_file():
        raise QuotesConfigError(f"Quotes sources config not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise QuotesConfigError(f"Invalid quotes sources config: {path}")
    payload = apply_translated_quotes_url(payload)
    return QuotesSourcesConfig(path=path.resolve(), payload=payload)
