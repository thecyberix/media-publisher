from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator

from media_publisher.models import PublishJob

DEFAULT_API_BASE = "https://www.happyscribe.com/api/v1"
DEFAULT_USER_AGENT = "media-publisher/0.1"
DEFAULT_FFMPEG = "ffmpeg"
EXPORT_POLL_INTERVAL_SECONDS = 1.0
EXPORT_POLL_MAX_ATTEMPTS = 60
TRANSCRIPTION_STATE_READY = "automatic_done"
METADATA_TRANSCRIPTION_ID = "happyscribe_transcription_id"
LIBRARY_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?happyscribe\.com/v2/(?P<organization_id>\d+)/library/(?P<folder_id>\d+)"
)


class HappyScribeError(RuntimeError):
    pass


@dataclass(frozen=True)
class HappyScribeLibraryLocation:
    organization_id: str
    folder_id: str


@dataclass(frozen=True)
class HappyScribeOrganization:
    id: str
    name: str
    role: str | None = None


@dataclass(frozen=True)
class HappyScribeTranscription:
    id: str
    name: str
    state: str
    video_url: str | None = None
    audio_url: str | None = None
    language: str | None = None
    folder_id: str | None = None
    folder_name: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class HappyScribeExport:
    id: str
    state: str
    format: str
    download_link: str | None = None


def _field_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    text = value.strip()
    return text or None


def _parse_transcription(payload: dict[str, Any]) -> HappyScribeTranscription:
    transcription_id = payload.get("id")
    if not isinstance(transcription_id, str):
        raise HappyScribeError("HappyScribe response is missing transcription id")

    name = _field_text(payload.get("name")) or transcription_id
    state = _field_text(payload.get("state")) or "unknown"
    tags = payload.get("tags", [])
    if not isinstance(tags, list):
        tags = []

    folder = payload.get("folder")
    folder_id: str | None = None
    folder_name: str | None = None
    if isinstance(folder, dict):
        raw_folder_id = folder.get("id")
        if raw_folder_id is not None:
            folder_id = str(raw_folder_id)
        folder_name = _field_text(folder.get("name"))

    return HappyScribeTranscription(
        id=transcription_id,
        name=name,
        state=state,
        video_url=_field_text(payload.get("videoUrl")),
        audio_url=_field_text(payload.get("audioUrl")),
        language=_field_text(payload.get("language")),
        folder_id=folder_id,
        folder_name=folder_name,
        tags=tuple(str(tag) for tag in tags),
    )


def _parse_export(payload: dict[str, Any]) -> HappyScribeExport:
    export_id = payload.get("id")
    if not isinstance(export_id, str):
        raise HappyScribeError("HappyScribe response is missing export id")

    state = _field_text(payload.get("state")) or "unknown"
    export_format = _field_text(payload.get("format")) or "unknown"
    return HappyScribeExport(
        id=export_id,
        state=state,
        format=export_format,
        download_link=_field_text(payload.get("download_link")),
    )


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", name).strip(" .")
    return cleaned or "video"


def parse_library_url(url: str) -> HappyScribeLibraryLocation:
    match = LIBRARY_URL_PATTERN.search(url.strip())
    if not match:
        raise HappyScribeError(
            "Invalid HappyScribe library URL. Expected format: "
            "https://www.happyscribe.com/v2/<organization_id>/library/<folder_id>"
        )
    return HappyScribeLibraryLocation(
        organization_id=match.group("organization_id"),
        folder_id=match.group("folder_id"),
    )


def resolve_library_location(
    *,
    library_url: str | None = None,
    organization_id: str | None = None,
    folder_id: str | None = None,
) -> HappyScribeLibraryLocation:
    if library_url:
        return parse_library_url(library_url)
    if organization_id and folder_id:
        return HappyScribeLibraryLocation(
            organization_id=organization_id.strip(),
            folder_id=folder_id.strip(),
        )
    raise HappyScribeError(
        "HappyScribe library location is required. Set HAPPYSCRIBE_LIBRARY_URL or "
        "both HAPPYSCRIBE_ORGANIZATION_ID and HAPPYSCRIBE_FOLDER_ID."
    )


