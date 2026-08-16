from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


SHARED_WORKFLOW_CONFIG = Path("config") / "workflow_config.json"
LOCAL_WORKFLOW_CONFIG = Path("workflow_config.json")


@dataclass(frozen=True)
class PersonProfile:
    name: str
    weekly_capacity_reels: int
    preferred_translation_type: str | None = None
    preferred_editing_type: str | None = None
    preferred_timing_type: str | None = None
    preferred_editor: str | None = None


@dataclass(frozen=True)
class WorkflowConfig:
    drive_url: str
    catalog_id: str
    translators: list[PersonProfile]
    editors: list[PersonProfile]
    timing_editors: list[PersonProfile]
    work_dir: Path
    target_reel_to_video_ratio: int
    max_video_seconds: int


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
    preferred_timing_type = item.get("preferred_timing_type")
    if preferred_timing_type is not None:
        preferred_timing_type = str(preferred_timing_type).strip() or None
    # Allow timing_editors profiles to reuse preferred_editing_type as an alias.
    if preferred_timing_type is None and preferred_editing_type is not None:
        preferred_timing_type = preferred_editing_type
    preferred_editor = item.get("preferred_editor")
    if preferred_editor is not None:
        preferred_editor = str(preferred_editor).strip() or None
    return PersonProfile(
        name=name,
        weekly_capacity_reels=weekly_capacity_reels,
        preferred_translation_type=preferred_translation_type,
        preferred_editing_type=preferred_editing_type,
        preferred_timing_type=preferred_timing_type,
        preferred_editor=preferred_editor,
    )


def _read_json_object(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"{path.as_posix()} must contain a JSON object")
    return data


def _load_shared_workflow_config(project_root: Path) -> dict:
    path = project_root / SHARED_WORKFLOW_CONFIG
    data = _read_json_object(path)
    if not data:
        raise RuntimeError(f"{SHARED_WORKFLOW_CONFIG.as_posix()} is required")
    return data


def _required_int(data: dict, key: str, source: str) -> int:
    if key not in data or data[key] in (None, ""):
        raise RuntimeError(f"{source} {key} is required")
    return int(data[key])


def _load_profiles_json(project_root: Path) -> dict:
    raw = os.getenv("WORKFLOW_PROFILES_JSON", "").strip()
    if raw:
        return json.loads(raw)
    data = _read_json_object(project_root / LOCAL_WORKFLOW_CONFIG)
    profiles = data.get("profiles")
    if isinstance(profiles, dict):
        return profiles
    return {}


def load_catalog_id(project_root: Path) -> str:
    from catalog_parser.parser import extract_sheet_id

    data = _load_shared_workflow_config(project_root)
    raw = str(data.get("catalog_id", "")).strip()
    if not raw:
        raise RuntimeError(f"{SHARED_WORKFLOW_CONFIG.as_posix()} catalog_id is required")
    return extract_sheet_id(raw)


def load_workflow_config(project_root: Path) -> WorkflowConfig:
    local_data = _read_json_object(project_root / LOCAL_WORKFLOW_CONFIG)
    shared = _load_shared_workflow_config(project_root)
    source = SHARED_WORKFLOW_CONFIG.as_posix()

    drive_url = (
        os.getenv("DRIVE_URL", "").strip()
        or str(local_data.get("drive_url", "")).strip()
    )
    if not drive_url:
        raise RuntimeError(
            "DRIVE_URL env var or workflow_config.json drive_url is required"
        )

    catalog_id = load_catalog_id(project_root)

    profiles = _load_profiles_json(project_root)
    translators_data = profiles.get("translators", [])
    editors_data = profiles.get("editors", [])
    timing_editors_data = profiles.get("timing_editors", [])
    if (
        not isinstance(translators_data, list)
        or not isinstance(editors_data, list)
        or not isinstance(timing_editors_data, list)
    ):
        raise RuntimeError(
            "WORKFLOW_PROFILES_JSON must contain translators[], editors[], and timing_editors[]"
        )

    translators = [p for p in (_parse_person(item) for item in translators_data) if p is not None]
    editors = [p for p in (_parse_person(item) for item in editors_data) if p is not None]
    timing_editors = [
        p for p in (_parse_person(item) for item in timing_editors_data) if p is not None
    ]
    if not translators:
        raise RuntimeError(
            "Configure translators via WORKFLOW_PROFILES_JSON or workflow_config.json profiles.translators"
        )
    if not editors:
        raise RuntimeError(
            "Configure editors via WORKFLOW_PROFILES_JSON or workflow_config.json profiles.editors"
        )
    if not timing_editors:
        raise RuntimeError(
            "Configure timing_editors via WORKFLOW_PROFILES_JSON or "
            "workflow_config.json profiles.timing_editors"
        )

    work_dir = Path(
        os.getenv("WORKFLOW_DIR", "").strip()
        or local_data.get("work_dir", "_tmp_drive_mix")
    )
    if not work_dir.is_absolute():
        work_dir = project_root / work_dir

    return WorkflowConfig(
        drive_url=drive_url,
        catalog_id=catalog_id,
        translators=translators,
        editors=editors,
        timing_editors=timing_editors,
        work_dir=work_dir,
        target_reel_to_video_ratio=_required_int(
            shared, "target_reel_to_video_ratio", source
        ),
        max_video_seconds=_required_int(shared, "max_video_seconds", source),
    )


def combined_media_output_folder_id(config: WorkflowConfig, drive_service) -> str:
    from media_publisher.sources.drive_layout import resolve_combined_media_files_id

    return resolve_combined_media_files_id(drive_service, drive_url=config.drive_url)
