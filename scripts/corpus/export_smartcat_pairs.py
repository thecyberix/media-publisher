"""Export English–Bulgarian subtitle cue pairs from Smartcat into JSONL."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from catalog_parser.airtable import AirtableClient
from catalog_parser.smartcat import DEFAULT_TARGET_LANGUAGE, SmartcatError
from catalog_parser.smartcat_export import (
    SmartcatApiSrtExporter,
    SmartcatDocumentContext,
    SmartcatWebSrtExporter,
    build_api_client_from_env,
    build_cookie_client_from_env,
    build_web_client_from_env,
    resolve_context_from_smartcat_link,
)
from catalog_parser.smartcat_cookie import ensure_storage_state_file
from catalog_parser.smartcat_web import DEFAULT_STORAGE_STATE, SmartcatWebSession
from catalog_parser.translation.corpus import (
    DEFAULT_HOLDOUT_COUNT,
    DEFAULT_HOLDOUT_PATH,
    DEFAULT_HOLDOUT_SEED,
    CorpusCandidate,
    build_corpus_selection,
    default_current_year,
    probe_corpus_sources,
    write_holdout_manifest,
)
from catalog_parser.translation.corpus_append import (
    resolve_web_document_context,
    write_aligned_pairs,
)

DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "corpus" / "subtitle_pairs.jsonl"
DEFAULT_SUMMARY = PROJECT_ROOT / "data" / "corpus" / "export_summary.json"


@dataclass
class ExportStats:
    candidates: int = 0
    exported_videos: int = 0
    exported_cues: int = 0
    skipped_existing: int = 0
    holdout_reserved: int = 0
    failed: int = 0


def load_env(project_root: Path) -> None:
    from catalog_parser.__main__ import load_env_file

    load_env_file(project_root / ".env")


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError, AttributeError):
                pass


def load_exported_titles(output_path: Path) -> set[str]:
    if not output_path.exists():
        return set()

    titles: set[str] = set()
    for line in output_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        title = payload.get("video_title")
        if isinstance(title, str) and title.strip():
            titles.add(title.strip())
    return titles


def write_summary(
    summary_path: Path,
    stats: ExportStats,
    failures: list[dict[str, str]],
    *,
    holdout_path: Path,
    current_year: str,
) -> None:
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "current_year": current_year,
        "exported_videos": stats.exported_videos,
        "exported_cues": stats.exported_cues,
        "candidates": stats.candidates,
        "holdout_reserved": stats.holdout_reserved,
        "holdout_manifest": str(holdout_path),
        "skipped_existing": stats.skipped_existing,
        "failed": stats.failed,
        "failures": failures,
        "statuses_included": [
            "3. Editing done",
            "5. Synchronization done",
            "Done & Published",
        ],
        "statuses_excluded": ["2. Translation done"],
    }
    summary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def resolve_context_web(
    web_client: Any,
    candidate: CorpusCandidate,
    *,
    target_language: str,
) -> SmartcatDocumentContext:
    return resolve_web_document_context(
        web_client,
        candidate,
        target_language=target_language,
    )


def export_candidates(
    candidates: list[CorpusCandidate],
    *,
    output_path: Path,
    summary_path: Path,
    holdout_path: Path,
    holdout_count: int,
    current_year: str,
    use_api: bool,
    backend: str,
    target_language: str,
    limit: int | None,
    resume: bool,
    dry_run: bool,
    delay_seconds: float,
) -> int:
    stats = ExportStats(candidates=len(candidates))
    failures: list[dict[str, str]] = []
    exported_titles = load_exported_titles(output_path) if resume else set()

    selected = candidates[:limit] if limit is not None else candidates
    if dry_run:
        by_source: dict[str, int] = {}
        for candidate in selected:
            by_source[candidate.source] = by_source.get(candidate.source, 0) + 1
            if resume and candidate.title in exported_titles:
                stats.skipped_existing += 1
                print(f"SKIP existing: {candidate.title}")
                continue
            print(f"DRY RUN: [{candidate.source}] {candidate.title} ({candidate.status})")
        print(
            f"Dry run complete: {len(selected)} export candidate(s), "
            f"{stats.skipped_existing} already exported"
        )
        for source, count in sorted(by_source.items()):
            print(f"  {source}: {count}")
        return 0

    api_client = None
    api_exporter = None
    web_client = None
    cookie_client = None
    web_exporter = None

    if use_api:
        api_client = build_api_client_from_env()
        api_exporter = SmartcatApiSrtExporter(api_client)
    elif backend == "cookie":
        cookie_client = build_cookie_client_from_env(project_root=PROJECT_ROOT)
        web_exporter = SmartcatWebSrtExporter(cookie_client)
    else:
        web_client = build_web_client_from_env(project_root=PROJECT_ROOT)
        web_exporter = SmartcatWebSrtExporter(web_client)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    session_cm = SmartcatWebSession(web_client) if web_client is not None else None
    if session_cm is not None:
        session_cm.__enter__()

    try:
        with output_path.open("a", encoding="utf-8") as output_handle:
            for index, candidate in enumerate(selected, start=1):
                if resume and candidate.title in exported_titles:
                    stats.skipped_existing += 1
                    print(f"[{index}/{len(selected)}] SKIP existing: {candidate.title}")
                    continue

                print(f"[{index}/{len(selected)}] Export [{candidate.source}]: {candidate.title}")
                try:
                    if api_client is not None and api_exporter is not None:
                        context = resolve_context_from_smartcat_link(
                            api_client,
                            candidate.smartcat_link,
                            title=candidate.title,
                            target_language=target_language,
                        )
                        source_srt, target_srt = api_exporter.export_bilingual_pair(context)
                    else:
                        assert web_exporter is not None
                        export_client = cookie_client or web_client
                        assert export_client is not None
                        context = resolve_context_web(
                            export_client,
                            candidate,
                            target_language=target_language,
                        )
                        source_srt, target_srt = web_exporter.export_bilingual_pair(context)

                    cue_count, issues = write_aligned_pairs(
                        candidate,
                        context,
                        source_srt,
                        target_srt,
                        output_handle=output_handle,
                    )
                    output_handle.flush()
                    stats.exported_videos += 1
                    stats.exported_cues += cue_count
                    exported_titles.add(candidate.title)
                    issue_suffix = f" ({'; '.join(issues)})" if issues else ""
                    print(f"  -> {cue_count} cue(s){issue_suffix}")
                except (SmartcatError, OSError, ValueError, json.JSONDecodeError) as exc:
                    stats.failed += 1
                    message = str(exc)
                    failures.append({"title": candidate.title, "source": candidate.source, "error": message})
                    print(f"  -> ERROR: {message}")

                if delay_seconds > 0 and index < len(selected):
                    time.sleep(delay_seconds)
    finally:
        if session_cm is not None:
            session_cm.__exit__(None, None, None)

    write_summary(
        summary_path,
        stats,
        failures,
        holdout_path=holdout_path,
        current_year=current_year,
    )
    print(
        f"Done: {stats.exported_videos} video(s), {stats.exported_cues} cue(s), "
        f"{stats.skipped_existing} skipped, {stats.failed} failed"
    )
    print(f"Output: {output_path}")
    print(f"Summary: {summary_path}")
    return 1 if stats.failed else 0


def resolve_smartcat_backend(
    *,
    project_root: Path,
    prefer_api: bool,
) -> tuple[bool, str]:
    account_id = os.getenv("SMARTCAT_ACCOUNT_ID", "").strip()
    api_key = os.getenv("SMARTCAT_API_KEY", "").strip()
    if prefer_api and account_id and api_key:
        return True, "api"

    storage_name = os.getenv("SMARTCAT_STORAGE_STATE", DEFAULT_STORAGE_STATE).strip()
    storage_path = Path(storage_name)
    if not storage_path.is_absolute():
        storage_path = project_root / storage_path

    ensure_storage_state_file(storage_path, project_root=project_root)

    if prefer_api:
        print(
            "Smartcat API credentials not found; checking web session export.",
            file=sys.stderr,
        )

    if not storage_path.is_file():
        raise SystemExit(
            "No Smartcat export backend is configured.\n\n"
            "Create smartcat-state.json locally (no paid API, no GitHub secret read):\n"
            "  1. Log in to https://ea.smartcat.com/projects in Chrome or Edge.\n"
            "  2. Install Cookie-Editor (Moustachauve): https://cookie-editor.com/\n"
            "     (not Hot Cleaner — that only exports encrypted files)\n"
            "  3. On a Smartcat page: Export → JSON (copies to clipboard).\n"
            "  4. Paste into smartcat-cookies.json, then run:\n"
            "       python -m catalog_parser --smartcat-import-session smartcat-cookies.json\n\n"
            "Alternative — Playwright login (needs working Playwright on this machine):\n"
            "  python -m catalog_parser --smartcat-login\n\n"
            "Optional — company API (paid plan):\n"
            "  SMARTCAT_ACCOUNT_ID=...  SMARTCAT_API_KEY=..."
        )

    try:
        cookie_client = build_cookie_client_from_env(project_root=project_root)
        cookie_client.verify_session(
            probe_project_id=os.getenv("SMARTCAT_PROBE_PROJECT_ID", "").strip() or None,
        )
    except SmartcatError as exc:
        raise SystemExit(f"Smartcat web session is not usable: {exc}") from exc

    return False, "cookie"


def build_airtable_client() -> AirtableClient:
    token = os.getenv("AIRTABLE_TOKEN", "").strip()
    base_id = os.getenv("AIRTABLE_BASE_ID", "").strip()
    table_name = os.getenv("AIRTABLE_TABLE_NAME", "").strip()
    if not token or not base_id or not table_name:
        raise SystemExit(
            "Airtable export requires AIRTABLE_TOKEN, AIRTABLE_BASE_ID, and "
            "AIRTABLE_TABLE_NAME in .env"
        )
    return AirtableClient(
        token=token,
        base_id=base_id,
        table_name=table_name,
        api_base=os.getenv("AIRTABLE_API_BASE", "https://api.airtable.com/v0").strip()
        or "https://api.airtable.com/v0",
    )


def main() -> int:
    configure_stdio()
    load_env(PROJECT_ROOT)

    parser = argparse.ArgumentParser(
        description=(
            "Export English/Bulgarian subtitle pairs from Smartcat for edited, "
            "published, and archived Airtable rows."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"JSONL output path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=DEFAULT_SUMMARY,
        help=f"Summary JSON path (default: {DEFAULT_SUMMARY})",
    )
    parser.add_argument(
        "--holdout-output",
        type=Path,
        default=PROJECT_ROOT / DEFAULT_HOLDOUT_PATH,
        help=f"Holdout manifest for {default_current_year()} verification videos",
    )
    parser.add_argument(
        "--current-year",
        default=os.getenv("CORPUS_CURRENT_YEAR", default_current_year()),
        help="Label/year for the live Airtable table (default: current calendar year).",
    )
    parser.add_argument(
        "--holdout-count",
        type=int,
        default=int(os.getenv("CORPUS_HOLDOUT_COUNT", str(DEFAULT_HOLDOUT_COUNT))),
        help=(
            f"Reserve this many videos from the current-year table for AI verification "
            f"(default: {DEFAULT_HOLDOUT_COUNT})."
        ),
    )
    parser.add_argument(
        "--holdout-seed",
        default=os.getenv("CORPUS_HOLDOUT_SEED", DEFAULT_HOLDOUT_SEED),
        help="Deterministic seed for holdout selection.",
    )
    parser.add_argument(
        "--archives-only",
        action="store_true",
        help="Export only archive bases (skip the current-year table entirely).",
    )
    parser.add_argument(
        "--current-only",
        action="store_true",
        help="Export only the current-year table (still applies holdout unless --holdout-count 0).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Export at most N videos (for testing).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip video titles already present in the output JSONL.",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Print candidate counts per archive/current table and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List candidates without calling Smartcat.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Seconds to wait between Smartcat exports (default: 0.5).",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Use Smartcat web session export instead of the company API.",
    )
    parser.add_argument(
        "--language",
        default=os.getenv("SMARTCAT_TARGET_LANGUAGE", DEFAULT_TARGET_LANGUAGE),
        help="Target language code for Smartcat export (default: bg).",
    )
    args = parser.parse_args()

    if args.archives_only and args.current_only:
        raise SystemExit("--archives-only and --current-only cannot be used together")

    use_api = not args.web
    if args.probe or args.dry_run:
        backend = "api" if use_api and os.getenv("SMARTCAT_ACCOUNT_ID") and os.getenv("SMARTCAT_API_KEY") else "web"
    else:
        use_api, backend = resolve_smartcat_backend(
            project_root=PROJECT_ROOT,
            prefer_api=use_api,
        )
        print(f"Smartcat export backend: {backend}")

    current_year = str(args.current_year).strip() or default_current_year()
    airtable = build_airtable_client()
    if args.probe:
        probe_corpus_sources(airtable, current_year=current_year)
        return 0

    selection = build_corpus_selection(
        airtable,
        current_year=current_year,
        holdout_count=max(0, args.holdout_count),
        holdout_seed=args.holdout_seed,
        include_archives=not args.current_only,
        include_current=not args.archives_only,
    )

    write_holdout_manifest(
        args.holdout_output,
        selection.holdout_candidates,
        current_year=current_year,
        holdout_count=max(0, args.holdout_count),
        holdout_seed=args.holdout_seed,
    )

    print(
        f"Corpus selection: {len(selection.export_candidates)} export candidate(s), "
        f"{len(selection.holdout_candidates)} holdout video(s) from {current_year} table"
    )
    if selection.holdout_candidates:
        print(f"Holdout manifest: {args.holdout_output}")

    return export_candidates(
        selection.export_candidates,
        output_path=args.output,
        summary_path=args.summary,
        holdout_path=args.holdout_output,
        holdout_count=max(0, args.holdout_count),
        current_year=current_year,
        use_api=use_api,
        backend=backend,
        target_language=args.language.strip() or DEFAULT_TARGET_LANGUAGE,
        limit=args.limit,
        resume=args.resume,
        dry_run=args.dry_run,
        delay_seconds=max(0.0, args.delay),
    )


if __name__ == "__main__":
    raise SystemExit(main())