def video_destination_path(
    download_dir: Path,
    transcription_name: str,
    *,
    suffix: str | None = None,
) -> Path:
    stem = _safe_filename(transcription_name)
    if stem.lower().endswith(".srt"):
        stem = stem[:-4]
    if suffix:
        stem = f"{stem}{suffix}"
    if not stem.lower().endswith(".mp4"):
        stem = f"{stem}.mp4"
    return download_dir / stem


def is_subtitled_export_name(name: str) -> bool:
    return name.strip().lower().endswith(".srt")


def subtitled_export_name(source_name: str) -> str:
    name = source_name.strip()
    if is_subtitled_export_name(name):
        return name
    return f"{name}.srt"


def find_transcription_by_name(
    transcriptions: list[HappyScribeTranscription],
    name: str,
) -> HappyScribeTranscription | None:
    target = name.strip().casefold()
    for transcription in transcriptions:
        if transcription.name.strip().casefold() == target:
            return transcription
    return None


def subtitle_destination_path(download_dir: Path, transcription_name: str) -> Path:
    stem = _safe_filename(transcription_name)
    if stem.lower().endswith(".srt"):
        return download_dir / f"{stem}"
    return download_dir / f"{stem}.srt"


def burned_video_destination_path(download_dir: Path, transcription_name: str) -> Path:
    return video_destination_path(download_dir, transcription_name, suffix="-subtitled")


def resolve_ffmpeg_path(ffmpeg_path: str | None = None) -> str:
    if ffmpeg_path:
        candidate = Path(ffmpeg_path)
        if candidate.exists():
            return str(candidate)
        found = shutil.which(ffmpeg_path)
        if found:
            return found
        raise HappyScribeError(f"ffmpeg not found at {ffmpeg_path!r}")

    found = shutil.which(DEFAULT_FFMPEG)
    if found:
        return found
    raise HappyScribeError(
        "ffmpeg is required to burn subtitles but was not found on PATH. "
        "Install ffmpeg or set HAPPYSCRIBE_FFMPEG."
    )



