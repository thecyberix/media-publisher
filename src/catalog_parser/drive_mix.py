from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from googleapiclient.discovery import Resource

from catalog_parser.drive_combine import (
    DriveCombineError,
    DriveMediaFile,
    download_drive_file,
    resolve_ffmpeg_path,
    upload_drive_file,
    verify_drive_output_folder_access,
)
from catalog_parser.drive_docs import extract_drive_folder_id
from catalog_parser.drive_media import (
    FOLDER_MIME_TYPE,
    SHORTCUT_MIME_TYPE,
    is_audio_mime_type,
    is_video_mime_type,
    list_folder_children,
    resolve_drive_item,
)
from catalog_parser.parser import TYPE_VIDEO, parse_video_type

WINDOWS_RESERVED_CHARS = '<>:"/\\|?*'
VideoOrientation = Literal["horizontal", "vertical"]


@dataclass(frozen=True)
class MixedMediaInput:
    pkg_folder_id: str
    audio_folder_id: str
    video: DriveMediaFile
    audios: list[DriveMediaFile]


@dataclass(frozen=True)
class MixMediaCheck:
    ok: bool
    media: MixedMediaInput | None = None
    error: str | None = None


def _drive_file(item: dict[str, Any], *, parent_id: str) -> DriveMediaFile | None:
    file_id = item.get("id")
    name = item.get("name")
    mime_type = item.get("mimeType")
    if not isinstance(file_id, str) or not file_id:
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(mime_type, str) or not mime_type:
        return None
    return DriveMediaFile(
        id=file_id,
        name=name,
        mime_type=mime_type,
        parent_id=parent_id,
    )


def _pick_single_child_folder(
    drive_service: Resource,
    parent_folder_id: str,
) -> str:
    subfolders: list[str] = []
    for item in list_folder_children(drive_service, parent_folder_id):
        resolved = resolve_drive_item(drive_service, item)
        if resolved.get("mimeType") != FOLDER_MIME_TYPE:
            continue
        folder_id = resolved.get("id")
        if isinstance(folder_id, str) and folder_id:
            subfolders.append(folder_id)
    if len(subfolders) != 1:
        raise DriveCombineError(
            f"Expected exactly 1 audio subfolder under {parent_folder_id!r}, found {len(subfolders)}"
        )
    return subfolders[0]


def _list_audio_files(
    drive_service: Resource,
    audio_folder_id: str,
) -> list[DriveMediaFile]:
    files: list[DriveMediaFile] = []
    for item in list_folder_children(drive_service, audio_folder_id):
        resolved = resolve_drive_item(drive_service, item)
        mime_type = resolved.get("mimeType")
        if not isinstance(mime_type, str) or not is_audio_mime_type(mime_type):
            continue
        media = _drive_file(resolved, parent_id=audio_folder_id)
        if media is not None:
            files.append(media)
    files.sort(key=lambda f: f.name.casefold())
    return files


def _is_ref_video_name(name: str) -> bool:
    stem = Path(name).stem.casefold()
    return stem.startswith("ref_") or stem.startswith("ref-")


def _is_ocd_video_name(name: str) -> bool:
    stem = Path(name).stem.casefold()
    return stem.startswith("ocd") or stem.startswith("ocd-")


def _is_all_video_name(name: str) -> bool:
    return "all video" in Path(name).stem.casefold()


def _has_reel_in_name(name: str) -> bool:
    return "reel" in name.casefold()


def required_orientation_for_video_type(video_type: str) -> VideoOrientation:
    """Videos are horizontal; Reels and Shorts are vertical."""
    if parse_video_type(video_type) == TYPE_VIDEO:
        return "horizontal"
    return "vertical"


def orientation_from_size(width: int, height: int) -> VideoOrientation | None:
    if width > height:
        return "horizontal"
    if height > width:
        return "vertical"
    return None


def _video_orientation(
    drive_service: Resource,
    video: DriveMediaFile,
) -> VideoOrientation | None:
    # Lazy import avoids a circular dependency with drive_video_size.
    # Prefer Drive videoMediaMetadata; if missing, download and ffprobe
    # (shared by ingest eligibility and combine).
    from catalog_parser.drive_video_size import video_size_from_drive_file

    size = video_size_from_drive_file(
        drive_service,
        video.id,
        file_name=video.name,
    )
    if size is None:
        return None
    return orientation_from_size(*size)


