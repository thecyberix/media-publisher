"""Run GitHub Actions workflow_dispatch presets and optionally wait for results.

Configuration: config/github_workflows.json (copy from github_workflows.example.json).

Environment:
  GITHUB_DISPATCH_TOKEN — PAT with Actions: Read and write (name overridable via token_env)

Examples:
  python scripts/run_github_workflow.py --list
  python scripts/run_github_workflow.py publish-private-videos
  python scripts/run_github_workflow.py publish-private-videos --no-wait
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from media_publisher.runtime_env import github_repository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_CANDIDATES = (
    PROJECT_ROOT / "config" / "github_workflows.json",
    PROJECT_ROOT / "config" / "github_workflows.example.json",
)


class GitHubActionsError(RuntimeError):
    pass


def load_workflow_config(config_path: Path | None = None) -> dict[str, Any]:
    if config_path is not None:
        path = config_path
        if not path.is_file():
            raise FileNotFoundError(f"Workflow config not found: {path}")
    else:
        path = next((candidate for candidate in DEFAULT_CONFIG_CANDIDATES if candidate.is_file()), None)
        if path is None:
            raise FileNotFoundError(
                "No workflow config found. Copy config/github_workflows.example.json "
                "to config/github_workflows.json"
            )
    return json.loads(path.read_text(encoding="utf-8"))


def github_request(
    url: str,
    *,
    token: str,
    api_version: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
) -> Any:
    payload = None if data is None else json.dumps(data).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", api_version)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read()
            if not body:
                return None
            return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubActionsError(f"GitHub API {exc.code} for {url}: {detail}") from exc


def dispatch_workflow(
    *,
    repo: str,
    workflow_file: str,
    ref: str,
    token: str,
    api_version: str,
    inputs: dict[str, str],
) -> None:
    url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/"
        f"{workflow_file}/dispatches"
    )
    github_request(
        url,
        token=token,
        api_version=api_version,
        method="POST",
        data={"ref": ref, "inputs": inputs},
    )


def _parse_github_time(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def find_run_after_dispatch(
    *,
    repo: str,
    workflow_file: str,
    ref: str,
    token: str,
    api_version: str,
    dispatched_after: datetime,
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> dict[str, Any]:
    url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/"
        f"{workflow_file}/runs?branch={ref}&event=workflow_dispatch&per_page=10"
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        payload = github_request(url, token=token, api_version=api_version)
        runs = payload.get("workflow_runs", []) if isinstance(payload, dict) else []
        for run in runs:
            created_at = run.get("created_at")
            if not isinstance(created_at, str):
                continue
            if _parse_github_time(created_at) >= dispatched_after:
                return run
        time.sleep(poll_interval_seconds)
    raise GitHubActionsError(
        f"Timed out after {timeout_seconds}s waiting for workflow run to appear"
    )


def wait_for_run_completion(
    *,
    repo: str,
    run_id: int,
    token: str,
    api_version: str,
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        run = github_request(url, token=token, api_version=api_version)
        status = run.get("status")
        if status == "completed":
            return run
        time.sleep(poll_interval_seconds)
    raise GitHubActionsError(
        f"Timed out after {timeout_seconds}s waiting for run {run_id} to complete"
    )


def list_run_jobs(
    *,
    repo: str,
    run_id: int,
    token: str,
    api_version: str,
) -> list[dict[str, Any]]:
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"
    payload = github_request(url, token=token, api_version=api_version)
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    return jobs if isinstance(jobs, list) else []


def download_job_log_text(
    *,
    repo: str,
    job_id: int,
    token: str,
    api_version: str,
) -> str:
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    url = f"https://api.github.com/repos/{repo}/actions/jobs/{job_id}/logs"
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", api_version)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=120) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code != 302:
            detail = exc.read().decode("utf-8", errors="replace")
            return f"<failed to download logs: HTTP {exc.code} {detail}>"
        location = exc.headers.get("Location")
        if not location:
            return "<failed to download logs: missing redirect location>"
        with urllib.request.urlopen(location, timeout=120) as response:
            payload = response.read()
    except urllib.error.URLError as exc:
        return f"<failed to download logs: {exc}>"

    if payload[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                parts: list[str] = []
                for name in sorted(archive.namelist()):
                    if not name.endswith(".txt"):
                        continue
                    text = archive.read(name).decode("utf-8", errors="replace")
                    parts.append(f"--- {name} ---\n{text}")
                return "\n".join(parts)
        except zipfile.BadZipFile:
            pass
    return payload.decode("utf-8", errors="replace")


def tail_lines(text: str, max_lines: int) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[-max_lines:])


def resolve_token(config: dict[str, Any], override: str | None) -> str:
    if override and override.strip():
        return override.strip()
    env_name = str(config.get("token_env", "GITHUB_DISPATCH_TOKEN"))
    token = os.getenv(env_name, "").strip()
    if not token:
        raise GitHubActionsError(
            f"Missing GitHub token. Set {env_name} or pass --token."
        )
    return token


def _stringify_workflow_input(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def resolve_preset(config: dict[str, Any], preset_name: str) -> dict[str, Any]:
    presets = config.get("presets", {})
    if preset_name not in presets:
        available = ", ".join(sorted(presets))
        raise KeyError(f"Unknown preset {preset_name!r}. Available: {available}")
    preset = presets[preset_name]
    workflow = preset.get("workflow")
    if not isinstance(workflow, str) or not workflow.strip():
        raise ValueError(f"Preset {preset_name!r} is missing workflow")
    inputs = preset.get("inputs", {})
    if not isinstance(inputs, dict):
        raise ValueError(f"Preset {preset_name!r} inputs must be an object")
    normalized_inputs = {
        str(key): _stringify_workflow_input(value) for key, value in inputs.items()
    }
    return {
        "name": preset_name,
        "description": preset.get("description", ""),
        "workflow": workflow,
        "inputs": normalized_inputs,
    }


def print_preset_list(config: dict[str, Any]) -> None:
    presets = config.get("presets", {})
    print("Presets:")
    for name in sorted(presets):
        preset = presets[name]
        description = preset.get("description", "")
        workflow = preset.get("workflow", "?")
        inputs = preset.get("inputs", {})
        print(f"  {name}")
        if description:
            print(f"    {description}")
        print(f"    workflow={workflow} inputs={json.dumps(inputs, ensure_ascii=False)}")
    cron_jobs = config.get("cron_jobs", {})
    if cron_jobs:
        print("\nCron job mirrors (reference only):")
        for name, job in sorted(cron_jobs.items()):
            schedule = job.get("schedule", "?")
            timezone_name = job.get("timezone", "UTC")
            preset = job.get("preset", "?")
            print(f"  {name}: {schedule} ({timezone_name}) -> preset {preset}")


def run_workflow(
    *,
    config: dict[str, Any],
    preset: dict[str, Any],
    token: str,
    wait: bool,
    log_failed_jobs: bool,
    log_tail_lines: int,
) -> int:
    defaults = config.get("defaults", {})
    repo = str(config.get("repository", "")).strip() or (github_repository() or "")
    ref = str(config.get("ref", "master")).strip()
    api_version = str(config.get("api_version", "2022-11-28"))
    if not repo:
        raise ValueError(
            "Set repository in workflow config, GITHUB_REPOSITORY, or git remote origin"
        )

    timeout_seconds = int(defaults.get("wait_timeout_seconds", 2700))
    poll_interval_seconds = int(defaults.get("poll_interval_seconds", 15))

    dispatched_after = datetime.now(timezone.utc)
    dispatch_workflow(
        repo=repo,
        workflow_file=preset["workflow"],
        ref=ref,
        token=token,
        api_version=api_version,
        inputs=preset["inputs"],
    )
    print(
        f"Triggered preset {preset['name']!r} "
        f"({preset['workflow']} on {repo}@{ref}) "
        f"inputs={preset['inputs']}"
    )
    if not wait:
        print("Not waiting for completion (--no-wait).")
        return 0

    run = find_run_after_dispatch(
        repo=repo,
        workflow_file=preset["workflow"],
        ref=ref,
        token=token,
        api_version=api_version,
        dispatched_after=dispatched_after,
        timeout_seconds=min(timeout_seconds, 120),
        poll_interval_seconds=poll_interval_seconds,
    )
    run_id = int(run["id"])
    run_url = run.get("html_url", "")
    print(f"Run #{run_id}: {run_url}")

    completed = wait_for_run_completion(
        repo=repo,
        run_id=run_id,
        token=token,
        api_version=api_version,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    conclusion = completed.get("conclusion", "unknown")
    print(f"Completed with conclusion: {conclusion}")

    jobs = list_run_jobs(
        repo=repo,
        run_id=run_id,
        token=token,
        api_version=api_version,
    )
    for job in jobs:
        name = job.get("name", "job")
        job_conclusion = job.get("conclusion", "unknown")
        print(f"  job {name}: {job_conclusion}")

    if conclusion != "success" and log_failed_jobs:
        for job in jobs:
            if job.get("conclusion") not in {"failure", "cancelled", "timed_out"}:
                continue
            job_id = job.get("id")
            if not isinstance(job_id, int):
                continue
            print(f"\n--- logs: {job.get('name', job_id)} ---")
            log_text = download_job_log_text(
                repo=repo,
                job_id=job_id,
                token=token,
                api_version=api_version,
            )
            print(tail_lines(log_text, log_tail_lines))

    return 0 if conclusion == "success" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Trigger GitHub workflow_dispatch presets and wait for results."
    )
    parser.add_argument(
        "preset",
        nargs="?",
        help="Preset name from config/github_workflows.json",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to github_workflows JSON (default: config/github_workflows.json)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List configured presets and exit",
    )
    parser.add_argument(
        "--token",
        default="",
        help="GitHub PAT (default: token_env from config)",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Dispatch only; do not poll for completion",
    )
    parser.add_argument(
        "--no-logs",
        action="store_true",
        help="Do not print failed job logs after a failed run",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        config = load_workflow_config(args.config)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.list:
        print_preset_list(config)
        return 0

    if not args.preset:
        parser.error("preset name is required unless --list is used")

    try:
        preset = resolve_preset(config, args.preset)
        token = resolve_token(config, args.token or None)
    except (KeyError, ValueError, GitHubActionsError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    defaults = config.get("defaults", {})
    wait = bool(defaults.get("wait", True)) and not args.no_wait
    log_failed_jobs = bool(defaults.get("log_failed_jobs", True)) and not args.no_logs
    log_tail_lines = int(defaults.get("log_tail_lines", 80))

    try:
        return run_workflow(
            config=config,
            preset=preset,
            token=token,
            wait=wait,
            log_failed_jobs=log_failed_jobs,
            log_tail_lines=log_tail_lines,
        )
    except GitHubActionsError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