def burn_subtitles_into_video(
    video_path: Path,
    subtitle_path: Path,
    destination: Path,
    *,
    ffmpeg_path: str | None = None,
) -> Path:
    ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    work_dir = destination.parent
    safe_subtitle_path = work_dir / "subtitles.burn.srt"
    shutil.copy(subtitle_path, safe_subtitle_path)

    command = [
        ffmpeg,
        "-y",
        "-i",
        str(video_path.resolve()),
        "-vf",
        "subtitles=subtitles.burn.srt",
        "-c:a",
        "copy",
        str(destination.resolve()),
    ]

    try:
        result = subprocess.run(
            command,
            cwd=work_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    finally:
        safe_subtitle_path.unlink(missing_ok=True)

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise HappyScribeError(f"ffmpeg subtitle burn failed: {detail}")

    return destination


def resolve_subtitled_transcription(
    transcriptions: list[HappyScribeTranscription],
    transcription: HappyScribeTranscription,
) -> HappyScribeTranscription:
    if is_subtitled_export_name(transcription.name):
        return transcription

    exported_name = subtitled_export_name(transcription.name)
    exported = find_transcription_by_name(transcriptions, exported_name)
    if exported is not None:
        return exported

    raise HappyScribeError(
        f"No HappyScribe subtitled export found for {transcription.name!r}. "
        f"Expected a library item named {exported_name!r}."
    )


class HappyScribeClient:
    def __init__(
        self,
        api_key: str,
        *,
        api_base: str = DEFAULT_API_BASE,
        organization_id: str | None = None,
        ffmpeg_path: str | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.api_base = api_base.rstrip("/")
        self.organization_id = organization_id.strip() if organization_id else None
        self.ffmpeg_path = ffmpeg_path.strip() if ffmpeg_path else None
        if not self.api_key:
            raise HappyScribeError("HAPPYSCRIBE_API_KEY is required")

    def _request(
        self,
        method: str,
        url: str,
        *,
        query: dict[str, str | int] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        if query:
            encoded = urllib.parse.urlencode(
                {key: str(value) for key, value in query.items()},
                doseq=True,
            )
            url = f"{url}?{encoded}"

        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.api_key}")
        request.add_header("Content-Type", "application/json")
        request.add_header("User-Agent", DEFAULT_USER_AGENT)

        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise HappyScribeError(
                f"HappyScribe {method} {url} failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise HappyScribeError(f"HappyScribe request failed: {exc.reason}") from exc

        if not payload:
            return {}
        return json.loads(payload.decode("utf-8"))

    def _url(self, path: str) -> str:
        return f"{self.api_base}/{path.lstrip('/')}"

    def list_organizations(self) -> list[HappyScribeOrganization]:
        response = self._request("GET", self._url("organizations"))
        organizations = response.get("organizations", [])
        if not isinstance(organizations, list):
            raise HappyScribeError("HappyScribe organizations response is invalid")

        parsed: list[HappyScribeOrganization] = []
        for item in organizations:
            if not isinstance(item, dict):
                continue
            org_id = item.get("id")
            name = _field_text(item.get("name"))
            if org_id is None or not name:
                continue
            parsed.append(
                HappyScribeOrganization(
                    id=str(org_id),
                    name=name,
                    role=_field_text(item.get("role")),
                )
            )
        return parsed

    def resolve_organization_id(self) -> str:
        if self.organization_id:
            return self.organization_id

        organizations = self.list_organizations()
        if not organizations:
            raise HappyScribeError(
                "No HappyScribe organizations found for this API key"
            )
        return organizations[0].id

    def iter_transcriptions(
        self,
        *,
        organization_id: str | None = None,
        folder_id: str | None = None,
        per_page: int = 100,
        tags: list[str] | None = None,
    ) -> Iterator[HappyScribeTranscription]:
        org_id = organization_id or self.resolve_organization_id()
        page = 0

        while True:
            query: dict[str, str | int] = {
                "organization_id": org_id,
                "page": page,
                "per_page": per_page,
            }
            if folder_id:
                query["folder_id"] = folder_id
            if tags:
                query["tags"] = ",".join(tags)

            response = self._request("GET", self._url("transcriptions"), query=query)
            results = response.get("results", [])
            if not isinstance(results, list):
                raise HappyScribeError("HappyScribe transcriptions response is invalid")

            for item in results:
                if isinstance(item, dict):
                    yield _parse_transcription(item)

            links = response.get("_links", {})
            if not isinstance(links, dict) or "next" not in links:
                break
            page += 1

    def list_transcriptions(
        self,
        *,
        organization_id: str | None = None,
        folder_id: str | None = None,
        per_page: int = 100,
        tags: list[str] | None = None,
    ) -> list[HappyScribeTranscription]:
        return list(
            self.iter_transcriptions(
                organization_id=organization_id,
                folder_id=folder_id,
                per_page=per_page,
                tags=tags,
            )
        )

    def list_library_transcriptions(
        self,
        location: HappyScribeLibraryLocation,
    ) -> list[HappyScribeTranscription]:
        return self.list_transcriptions(
            organization_id=location.organization_id,
            folder_id=location.folder_id,
        )

    def get_transcription(self, transcription_id: str) -> HappyScribeTranscription:
        response = self._request("GET", self._url(f"transcriptions/{transcription_id}"))
        if not isinstance(response, dict):
            raise HappyScribeError("HappyScribe transcription response is invalid")
        return _parse_transcription(response)

    def create_export(
        self,
        transcription_id: str,
        *,
        export_format: str,
    ) -> HappyScribeExport:
        response = self._request(
            "POST",
            self._url("exports"),
            body={
                "export": {
                    "format": export_format,
                    "transcription_ids": [transcription_id],
                }
            },
        )
        if not isinstance(response, dict):
            raise HappyScribeError("HappyScribe export response is invalid")
        return _parse_export(response)

    def get_export(self, export_id: str) -> HappyScribeExport:
        response = self._request("GET", self._url(f"exports/{export_id}"))
        if not isinstance(response, dict):
            raise HappyScribeError("HappyScribe export response is invalid")
        return _parse_export(response)

    def wait_for_export(self, export_id: str) -> HappyScribeExport:
        for _ in range(EXPORT_POLL_MAX_ATTEMPTS):
            export = self.get_export(export_id)
            if export.state == "ready" and export.download_link:
                return export
            if export.state == "failed":
                raise HappyScribeError(
                    f"HappyScribe export {export_id!r} failed (state={export.state!r})"
                )
            time.sleep(EXPORT_POLL_INTERVAL_SECONDS)

        raise HappyScribeError(
            f"HappyScribe export {export_id!r} did not become ready in time"
        )

    def get_subtitle_download_url(
        self,
        transcription_id: str,
        *,
        export_format: str = "srt",
    ) -> str:
        export = self.create_export(transcription_id, export_format=export_format)
        if export.download_link and export.state == "ready":
            return export.download_link
        export = self.wait_for_export(export.id)
        if not export.download_link:
            raise HappyScribeError(
                f"HappyScribe {export_format} export {export.id!r} did not return a download link"
            )
        return export.download_link

    def download_subtitles(
        self,
        transcription_id: str,
        destination: Path,
        *,
        export_format: str = "srt",
    ) -> Path:
        download_url = self.get_subtitle_download_url(
            transcription_id,
            export_format=export_format,
        )
        return self.download_file(download_url, destination)

    def get_video_download_url(self, transcription_id: str) -> str:
        transcription = self.get_transcription(transcription_id)
        if transcription.state != TRANSCRIPTION_STATE_READY:
            raise HappyScribeError(
                f"Transcription {transcription_id!r} is not ready "
                f"(state={transcription.state!r})"
            )

        if transcription.video_url:
            return transcription.video_url

        export = self.create_export(transcription_id, export_format="mp4")
        if export.download_link:
            return export.download_link

        if export.state != "ready":
            raise HappyScribeError(
                f"HappyScribe mp4 export {export.id!r} is not ready "
                f"(state={export.state!r})"
            )
        raise HappyScribeError(
            f"HappyScribe mp4 export {export.id!r} did not return a download link"
        )

    def download_file(self, url: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, method="GET")
        request.add_header("User-Agent", DEFAULT_USER_AGENT)

        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                destination.write_bytes(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise HappyScribeError(
                f"Download failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise HappyScribeError(f"Download failed: {exc.reason}") from exc

        return destination

    def download_video(
        self,
        transcription_id: str,
        destination: Path,
    ) -> Path:
        download_url = self.get_video_download_url(transcription_id)
        return self.download_file(download_url, destination)

    def download_video_with_burned_subtitles(
        self,
        transcription_id: str,
        destination: Path,
        *,
        work_dir: Path | None = None,
        keep_sources: bool = False,
    ) -> Path:
        transcription = self.get_transcription(transcription_id)
        if transcription.state != TRANSCRIPTION_STATE_READY:
            raise HappyScribeError(
                f"Transcription {transcription_id!r} is not ready "
                f"(state={transcription.state!r})"
            )

        work = work_dir or destination.parent / ".work"
        work.mkdir(parents=True, exist_ok=True)
        stem = _safe_filename(transcription.name)
        if stem.lower().endswith(".srt"):
            stem = stem[:-4]

        source_video = work / f"{stem}.source.mp4"
        subtitle_file = work / f"{stem}.srt"

        self.download_video(transcription_id, source_video)
        self.download_subtitles(transcription_id, subtitle_file)
        burn_subtitles_into_video(
            source_video,
            subtitle_file,
            destination,
            ffmpeg_path=self.ffmpeg_path,
        )

        if not keep_sources:
            source_video.unlink(missing_ok=True)
            subtitle_file.unlink(missing_ok=True)

        return destination

    def resolve_subtitled_transcription_for_id(
        self,
        transcription_id: str,
        *,
        location: HappyScribeLibraryLocation | None = None,
        transcriptions: list[HappyScribeTranscription] | None = None,
    ) -> HappyScribeTranscription:
        transcription = self.get_transcription(transcription_id)
        if transcriptions is None:
            if location is None:
                raise HappyScribeError(
                    "HappyScribe library location is required to resolve subtitled exports"
                )
            transcriptions = self.list_library_transcriptions(location)
        return resolve_subtitled_transcription(transcriptions, transcription)

    def download_subtitled_video(
        self,
        transcription_id: str,
        destination: Path,
        *,
        location: HappyScribeLibraryLocation | None = None,
        transcriptions: list[HappyScribeTranscription] | None = None,
    ) -> tuple[Path, HappyScribeTranscription]:
        subtitled = self.resolve_subtitled_transcription_for_id(
            transcription_id,
            location=location,
            transcriptions=transcriptions,
        )
        path = self.download_video(subtitled.id, destination)
        return path, subtitled

    def download_library_videos(
        self,
        location: HappyScribeLibraryLocation,
        download_dir: Path,
        *,
        only_ready: bool = True,
        burn_subtitles: bool = True,
    ) -> list[Path]:
        transcriptions = self.list_library_transcriptions(location)
        downloaded: list[Path] = []
        for transcription in transcriptions:
            if only_ready and transcription.state != TRANSCRIPTION_STATE_READY:
                continue
            if is_subtitled_export_name(transcription.name):
                continue
            if burn_subtitles:
                destination = burned_video_destination_path(
                    download_dir,
                    transcription.name,
                )
                downloaded.append(
                    self.download_video_with_burned_subtitles(
                        transcription.id,
                        destination,
                        work_dir=download_dir / ".work",
                    )
                )
            else:
                destination = video_destination_path(download_dir, transcription.name)
                downloaded.append(self.download_video(transcription.id, destination))
        return downloaded

    def test_connection(
        self,
        location: HappyScribeLibraryLocation | None = None,
    ) -> tuple[str, int]:
        if location is not None:
            count = len(
                self.list_transcriptions(
                    organization_id=location.organization_id,
                    folder_id=location.folder_id,
                    per_page=1,
                )
            )
            return location.organization_id, count

        org_id = self.resolve_organization_id()
        count = len(self.list_transcriptions(organization_id=org_id, per_page=1))
        return org_id, count


def transcription_id_from_job(job: PublishJob) -> str | None:
    return _field_text(job.metadata.get(METADATA_TRANSCRIPTION_ID))


def enrich_job_from_happyscribe(
    job: PublishJob,
    *,
    api_key: str,
    download_dir: Path,
    organization_id: str | None = None,
    api_base: str = DEFAULT_API_BASE,
    transcription_id: str | None = None,
    ffmpeg_path: str | None = None,
    browser_state_path: Path | None = None,
    burn_subtitles: bool = False,
) -> PublishJob:
    """Download a HappyScribe video for a publish job."""
    transcription_id = transcription_id or transcription_id_from_job(job)
    if not transcription_id:
        raise HappyScribeError(
            f"Publish job is missing {METADATA_TRANSCRIPTION_ID!r} metadata"
        )

    if browser_state_path is not None:
        from media_publisher.sources.happyscribe_web import export_video_for_transcription_name

        client = HappyScribeClient(
            api_key,
            api_base=api_base,
            organization_id=organization_id,
        )
        transcription = client.get_transcription(transcription_id)
        destination = export_video_for_transcription_name(
            transcription_id,
            transcription.name,
            download_dir,
            browser_state_path=browser_state_path,
        )
        metadata = dict(job.metadata)
        metadata[METADATA_TRANSCRIPTION_ID] = transcription_id
        metadata["happyscribe_state"] = transcription.state
        metadata["happyscribe_subtitled"] = "True"
        metadata["happyscribe_export"] = "web"
        return replace(job, video_path=str(destination), metadata=metadata)

    client = HappyScribeClient(
        api_key,
        api_base=api_base,
        organization_id=organization_id,
        ffmpeg_path=ffmpeg_path,
    )
    transcription = client.get_transcription(transcription_id)
    if burn_subtitles:
        destination = burned_video_destination_path(download_dir, transcription.name)
        client.download_video_with_burned_subtitles(
            transcription_id,
            destination,
            work_dir=download_dir / ".work",
        )
        export_mode = "ffmpeg"
        subtitled = "True"
    else:
        destination = video_destination_path(download_dir, transcription.name)
        client.download_video(transcription_id, destination)
        export_mode = "source"
        subtitled = "False"

    metadata = dict(job.metadata)
    metadata[METADATA_TRANSCRIPTION_ID] = transcription_id
    metadata["happyscribe_state"] = transcription.state
    metadata["happyscribe_subtitled"] = subtitled
    metadata["happyscribe_export"] = export_mode

    return replace(
        job,
        video_path=str(destination),
        metadata=metadata,
    )
