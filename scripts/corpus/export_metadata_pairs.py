"""Export EN↔BG title/description pairs (Airtable BG + Drive/Airtable EN)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from catalog_parser.airtable import AirtableClient
from catalog_parser.auth import (
    DEFAULT_AUTH_PORT,
    get_docs_service,
    get_drive_service,
    get_drive_service_noninteractive,
)
from catalog_parser.drive_docs import DriveDocsError
from catalog_parser.runtime_env import materialize_credentials
from catalog_parser.translation.corpus import (
    DEFAULT_HOLDOUT_COUNT,
    DEFAULT_HOLDOUT_SEED,
    default_current_year,
)
from catalog_parser.translation.metadata_corpus import (
    DriveFieldCache,
    MetadataCandidate,
    build_pairs_for_candidate,
    load_metadata_candidates_for_corpus,
)

DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "corpus" / "metadata_pairs.jsonl"
DEFAULT_SUMMARY = PROJECT_ROOT / "data" / "corpus" / "metadata_export_summary.json"
DEFAULT_CREDENTIALS = PROJECT_ROOT / "credentials.json"
DEFAULT_TOKEN = PROJECT_ROOT / "token.json"
DEFAULT_SERVICE_ACCOUNT = (
    PROJECT_ROOT / "credentials" / "google-sheets-service-account.json"
)


@dataclass
class ExportStats:
    candidates: int = 0
    title_pairs: int = 0
    description_pairs: int = 0
    skipped_existing: int = 0
    failed: int = 0
    drive_reads: int = 0


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError, AttributeError):
                pass


def load_env(project_root: Path) -> None:
    from catalog_parser.__main__ import load_env_file

    load_env_file(project_root / ".env")
    materialize_credentials(project_root)
    # Prefer the local service-account file when OAuth client credentials are absent.
    sa_path = DEFAULT_SERVICE_ACCOUNT
    if sa_path.is_file() and not os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip():
        os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"] = str(sa_path)
    if sa_path.is_file() and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(sa_path)


def build_google_services(
    *,
    credentials: Path,
    token: Path,
    auth_port: int,
    use_console: bool,
):
    if credentials.is_file():
        drive_service = get_drive_service(
            credentials,
            token,
            auth_port=auth_port,
            use_console=use_console,
        )
        docs_service = get_docs_service(
            credentials,
            token,
            auth_port=auth_port,
            use_console=use_console,
        )
        return drive_service, docs_service

    drive_service = get_drive_service_noninteractive()
    # Docs uses the same service-account credentials (full SCOPES via env file).
    docs_service = get_docs_service(credentials, token, use_console=True)
    return drive_service, docs_service


def build_airtable_client() -> AirtableClient:
    token = os.getenv("AIRTABLE_TOKEN", "").strip()
    base_id = os.getenv("AIRTABLE_BASE_ID", "").strip()
    table_name = os.getenv("AIRTABLE_TABLE_NAME", "").strip()
    if not token or not base_id or not table_name:
        raise SystemExit(
            "Requires AIRTABLE_TOKEN, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME in .env"
        )
    return AirtableClient(
        token=token,
        base_id=base_id,
        table_name=table_name,
        api_base=os.getenv("AIRTABLE_API_BASE", "https://api.airtable.com/v0").strip()
        or "https://api.airtable.com/v0",
    )


def load_exported_record_ids(output_path: Path) -> set[str]:
    if not output_path.exists():
        return set()
    ids: set[str] = set()
    for line in output_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        record_id = payload.get("record_id")
        if isinstance(record_id, str) and record_id.strip():
            ids.add(record_id.strip())
    return ids


def write_summary(
    path: Path,
    stats: ExportStats,
    failures: list[dict[str, str]],
    *,
    current_year: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "current_year": current_year,
                "candidates": stats.candidates,
                "title_pairs": stats.title_pairs,
                "description_pairs": stats.description_pairs,
                "skipped_existing": stats.skipped_existing,
                "failed": stats.failed,
                "drive_reads": stats.drive_reads,
                "failures": failures,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def export_metadata(
    candidates: list[MetadataCandidate],
    *,
    drive_cache: DriveFieldCache | None,
    output_path: Path,
    summary_path: Path,
    current_year: str,
    limit: int | None,
    resume: bool,
    dry_run: bool,
    delay_seconds: float,
) -> int:
    stats = ExportStats(candidates=len(candidates))
    failures: list[dict[str, str]] = []
    exported_ids = load_exported_record_ids(output_path) if resume else set()
    selected = candidates[:limit] if limit is not None else candidates

    if dry_run:
        for candidate in selected:
            has_bg = bool(candidate.bg_title or candidate.bg_description)
            print(
                f"DRY RUN: [{candidate.source}] {candidate.title} "
                f"bg_title={bool(candidate.bg_title)} "
                f"bg_desc={bool(candidate.bg_description)} "
                f"folder={bool(candidate.video_folder)} "
                f"eligible={has_bg}"
            )
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as handle:
        for index, candidate in enumerate(selected, start=1):
            if resume and candidate.record_id in exported_ids:
                stats.skipped_existing += 1
                print(
                    f"[{index}/{len(selected)}] SKIP existing: {candidate.title}",
                    flush=True,
                )
                continue

            print(
                f"[{index}/{len(selected)}] Metadata [{candidate.source}]: {candidate.title}",
                flush=True,
            )
            before_cache = (
                drive_cache.cached_folder_count if drive_cache is not None else 0
            )
            try:
                pairs, notes = build_pairs_for_candidate(candidate, drive_cache)
                if (
                    drive_cache is not None
                    and drive_cache.cached_folder_count > before_cache
                ):
                    stats.drive_reads += 1
                if not pairs:
                    stats.failed += 1
                    message = "; ".join(notes) if notes else "no pairs"
                    failures.append(
                        {
                            "title": candidate.title,
                            "source": candidate.source,
                            "error": message,
                        }
                    )
                    print(f"  -> ERROR: {message}", flush=True)
                    continue

                for pair in pairs:
                    handle.write(
                        json.dumps(asdict(pair), ensure_ascii=False) + "\n"
                    )
                    if pair.kind == "title":
                        stats.title_pairs += 1
                    else:
                        stats.description_pairs += 1
                handle.flush()
                exported_ids.add(candidate.record_id)
                note_suffix = f" ({'; '.join(notes)})" if notes else ""
                print(
                    f"  -> title={sum(1 for p in pairs if p.kind == 'title')} "
                    f"description={sum(1 for p in pairs if p.kind == 'description')}"
                    f"{note_suffix}",
                    flush=True,
                )
            except (DriveDocsError, OSError, ValueError, json.JSONDecodeError) as exc:
                stats.failed += 1
                failures.append(
                    {
                        "title": candidate.title,
                        "source": candidate.source,
                        "error": str(exc),
                    }
                )
                print(f"  -> ERROR: {exc}", flush=True)

            if delay_seconds > 0 and index < len(selected):
                time.sleep(delay_seconds)

    write_summary(summary_path, stats, failures, current_year=current_year)
    print(
        f"Done: {stats.title_pairs} title pair(s), "
        f"{stats.description_pairs} description pair(s), "
        f"{stats.skipped_existing} skipped, {stats.failed} failed, "
        f"{stats.drive_reads} Drive folder read(s)"
    )
    print(f"Output: {output_path}")
    print(f"Summary: {summary_path}")
    return 1 if stats.failed else 0


def main() -> int:
    configure_stdio()
    load_env(PROJECT_ROOT)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument(
        "--current-year",
        default=os.getenv("CORPUS_CURRENT_YEAR", default_current_year()),
    )
    parser.add_argument(
        "--holdout-count",
        type=int,
        default=int(os.getenv("CORPUS_HOLDOUT_COUNT", str(DEFAULT_HOLDOUT_COUNT))),
    )
    parser.add_argument("--holdout-seed", default=DEFAULT_HOLDOUT_SEED)
    parser.add_argument("--archives-only", action="store_true")
    parser.add_argument("--current-only", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument(
        "--credentials",
        type=Path,
        default=Path(
            os.getenv(
                "GOOGLE_OAUTH_CREDENTIALS",
                os.getenv("GOOGLE_CREDENTIALS", str(DEFAULT_CREDENTIALS)),
            )
        ),
    )
    parser.add_argument(
        "--token",
        type=Path,
        default=Path(
            os.getenv(
                "GOOGLE_OAUTH_TOKEN",
                os.getenv("GOOGLE_TOKEN", str(DEFAULT_TOKEN)),
            )
        ),
    )
    parser.add_argument("--auth-port", type=int, default=DEFAULT_AUTH_PORT)
    parser.add_argument("--console-auth", action="store_true")
    parser.add_argument(
        "--skip-drive",
        action="store_true",
        help="Only use EN fields already present in Airtable (no Drive reads).",
    )
    args = parser.parse_args()

    if args.archives_only and args.current_only:
        raise SystemExit("--archives-only and --current-only cannot be used together")

    airtable = build_airtable_client()
    current_year = str(args.current_year).strip() or default_current_year()
    candidates = load_metadata_candidates_for_corpus(
        airtable,
        current_year=current_year,
        holdout_count=max(0, args.holdout_count),
        holdout_seed=args.holdout_seed,
        include_archives=not args.current_only,
        include_current=not args.archives_only,
    )
    print(f"Metadata candidates: {len(candidates)}")

    drive_cache: DriveFieldCache | None = None
    if not args.dry_run and not args.skip_drive:
        drive_service, docs_service = build_google_services(
            credentials=args.credentials,
            token=args.token,
            auth_port=args.auth_port,
            use_console=args.console_auth,
        )
        drive_cache = DriveFieldCache(drive_service, docs_service)

    return export_metadata(
        candidates,
        drive_cache=drive_cache,
        output_path=args.output,
        summary_path=args.summary,
        current_year=current_year,
        limit=args.limit,
        resume=args.resume,
        dry_run=args.dry_run,
        delay_seconds=max(0.0, args.delay),
    )


if __name__ == "__main__":
    raise SystemExit(main())
