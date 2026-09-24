"""Select and validate GitHub Actions workflow-state artifacts."""
from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BACKUP_RELATIVE = "backups/airtable-latest.json"


@dataclass(frozen=True)
class WorkflowRun:
    database_id: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime
    url: str
    number: int
    display_title: str
    conclusion: str


class WorkflowStateError(RuntimeError):
    """Restore cannot proceed with a trustworthy previous snapshot."""


class MissingWorkflowStateArtifact(WorkflowStateError):
    """The named artifact is not present on a successful run (e.g. ingest-only)."""


def parse_github_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowStateError(f"Missing GitHub timestamp: {value!r}")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def workflow_run_from_payload(payload: dict[str, Any]) -> WorkflowRun:
    database_id = payload.get("databaseId")
    if not isinstance(database_id, int):
        raise WorkflowStateError(f"Run is missing databaseId: {payload!r}")
    created_at = parse_github_datetime(payload.get("createdAt"))
    started_raw = payload.get("startedAt") or payload.get("createdAt")
    updated_raw = payload.get("updatedAt") or payload.get("createdAt")
    url = payload.get("url")
    number = payload.get("number")
    title = payload.get("displayTitle")
    conclusion = payload.get("conclusion")
    return WorkflowRun(
        database_id=database_id,
        created_at=created_at,
        started_at=parse_github_datetime(started_raw),
        updated_at=parse_github_datetime(updated_raw),
        url=url if isinstance(url, str) else "",
        number=number if isinstance(number, int) else 0,
        display_title=title if isinstance(title, str) else "",
        conclusion=conclusion if isinstance(conclusion, str) else "",
    )


def select_previous_successful_run(
    payloads: list[dict[str, Any]],
    *,
    exclude_run_id: int | None = None,
) -> WorkflowRun | None:
    """Return the newest successful run, excluding the current Actions run."""
    eligible = eligible_runs_newest_first(payloads, exclude_run_id=exclude_run_id)
    return eligible[0] if eligible else None


def eligible_runs_newest_first(
    payloads: list[dict[str, Any]],
    *,
    exclude_run_id: int | None = None,
) -> list[WorkflowRun]:
    runs = [workflow_run_from_payload(item) for item in payloads]
    eligible = [run for run in runs if run.database_id != exclude_run_id]
    eligible.sort(key=lambda run: (run.created_at, run.database_id), reverse=True)
    return eligible


