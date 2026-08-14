"""Trigger a GitHub Actions workflow_dispatch (same API as cron-job.org).

Prefer scripts/run_github_workflow.py for named presets, waiting, and log output.

Environment:
  GITHUB_DISPATCH_TOKEN — fine-grained or classic PAT with Actions: Read and write

Examples:
  python scripts/trigger_github_workflow_dispatch.py publish.yml --timing scheduled
  python scripts/trigger_github_workflow_dispatch.py publish.yml --mode videos
  python scripts/trigger_github_workflow_dispatch.py catalog-daily-workflow.yml --input dry_run=true
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_REPO = "thecyberix/media-publisher"
DEFAULT_REF = "master"
DEFAULT_API_VERSION = "2022-11-28"


def dispatch_workflow(
    workflow_file: str,
    *,
    repo: str,
    ref: str,
    token: str,
    inputs: dict[str, str],
) -> None:
    url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/"
        f"{workflow_file}/dispatches"
    )
    body = json.dumps({"ref": ref, "inputs": inputs}).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", DEFAULT_API_VERSION)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status not in {200, 201, 204}:
                raise RuntimeError(f"Unexpected status {response.status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {exc.code}: {detail}") from exc


def parse_inputs(raw_inputs: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in raw_inputs:
        if "=" not in item:
            raise ValueError(f"Invalid --input {item!r}; expected key=value")
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"Invalid --input {item!r}; empty key")
        parsed[key] = value
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Trigger workflow_dispatch via GitHub API (cron-job.org compatible)."
    )
    parser.add_argument(
        "workflow",
        help="Workflow file name, e.g. publish.yml or catalog-daily-workflow.yml",
    )
    parser.add_argument(
        "--repo",
        default=os.getenv("GITHUB_REPOSITORY", DEFAULT_REPO),
        help=f"owner/repo (default: {DEFAULT_REPO})",
    )
    parser.add_argument(
        "--ref",
        default=os.getenv("GITHUB_DISPATCH_REF", DEFAULT_REF),
        help=f"Git ref to run (default: {DEFAULT_REF})",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("GITHUB_DISPATCH_TOKEN", "").strip(),
        help="PAT with Actions: Read and write (or set GITHUB_DISPATCH_TOKEN)",
    )
    parser.add_argument(
        "--mode",
        choices=("all", "videos", "quotes"),
        help="Publish workflow mode input (publish.yml only)",
    )
    parser.add_argument(
        "--timing",
        choices=("standard", "immediate", "scheduled"),
        help="Publish workflow timing (publish.yml only)",
    )
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Additional workflow input (repeatable)",
    )
    args = parser.parse_args()

    token = args.token
    if not token:
        print(
            "GITHUB_DISPATCH_TOKEN is required (fine-grained PAT with Actions: Read and write).",
            file=sys.stderr,
        )
        return 1

    inputs = parse_inputs(args.input)
    if args.mode is not None:
        inputs["mode"] = args.mode
    if args.timing is not None:
        inputs["timing"] = args.timing

    dispatch_workflow(
        args.workflow,
        repo=args.repo,
        ref=args.ref,
        token=token,
        inputs=inputs,
    )
    print(
        f"Triggered {args.workflow} on {args.repo}@{args.ref}"
        + (f" with inputs {inputs}" if inputs else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