def _filter_videos_by_orientation(
    drive_service: Resource,
    videos: list[DriveMediaFile],
    required_orientation: VideoOrientation,
) -> list[DriveMediaFile]:
    matching: list[DriveMediaFile] = []
    for video in videos:
        if _video_orientation(drive_service, video) == required_orientation:
            matching.append(video)
    return matching


def _pick_preferred_video(
    candidates: list[DriveMediaFile],
    *,
    prefer_reel_name: bool = True,
) -> DriveMediaFile:
    if len(candidates) == 1:
        return candidates[0]
    if prefer_reel_name:
        preferred = [video for video in candidates if _has_reel_in_name(video.name)]
    else:
        preferred = [video for video in candidates if not _has_reel_in_name(video.name)]
    pool = preferred or candidates
    return sorted(pool, key=lambda video: video.name.casefold())[0]


def _pick_merge_video(
    videos: list[DriveMediaFile],
    *,
    drive_service: Resource | None = None,
    required_orientation: VideoOrientation | None = None,
) -> DriveMediaFile:
    if not videos:
        raise DriveCombineError("No video files found in audio subfolder")

    all_video_candidates = [video for video in videos if _is_all_video_name(video.name)]
    if all_video_candidates:
        candidates = all_video_candidates
    else:
        candidates = [
            video
            for video in videos
            if not _is_ref_video_name(video.name) and not _is_ocd_video_name(video.name)
        ]
        if not candidates:
            raise DriveCombineError("No suitable video file found for merge")

    if required_orientation is not None:
        if drive_service is None:
            raise DriveCombineError(
                "Drive service is required to select video by orientation"
            )
        oriented = _filter_videos_by_orientation(
            drive_service,
            candidates,
            required_orientation,
        )
        if not oriented:
            raise DriveCombineError(
                f"No suitable {required_orientation} video file found for merge"
            )
        candidates = oriented

    prefer_reel_name = required_orientation != "horizontal"
    return _pick_preferred_video(candidates, prefer_reel_name=prefer_reel_name)


def _list_videos_in_folder_tree(
    drive_service: Resource,
    folder_id: str,
    *,
    max_depth: int = 3,
    depth: int = 0,
) -> list[DriveMediaFile]:
    videos: list[DriveMediaFile] = []
    for item in list_folder_children(drive_service, folder_id):
        resolved = resolve_drive_item(drive_service, item)
        mime_type = resolved.get("mimeType")
        if not isinstance(mime_type, str):
            continue
        if is_video_mime_type(mime_type):
            media = _drive_file(resolved, parent_id=folder_id)
            if media is not None:
                videos.append(media)
            continue
        if mime_type == FOLDER_MIME_TYPE and depth < max_depth:
            child_id = resolved.get("id")
            if isinstance(child_id, str) and child_id:
                videos.extend(
                    _list_videos_in_folder_tree(
                        drive_service,
                        child_id,
                        max_depth=max_depth,
                        depth=depth + 1,
                    )
                )
    return videos


def _find_video_file(
    drive_service: Resource,
    audio_folder_id: str,
    *,
    video_type: str | None = None,
) -> DriveMediaFile:
    videos = _list_videos_in_folder_tree(drive_service, audio_folder_id)
    required_orientation = (
        required_orientation_for_video_type(video_type) if video_type else None
    )
    return _pick_merge_video(
        videos,
        drive_service=drive_service,
        required_orientation=required_orientation,
    )


def find_video_and_audio_subfolder(
    drive_service: Resource,
    pkg_folder_id: str,
    *,
    video_type: str | None = None,
) -> MixedMediaInput:
    audio_folder_id = _pick_single_child_folder(drive_service, pkg_folder_id)
    audios = _list_audio_files(drive_service, audio_folder_id)
    if not audios:
        raise DriveCombineError(f"No audio files found in audio subfolder {audio_folder_id!r}")
    video = _find_video_file(drive_service, audio_folder_id, video_type=video_type)
    return MixedMediaInput(
        pkg_folder_id=pkg_folder_id,
        audio_folder_id=audio_folder_id,
        video=video,
        audios=audios,
    )


def check_mixable_media(
    drive_service: Resource,
    pkg_folder_id: str,
    *,
    video_type: str | None = None,
) -> MixMediaCheck:
    try:
        media = find_video_and_audio_subfolder(
            drive_service,
            pkg_folder_id,
            video_type=video_type,
        )
    except DriveCombineError as exc:
        return MixMediaCheck(ok=False, error=str(exc))
    except Exception as exc:
        return MixMediaCheck(ok=False, error=str(exc))
    return MixMediaCheck(ok=True, media=media)