def run_ids_from_artifact_list(
    payload: dict[str, Any],
    *,
    artifact_name: str,
    exclude_run_id: int | None = None,
) -> list[int]:
    """Newest unique workflow run ids that uploaded an unexpired named artifact."""
    items = payload.get("artifacts")
    if not isinstance(items, list):
        raise WorkflowStateError("Artifact list JSON must contain an artifacts array")
    ranked: list[tuple[datetime, int]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("name") != artifact_name:
            continue
        if item.get("expired") is True:
            continue
        workflow_run = item.get("workflow_run")
        run_id = workflow_run.get("id") if isinstance(workflow_run, dict) else None
        if not isinstance(run_id, int) or run_id == exclude_run_id:
            continue
        ranked.append((parse_github_datetime(item.get("created_at")), run_id))
    ranked.sort(key=lambda pair: (pair[0], pair[1]), reverse=True)
    seen: list[int] = []
    for _, run_id in ranked:
        if run_id not in seen:
            seen.append(run_id)
    return seen


def format_run_log_line(run: WorkflowRun) -> str:
    title = run.display_title or "(no title)"
    url = f"  {run.url}" if run.url else ""
    return (
        f"{run.database_id}  created={run.created_at.isoformat()}  "
        f"#{run.number}  {title}{url}"
    )


def backup_fetched_at_matches_run(
    fetched_at: datetime,
    run: WorkflowRun,
    *,
    slack: timedelta = timedelta(hours=2),
) -> bool:
    """True when the Airtable snapshot was taken during the selected run."""
    fetched = fetched_at.astimezone(timezone.utc)
    start = min(run.started_at, run.created_at) - slack
    end = max(run.updated_at, run.started_at, run.created_at) + slack
    return start <= fetched <= end


def run_is_within_max_age(
    run: WorkflowRun,
    *,
    now: datetime,
    max_age: timedelta,
) -> bool:
    return now.astimezone(timezone.utc) - run.created_at <= max_age


def find_restored_file(extract_dir: Path, relative: str) -> Path | None:
    for candidate in (
        extract_dir / relative,
        extract_dir / "output" / relative,
    ):
        if candidate.is_file():
            return candidate
    return None


def read_backup_fetched_at(path: Path) -> datetime:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise WorkflowStateError(f"Backup {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowStateError(f"Backup {path} must contain a JSON object")
    return parse_github_datetime(data.get("fetched_at"))


def copy_restored_files(
    *,
    extract_dir: Path,
    output_root: Path,
    required: list[str],
    optional: list[str],
    log: Callable[[str], None] = print,
) -> dict[str, Path]:
    copied: dict[str, Path] = {}
    missing_required: list[str] = []
    for relative in required:
        source = find_restored_file(extract_dir, relative)
        if source is None:
            missing_required.append(relative)
            continue
        dest = output_root / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        copied[relative] = dest
        log(f"Restored {dest} from {source}")
    if missing_required:
        raise WorkflowStateError(
            "Required workflow-state file(s) missing from artifact: "
            + ", ".join(missing_required)
        )
    for relative in optional:
        source = find_restored_file(extract_dir, relative)
        if source is None:
            log(f"Optional artifact file not present: {relative}")
            continue
        dest = output_root / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        copied[relative] = dest
        log(f"Restored {dest} from {source}")
    return copied


def validate_selected_run(
    run: WorkflowRun,
    *,
    now: datetime,
    max_run_age: timedelta | None,
    warn_run_age: timedelta,
    log: Callable[[str], None] = print,
) -> None:
    age = now.astimezone(timezone.utc) - run.created_at
    log(f"Selected run age: {age}")
    if age > warn_run_age:
        log(
            f"WARNING: selected run is older than {warn_run_age} "
            f"(created {run.created_at.isoformat()})"
        )
    if max_run_age is not None and not run_is_within_max_age(
        run, now=now, max_age=max_run_age
    ):
        raise WorkflowStateError(
            f"Selected run {run.database_id} is older than {max_run_age} "
            f"(created {run.created_at.isoformat()}). Refusing to restore a "
            "stale snapshot."
        )


def validate_backup_matches_run(
    backup_path: Path,
    run: WorkflowRun,
    *,
    slack: timedelta,
    log: Callable[[str], None] = print,
) -> None:
    fetched_at = read_backup_fetched_at(backup_path)
    log(f"Backup fetched_at={fetched_at.isoformat()}")
    if backup_fetched_at_matches_run(fetched_at, run, slack=slack):
        log("Backup fetched_at matches selected run.")
        return
    raise WorkflowStateError(
        f"Backup fetched_at {fetched_at.isoformat()} does not match selected "
        f"run {run.database_id} "
        f"(created {run.created_at.isoformat()}, "
        f"started {run.started_at.isoformat()}, "
        f"updated {run.updated_at.isoformat()}). "
        "Refusing to use a snapshot from a different run."
    )


def restore_workflow_state(
    *,
    workflow: str,
    exclude_run_id: int | None,
    artifact: str,
    extract_dir: Path,
    output_root: Path,
    required: list[str],
    optional: list[str],
    list_limit: int,
    max_run_age: timedelta | None,
    warn_run_age: timedelta,
    backup_slack: timedelta,
    list_runs: Callable[..., list[dict[str, Any]]],
    download: Callable[..., None],
    now: datetime | None = None,
    log: Callable[[str], None] = print,
) -> WorkflowRun:
    now = now or datetime.now(timezone.utc)
    log(
        f"Looking for previous successful {workflow} run "
        f"(exclude run_id={exclude_run_id if exclude_run_id is not None else 'none'})"
    )
    payloads = list_runs(workflow, limit=list_limit)
    eligible = eligible_runs_newest_first(
        payloads, exclude_run_id=exclude_run_id
    )
    if not eligible:
        raise WorkflowStateError(
            f"No successful {workflow} run found to restore from "
            f"(exclude run_id={exclude_run_id})."
        )

    log("Eligible successes (newest first by createdAt):")
    for index, run in enumerate(eligible[:10], start=1):
        log(f"  {index}. {format_run_log_line(run)}")
    if exclude_run_id is not None and any(
        item.get("databaseId") == exclude_run_id for item in payloads
    ):
        log(f"Skipped current run_id={exclude_run_id} in gh run list results")

    skipped_missing: list[int] = []
    skipped_too_old: list[int] = []
    selected = None
    for run in eligible:
        if max_run_age is not None and not run_is_within_max_age(
            run, now=now, max_age=max_run_age
        ):
            skipped_too_old.append(run.database_id)
            log(
                f"Skipping run {run.database_id}: older than {max_run_age} "
                f"(created {run.created_at.isoformat()})"
            )
            continue
        log(f"Trying: {format_run_log_line(run)}")
        validate_selected_run(
            run,
            now=now,
            max_run_age=None,
            warn_run_age=warn_run_age,
            log=log,
        )
        log(f"Downloading artifact {artifact} from run {run.database_id}")
        try:
            download(run_id=run.database_id, artifact=artifact, extract_dir=extract_dir)
        except MissingWorkflowStateArtifact as exc:
            skipped_missing.append(run.database_id)
            log(f"Skipping run {run.database_id}: {exc}")
            continue
        selected = run
        break

    if selected is None:
        reasons: list[str] = []
        if skipped_missing:
            reasons.append(f"no {artifact} artifact: {skipped_missing}")
        if skipped_too_old:
            reasons.append(f"older than {max_run_age}: {skipped_too_old}")
        detail = f" ({'; '.join(reasons)})" if reasons else ""
        raise WorkflowStateError(
            f"No successful {workflow} run with artifact {artifact} found "
            f"(exclude run_id={exclude_run_id}).{detail}"
        )

    log(f"Selected: {format_run_log_line(selected)}")
    backup_source = find_restored_file(extract_dir, BACKUP_RELATIVE)
    if backup_source is not None:
        validate_backup_matches_run(
            backup_source, selected, slack=backup_slack, log=log
        )
    elif BACKUP_RELATIVE in required:
        raise WorkflowStateError(
            f"Required workflow-state file missing from artifact: {BACKUP_RELATIVE}"
        )
    copy_restored_files(
        extract_dir=extract_dir,
        output_root=output_root,
        required=required,
        optional=optional,
        log=log,
    )
    return selected

