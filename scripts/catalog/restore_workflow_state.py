"""Restore catalog workflow-state from the previous successful daily run."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from catalog_parser.workflow.github_state import (
    MissingWorkflowStateArtifact,
    WorkflowStateError,
    restore_workflow_state,
    run_ids_from_artifact_list,
)

DEFAULT_WORKFLOW = "catalog-daily-workflow.yml"
DEFAULT_ARTIFACT = "workflow-state"
DEFAULT_LIST_LIMIT = 20

_GH_RUN_JSON_FIELDS = (
    "databaseId,createdAt,updatedAt,startedAt,url,number,displayTitle,conclusion"
)


def _run_gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    print(f"+ gh {' '.join(args)}", flush=True)
    return subprocess.run(
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _parse_json(stdout: str, *, what: str):
    try:
        return json.loads(stdout)
    except ValueError as exc:
        raise WorkflowStateError(f"{what} returned invalid JSON: {exc}") from exc


def _repo_slug() -> str:
    env = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if env:
        return env
    result = _run_gh(
        ["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise WorkflowStateError(f"gh repo view failed: {stderr}")
    slug = result.stdout.strip()
    if not slug or "/" not in slug:
        raise WorkflowStateError(f"Could not determine GitHub repository: {slug!r}")
    return slug


def _run_view_payload(run_id: int) -> dict:
    result = _run_gh(
        ["run", "view", str(run_id), "--json", _GH_RUN_JSON_FIELDS]
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise WorkflowStateError(f"gh run view failed for {run_id}: {stderr}")
    payload = _parse_json(result.stdout, what=f"gh run view {run_id}")
    if not isinstance(payload, dict):
        raise WorkflowStateError(f"gh run view {run_id} JSON must be an object")
    return payload


def list_runs_from_artifacts(*, artifact: str, limit: int) -> list[dict]:
    slug = _repo_slug()
    result = _run_gh(
        [
            "api",
            f"repos/{slug}/actions/artifacts?name={artifact}&per_page={max(limit, 30)}",
        ]
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise WorkflowStateError(f"gh api artifacts failed: {stderr}")
    payload = _parse_json(result.stdout, what="gh api artifacts")
    if not isinstance(payload, dict):
        raise WorkflowStateError("gh api artifacts JSON must be an object")
    run_ids = run_ids_from_artifact_list(payload, artifact_name=artifact)
    print(
        f"Artifact {artifact}: {len(run_ids)} unexpired run(s) "
        f"(loading up to {limit})",
        flush=True,
    )
    runs: list[dict] = []
    for run_id in run_ids:
        if len(runs) >= limit:
            break
        runs.append(_run_view_payload(run_id))
    return runs


def list_successful_runs(workflow: str, *, limit: int) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=4)).date().isoformat()
    result = _run_gh(
        [
            "run",
            "list",
            f"--workflow={workflow}",
            "--status=success",
            f"--created=>={since}",
            f"--limit={limit}",
            "--json",
            _GH_RUN_JSON_FIELDS,
        ]
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise WorkflowStateError(f"gh run list failed: {stderr}")
    payload = _parse_json(result.stdout, what="gh run list")
    if not isinstance(payload, list):
        raise WorkflowStateError("gh run list JSON must be an array of runs")
    return payload


def list_candidate_runs(
    workflow: str, *, limit: int, artifact: str
) -> list[dict]:
    try:
        from_artifacts = list_runs_from_artifacts(artifact=artifact, limit=limit)
    except WorkflowStateError as exc:
        print(
            f"WARNING: artifact listing failed ({exc}); falling back to gh run list",
            flush=True,
        )
        from_artifacts = []
    if from_artifacts:
        return from_artifacts
    print(
        "WARNING: no unexpired workflow-state artifacts listed; "
        "falling back to gh run list",
        flush=True,
    )
    return list_successful_runs(workflow, limit=limit)


def download_artifact(*, run_id: int, artifact: str, extract_dir: Path) -> None:
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    result = _run_gh(
        [
            "run",
            "download",
            str(run_id),
            "-n",
            artifact,
            "-D",
            str(extract_dir),
        ]
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        message = (
            f"gh run download failed for run {run_id} artifact {artifact}: {stderr}"
        )
        if "no valid artifacts found" in stderr.lower():
            raise MissingWorkflowStateArtifact(message)
        raise WorkflowStateError(message)
    if result.stdout.strip():
        print(result.stdout.rstrip(), flush=True)
    if result.stderr.strip():
        print(result.stderr.rstrip(), flush=True)


def _parse_run_id(value: str | None) -> int | None:
    if value is None or not str(value).strip():
        return None
    try:
        return int(str(value).strip())
    except ValueError as exc:
        raise WorkflowStateError(f"Invalid run id: {value!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Restore workflow-state from the newest unexpired GitHub artifact "
            "of that name, not from gh run list pagination. Ingest-only "
            "successes are skipped because they do not upload the artifact."
        )
    )
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    parser.add_argument(
        "--exclude-run-id",
        default="",
        help="GitHub Actions run id to skip (usually github.run_id)",
    )
    parser.add_argument("--artifact", default=DEFAULT_ARTIFACT)
    parser.add_argument(
        "--extract-dir",
        type=Path,
        default=Path("/tmp/workflow-state-restore"),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("output"),
    )
    parser.add_argument(
        "--required",
        action="append",
        default=[],
        help="Relative path that must exist in the artifact (repeatable)",
    )
    parser.add_argument(
        "--optional",
        action="append",
        default=[],
        help="Relative path copied when present (repeatable)",
    )
    parser.add_argument("--list-limit", type=int, default=DEFAULT_LIST_LIMIT)
    parser.add_argument(
        "--max-run-age-hours",
        type=float,
        default=None,
        help="Fail if the selected run is older than this many hours",
    )
    parser.add_argument(
        "--warn-run-age-hours",
        type=float,
        default=36,
        help="Log a warning if the selected run is older than this many hours",
    )
    parser.add_argument(
        "--backup-match-slack-hours",
        type=float,
        default=2,
        help="Allowed drift between backup fetched_at and the selected run",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    required = args.required or [
        "backups/airtable-latest.json",
        "workflow/status_history.json",
    ]
    optional = args.optional or [
        "workflow/editor_last_assigned.json",
        "backups/airtable-archive-titles.json",
    ]
    max_run_age = (
        timedelta(hours=args.max_run_age_hours)
        if args.max_run_age_hours is not None
        else None
    )
    try:
        restore_workflow_state(
            workflow=args.workflow,
            exclude_run_id=_parse_run_id(args.exclude_run_id),
            artifact=args.artifact,
            extract_dir=args.extract_dir,
            output_root=args.output_root,
            required=required,
            optional=optional,
            list_limit=args.list_limit,
            max_run_age=max_run_age,
            warn_run_age=timedelta(hours=args.warn_run_age_hours),
            backup_slack=timedelta(hours=args.backup_match_slack_hours),
            list_runs=lambda workflow, *, limit: list_candidate_runs(
                workflow, limit=limit, artifact=args.artifact
            ),
            download=download_artifact,
        )
    except WorkflowStateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