def record_has_mixable_media(
    drive_service: Resource,
    record: dict,
    *,
    folder_link_field: str = "pkgLink",
    video_type: str | None = None,
) -> bool:
    folder_link = record.get(folder_link_field)
    if not isinstance(folder_link, str) or not folder_link.strip():
        return False
    folder_id = extract_drive_folder_id(folder_link)
    if folder_id is None:
        return False
    return check_mixable_media(
        drive_service,
        folder_id,
        video_type=video_type,
    ).ok


def format_mix_media_check(check: MixMediaCheck) -> str:
    if not check.ok:
        return f"Not mixable: {check.error or 'unknown error'}"
    if check.media is None:
        return "Mixable: ok (no details)"
    audio_names = ", ".join(item.name for item in check.media.audios)
    return (
        f"Mixable: video={check.media.video.name!r}, "
        f"audios ({len(check.media.audios)}): {audio_names}"
    )


def _sanitize_local_filename(name: str) -> str:
    cleaned = "".join("_" if ch in WINDOWS_RESERVED_CHARS or ord(ch) < 32 else ch for ch in name)
    cleaned = cleaned.rstrip(" .")
    if not cleaned:
        return "unnamed"
    return cleaned


def combine_video_with_mixed_audios(
    video_path: Path,
    audio_paths: list[Path],
    output_path: Path,
    *,
    ffmpeg_path: str | None = None,
    audio_bitrate: str = "192k",
) -> Path:
    if not audio_paths:
        raise DriveCombineError("No audio inputs provided for mix")
    ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    # Build: ffmpeg -i video -i a1 -i a2 ... -filter_complex "[1:a][2:a]...amix=inputs=N:duration=longest"
    # Map video from input 0, audio from the amix output.
    inputs: list[str] = [ffmpeg, "-y", "-i", str(video_path)]
    for audio in audio_paths:
        inputs += ["-i", str(audio)]

    mix_inputs = "".join(f"[{idx}:a:0]" for idx in range(1, 1 + len(audio_paths)))
    filter_complex = f"{mix_inputs}amix=inputs={len(audio_paths)}:duration=longest:dropout_transition=0[aout]"

    command = [
        *inputs,
        "-filter_complex",
        filter_complex,
        "-map",
        "0:v:0",
        "-map",
        "[aout]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        audio_bitrate,
        "-shortest",
        str(output_path),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise DriveCombineError(f"Failed to run ffmpeg: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
        raise DriveCombineError(f"ffmpeg mix failed: {detail}")

    if not output_path.exists():
        raise DriveCombineError(f"ffmpeg did not create output file: {output_path}")
    return output_path


def mix_folder_media_to_drive(
    drive_service: Resource,
    *,
    pkg_folder_id: str,
    output_parent_id: str,
    output_name: str,
    work_dir: Path,
    ffmpeg_path: str | None = None,
    dry_run: bool = False,
    video_type: str | None = None,
) -> DriveMediaFile:
    if dry_run:
        check = check_mixable_media(
            drive_service,
            pkg_folder_id,
            video_type=video_type,
        )
        if not check.ok:
            raise DriveCombineError(check.error or "Folder is not mixable")
        media = check.media
        if media is None:
            raise DriveCombineError("Folder is not mixable")
        return DriveMediaFile(
            id="dry-run",
            name=output_name,
            mime_type="video/mp4",
            parent_id=output_parent_id,
        )

    work_dir.mkdir(parents=True, exist_ok=True)

    inputs = find_video_and_audio_subfolder(
        drive_service,
        pkg_folder_id,
        video_type=video_type,
    )
    video_path = download_drive_file(
        drive_service,
        inputs.video.id,
        work_dir / _sanitize_local_filename(inputs.video.name),
    )

    audio_paths: list[Path] = []
    for audio in inputs.audios:
        audio_paths.append(
            download_drive_file(
                drive_service,
                audio.id,
                work_dir / _sanitize_local_filename(audio.name),
            )
        )

    output_path = work_dir / _sanitize_local_filename(output_name)
    combine_video_with_mixed_audios(
        video_path,
        audio_paths,
        output_path,
        ffmpeg_path=ffmpeg_path,
    )

    verify_drive_output_folder_access(drive_service, output_parent_id)
    return upload_drive_file(
        drive_service,
        output_parent_id,
        output_path,
        name=output_name,
        mime_type="video/mp4",
    )

