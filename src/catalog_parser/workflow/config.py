from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PersonProfile:
    name: str
    weekly_capacity_reels: int
    preferred_translation_type: str | None = None
    preferred_editing_type: str | None = None


@dataclass(frozen=True)
class WorkflowConfig:
    output_drive_folder: str
    translators: list[PersonProfile]
    editors: list[PersonProfile]
    work_dir: Path = Path("_tmp_drive_mix")
    target_reel_to_video_ratio: int = 6
    max_video_seconds: int = 15 * 60


def _parse_person(item: object) -> PersonProfile | None:
    if not isinstance(item, dict):
        return None
    name = str(item.get("name", "")).strip()
    if not name:
        return None
    weekly_capacity_reels = int(item.get("weekly_capacity_reels", 0))
    if weekly_capacity_reels <= 0:
        return None
    preferred_translation_type = item.get("preferred_translation_type")
    if preferred_translation_type is not None:
        preferred_translation_type = str(preferred_translation_type).strip() or None
    preferred_editing_type = item.get("preferred_editing_type")
    if preferred_editing_type is not None:
        preferred_editing_type = str(preferred_editing_type).strip() or None
    return PersonProfile(
        name=name,
        weekly_capacity_reels=weekly_capacity_reels,
        preferred_translation_type=preferred_translation_type,
        preferred_editing_type=preferred_editing_type,
    )


def _load_profiles_json(project_root: Path) -> dict:
    raw = os.getenv("WORKFLOW_PROFILES_JSON", "").strip()
    if raw:
        return json.loads(raw)
    config_path = project_root / "workflow_config.json"
    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
        profiles = data.get("profiles")
        if isinstance(profiles, dict):
            return profiles
    return {}


def load_workflow_config(project_root: Path) -> WorkflowConfig:
    config_path = project_root / "workflow_config.json"
    file_data: dict = {}
    if config_path.exists():
        file_data = json.loads(config_path.read_text(encoding="utf-8"))

    output_drive_folder = (
        os.getenv("OUTPUT_DRIVE_FOLDER", "").strip()
        or str(file_data.get("output_drive_folder", "")).strip()
    )
    if not output_drive_folder:
        raise RuntimeError(
            "OUTPUT_DRIVE_FOLDER env var or workflow_config.json output_drive_folder is required"
        )

    profiles = _load_profiles_json(project_root)
    translators_data = profiles.get("translators", [])
    editors_data = profiles.get("editors", [])
    if not isinstance(translators_data, list) or not isinstance(editors_data, list):
        raise RuntimeError("WORKFLOW_PROFILES_JSON must contain translators[] and editors[]")

    translators = [p for p in (_parse_person(item) for item in translators_data) if p is not None]
    editors = [p for p in (_parse_person(item) for item in editors_data) if p is not None]
    if not translators:
        raise RuntimeError(
            "Configure translators via WORKFLOW_PROFILES_JSON or workflow_config.json profiles.translators"
        )
    if not editors:
        raise RuntimeError(
            "Configure editors via WORKFLOW_PROFILES_JSON or workflow_config.json profiles.editors"
        )

    work_dir = Path(
        os.getenv("WORKFLOW_DIR", "").strip()
        or file_data.get("work_dir", "_tmp_drive_mix")
    )
    if not work_dir.is_absolute():
        work_dir = project_root / work_dir

    return WorkflowConfig(
        output_drive_folder=output_drive_folder,
        translators=translators,
        editors=editors,
        work_dir=work_dir,
        target_reel_to_video_ratio=int(
            os.getenv("WORKFLOW_REEL_TO_VIDEO_RATIO", "").strip()
            or file_data.get("target_reel_to_video_ratio", 6)
        ),
        max_video_seconds=int(
            os.getenv("WORKFLOW_MAX_VIDEO_SECONDS", "").strip()
            or file_data.get("max_video_seconds", 15 * 60)
        ),
    )
