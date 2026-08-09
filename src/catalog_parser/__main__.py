from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from catalog_parser.airtable import AirtableClient, load_existing_titles_for_ingest
from catalog_parser.eligibility import (
    airtable_identity_collision_reasons,
    catalog_original_video_key,
    catalog_video_folder_id,
    catalog_yt_title_key,
    explain_catalog_eligibility,
    is_catalog_eligible,
    needs_bulgarian_translation,
    register_title_identity,
)
from catalog_parser.drive_mix import record_has_mixable_media
from catalog_parser.auth import (
    DEFAULT_AUTH_PORT,
    get_docs_service,
    get_drive_service,
    get_sheets_service,
    inspect_credentials,
)
from catalog_parser.canva import CanvaClient, build_canva_client_from_env
from catalog_parser.drive_docs import enrich_records_with_yt_titles
from catalog_parser.drive_thumbnail import enrich_records_with_original_video_thumbnails
from catalog_parser.parser import (
    DEFAULT_LIMIT,
    DEFAULT_VIDEO_TYPE,
    VIDEO_TYPES,
    extract_sheet_id,
    filter_by_pkg_tn,
    parse_catalog,
    parse_video_type,
    tn_is_marked,
    type_duration_bounds,
)
from catalog_parser.runtime_env import materialize_credentials, maybe_persist_canva_token
from catalog_parser.smartcat import DEFAULT_TARGET_LANGUAGE, DEFAULT_UI_BASE, SmartcatError
from catalog_parser.smartcat_api import SmartcatApiClient
from catalog_parser.smartcat_web import (
    DEFAULT_STORAGE_STATE,
    SmartcatWebClient,
    SmartcatWebSession,
    enrich_records_with_bulgarian_srt_links_web,
    login_interactive,
)
from catalog_parser.smartcat_cookie import (
    import_browser_session_file,
    print_smartcat_import_instructions,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CREDENTIALS = PROJECT_ROOT / "credentials.json"
DEFAULT_TOKEN = PROJECT_ROOT / "token.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "catalog.json"
DEFAULT_SMARTCAT_STATE = PROJECT_ROOT / DEFAULT_STORAGE_STATE
DEFAULT_CANVA_TOKEN = PROJECT_ROOT / "credentials" / "canva-token.json"
DEFAULT_UNASSIGNED_INGEST_COUNT = 4


def load_existing_airtable_titles() -> set[str]:
    airtable_token = os.getenv("AIRTABLE_TOKEN", "").strip()
    airtable_base_id = os.getenv("AIRTABLE_BASE_ID", "").strip()
    airtable_table_name = os.getenv("AIRTABLE_TABLE_NAME", "").strip()
    if not airtable_token or not airtable_base_id or not airtable_table_name:
        return set()

    airtable_client = AirtableClient(
        token=airtable_token,
        base_id=airtable_base_id,
        table_name=airtable_table_name,
        api_base=os.getenv("AIRTABLE_API_BASE", "https://api.airtable.com/v0").strip()
        or "https://api.airtable.com/v0",
    )
    return load_existing_titles_for_ingest(airtable_client, project_root=PROJECT_ROOT)


def load_existing_airtable_video_folder_ids() -> set[str]:
    from catalog_parser.airtable import load_existing_video_folder_ids_for_ingest

    airtable_token = os.getenv("AIRTABLE_TOKEN", "").strip()
    airtable_base_id = os.getenv("AIRTABLE_BASE_ID", "").strip()
    airtable_table_name = os.getenv("AIRTABLE_TABLE_NAME", "").strip()
    if not airtable_token or not airtable_base_id or not airtable_table_name:
        return set()

    airtable_client = AirtableClient(
        token=airtable_token,
        base_id=airtable_base_id,
        table_name=airtable_table_name,
        api_base=os.getenv("AIRTABLE_API_BASE", "https://api.airtable.com/v0").strip()
        or "https://api.airtable.com/v0",
    )
    return load_existing_video_folder_ids_for_ingest(airtable_client)


def load_existing_airtable_original_video_names() -> set[str]:
    from catalog_parser.airtable import load_existing_original_video_names_for_ingest

    airtable_token = os.getenv("AIRTABLE_TOKEN", "").strip()
    airtable_base_id = os.getenv("AIRTABLE_BASE_ID", "").strip()
    airtable_table_name = os.getenv("AIRTABLE_TABLE_NAME", "").strip()
    if not airtable_token or not airtable_base_id or not airtable_table_name:
        return set()

    airtable_client = AirtableClient(
        token=airtable_token,
        base_id=airtable_base_id,
        table_name=airtable_table_name,
        api_base=os.getenv("AIRTABLE_API_BASE", "https://api.airtable.com/v0").strip()
        or "https://api.airtable.com/v0",
    )
    return load_existing_original_video_names_for_ingest(airtable_client)


def load_existing_airtable_original_video_keys() -> set[str]:
    from catalog_parser.airtable import load_existing_original_video_keys_for_ingest

    airtable_token = os.getenv("AIRTABLE_TOKEN", "").strip()
    airtable_base_id = os.getenv("AIRTABLE_BASE_ID", "").strip()
    airtable_table_name = os.getenv("AIRTABLE_TABLE_NAME", "").strip()
    if not airtable_token or not airtable_base_id or not airtable_table_name:
        return set()

    airtable_client = AirtableClient(
        token=airtable_token,
        base_id=airtable_base_id,
        table_name=airtable_table_name,
        api_base=os.getenv("AIRTABLE_API_BASE", "https://api.airtable.com/v0").strip()
        or "https://api.airtable.com/v0",
    )
    return load_existing_original_video_keys_for_ingest(airtable_client)


def enrich_single_record_with_smartcat_api(
    record: dict,
    *,
    smartcat_language: str,
) -> dict:
    from catalog_parser.smartcat import enrich_records_with_bulgarian_srt_links

    smartcat_account_id = os.getenv("SMARTCAT_ACCOUNT_ID", "").strip()
    smartcat_api_key = os.getenv("SMARTCAT_API_KEY", "").strip()
    api_client = SmartcatApiClient(
        account_id=smartcat_account_id,
        api_key=smartcat_api_key,
        api_base=os.getenv("SMARTCAT_API_BASE", "https://ea.smartcat.ai").strip()
        or "https://ea.smartcat.ai",
    )
    return enrich_records_with_bulgarian_srt_links(
        [record],
        api_client,
        language=smartcat_language,
    )[0]


def enrich_single_record_with_smartcat_web(
    record: dict,
    session: SmartcatWebSession,
    *,
    smartcat_language: str,
) -> dict:
    from catalog_parser.smartcat import enrich_records_with_bulgarian_srt_links

    return enrich_records_with_bulgarian_srt_links(
        [record],
        session,
        language=smartcat_language,
    )[0]


def build_eligible_catalog_records(
    candidates: list[dict],
    *,
    target_count: int,
    existing_titles: set[str],
    existing_folder_ids: set[str] | None = None,
    existing_original_video_names: set[str] | None = None,
    existing_original_video_keys: set[str] | None = None,
    smartcat_enabled: bool,
    smartcat_api: bool,
    smartcat_language: str,
    web_client: SmartcatWebClient | None,
    drive_docs_enabled: bool,
    drive_service,
    docs_service,
    canva_client: CanvaClient | None,
    require_mixable_media: bool,
    thumbnail_staging_dir: Path | None = None,
    video_type: str | None = None,
) -> tuple[list[dict], int]:
    eligible: list[dict] = []
    scanned = 0
    folder_ids = existing_folder_ids if existing_folder_ids is not None else set()
    original_video_names = (
        existing_original_video_names
        if existing_original_video_names is not None
        else set()
    )
    original_video_keys = (
        existing_original_video_keys
        if existing_original_video_keys is not None
        else set()
    )

    def _print_skip_reasons(reasons: list[str]) -> None:
        for reason in reasons:
            print(f"  -> skipped: {reason}")

    def _identity_reasons(record: dict) -> list[str]:
        return airtable_identity_collision_reasons(
            record,
            existing_titles,
            existing_folder_ids=folder_ids,
            existing_original_video_names=original_video_names,
            existing_original_video_keys=original_video_keys,
            video_type=video_type,
        )

    def _mark_eligible(record: dict) -> None:
        eligible.append(record)
        register_title_identity(existing_titles, record, video_type=video_type)
        folder_id = catalog_video_folder_id(record)
        if folder_id:
            folder_ids.add(folder_id)
        yt_title_key = catalog_yt_title_key(record)
        if yt_title_key:
            original_video_names.add(yt_title_key)
        original_video_key = catalog_original_video_key(record)
        if original_video_key:
            original_video_keys.add(original_video_key)
        print(f"  -> eligible ({len(eligible)}/{target_count})")

    def _run_ai_enrichment(record: dict) -> dict:
        """Smartcat subtitle prefill + metadata/caption translate for survivors only."""
        if smartcat_enabled and record.get("pkgBgSrtLk"):
            from catalog_parser.translation.prefill import (
                ai_prefill_enabled,
                prefill_record_if_needed,
            )

            if ai_prefill_enabled():
                try:
                    from catalog_parser.smartcat_export import (
                        build_cookie_client_from_env,
                    )

                    cookie_client = build_cookie_client_from_env(
                        project_root=PROJECT_ROOT
                    )
                    prefill = prefill_record_if_needed(
                        record,
                        cookie_client,
                        project_root=PROJECT_ROOT,
                    )
                    if prefill.skipped:
                        print("  -> AI prefill skipped (already has translation)")
                    elif prefill.ok:
                        print(
                            f"  -> AI prefill wrote {prefill.written_segments} "
                            f"segment(s) from {prefill.source_cues} cue(s)"
                        )
                    else:
                        print(
                            f"  -> AI prefill failed (continuing): {prefill.error}"
                        )
                except Exception as exc:  # noqa: BLE001
                    print(f"  -> AI prefill failed (continuing): {exc}")

        from catalog_parser.translation.prefill import ai_prefill_enabled
        from catalog_parser.translation.caption_prefill import (
            translate_record_caption_if_needed,
        )
        from catalog_parser.translation.metadata_prefill import (
            translate_record_metadata_if_needed,
        )

        if not ai_prefill_enabled():
            return record

        try:
            meta = translate_record_metadata_if_needed(
                record,
                project_root=PROJECT_ROOT,
            )
            if meta.skipped and not meta.errors:
                print("  -> AI metadata translate skipped")
            elif meta.title_translated or meta.description_translated:
                parts: list[str] = []
                if meta.title_translated:
                    parts.append("title")
                if meta.description_translated:
                    parts.append("description")
                print(f"  -> AI metadata translated {', '.join(parts)}")
                if meta.errors:
                    print(
                        "  -> AI metadata partial errors: "
                        + "; ".join(meta.errors)
                    )
            elif meta.errors:
                print(
                    "  -> AI metadata translate failed (continuing): "
                    + "; ".join(meta.errors)
                )
        except Exception as exc:  # noqa: BLE001
            print(f"  -> AI metadata translate failed (continuing): {exc}")

        try:
            caption = translate_record_caption_if_needed(
                record,
                project_root=PROJECT_ROOT,
                drive_service=drive_service if drive_docs_enabled else None,
            )
            if caption.skipped and not caption.errors:
                print("  -> AI caption translate skipped")
            elif caption.caption_translated:
                source = caption.source or "unknown"
                print(f"  -> AI caption translated (source={source})")
                if caption.errors:
                    print(
                        "  -> AI caption partial errors: "
                        + "; ".join(caption.errors)
                    )
            elif caption.errors:
                print(
                    "  -> AI caption translate failed (continuing): "
                    + "; ".join(caption.errors)
                )
        except Exception as exc:  # noqa: BLE001
            print(f"  -> AI caption translate failed (continuing): {exc}")
        return record

    def process_candidate(candidate: dict) -> None:
        nonlocal scanned
        if len(eligible) >= target_count:
            return

        scanned += 1
        record = dict(candidate)
        label = record.get("ctTitle")
        label_text = label if isinstance(label, str) and label else f"row {scanned}"
        print(f"Candidate {scanned}: {label_text}")

        # Cheap Airtable identity checks before Smartcat / Drive / AI.
        early_dupes = _identity_reasons(record)
        if early_dupes:
            _print_skip_reasons(early_dupes)
            return

        if smartcat_enabled:
            if smartcat_api:
                record = enrich_single_record_with_smartcat_api(
                    record,
                    smartcat_language=smartcat_language,
                )
            elif web_client is not None and smartcat_session is not None:
                record = enrich_single_record_with_smartcat_web(
                    record,
                    smartcat_session,
                    smartcat_language=smartcat_language,
                )
            if record.get("pkgBgSrtLk"):
                print("  -> Smartcat editor link resolved")
            elif record.get("pkgBgSrtLkSkipReason"):
                print(f"  -> Smartcat: {record['pkgBgSrtLkSkipReason']}")
            elif record.get("pkgBgSrtLkError"):
                print(f"  -> Smartcat error: {record['pkgBgSrtLkError']}")
            else:
                print("  -> Smartcat: no editor link")

            # Completed Smartcat translations stay skipped (no open editor link).
            if not needs_bulgarian_translation(record):
                _print_skip_reasons(
                    explain_catalog_eligibility(
                        record,
                        existing_titles,
                        existing_folder_ids=folder_ids,
                        existing_original_video_names=original_video_names,
                        existing_original_video_keys=original_video_keys,
                        drive_service=None,
                        require_smartcat=True,
                        require_mixable_media=False,
                        video_type=video_type,
                    )
                )
                return

        if drive_docs_enabled:
            record = enrich_records_with_yt_titles(
                [record],
                drive_service,
                docs_service,
            )[0]
            # ytTitle may unlock Original Video Name collisions — check before thumbs/AI.
            post_drive_dupes = _identity_reasons(record)
            if post_drive_dupes:
                _print_skip_reasons(post_drive_dupes)
                return
            record = enrich_records_with_original_video_thumbnails(
                [record],
                drive_service,
                docs_service,
                canva_client=canva_client,
                staging_dir=thumbnail_staging_dir,
                catalog_peers=candidates,
            )[0]

        if require_mixable_media:
            if drive_service is None or not record_has_mixable_media(
                drive_service,
                record,
                video_type=video_type,
            ):
                _print_skip_reasons(
                    explain_catalog_eligibility(
                        record,
                        existing_titles,
                        existing_folder_ids=folder_ids,
                        existing_original_video_names=original_video_names,
                        existing_original_video_keys=original_video_keys,
                        drive_service=drive_service,
                        require_smartcat=False,
                        require_mixable_media=True,
                        video_type=video_type,
                    )
                )
                return

        record = _run_ai_enrichment(record)

        if is_catalog_eligible(
            record,
            existing_titles,
            existing_folder_ids=folder_ids,
            existing_original_video_names=original_video_names,
            existing_original_video_keys=original_video_keys,
            drive_service=drive_service if require_mixable_media else None,
            require_smartcat=smartcat_enabled,
            require_mixable_media=require_mixable_media,
            video_type=video_type,
        ):
            _mark_eligible(record)
        else:
            _print_skip_reasons(
                explain_catalog_eligibility(
                    record,
                    existing_titles,
                    existing_folder_ids=folder_ids,
                    existing_original_video_names=original_video_names,
                    existing_original_video_keys=original_video_keys,
                    drive_service=drive_service if require_mixable_media else None,
                    require_smartcat=smartcat_enabled,
                    require_mixable_media=require_mixable_media,
                    video_type=video_type,
                )
            )

    smartcat_session: SmartcatWebSession | None = None
    if smartcat_enabled and not smartcat_api and web_client is not None:
        smartcat_session = SmartcatWebSession(web_client)
        with smartcat_session:
            for candidate in candidates:
                process_candidate(candidate)
                if len(eligible) >= target_count:
                    break
    else:
        for candidate in candidates:
            process_candidate(candidate)
            if len(eligible) >= target_count:
                break

    return eligible, scanned


def env_flag_enabled(env_name: str, *, default: bool) -> bool:
    raw = os.getenv(env_name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"0", "false", "no"}:
        return False
    return normalized in {"1", "true", "yes"}


def resolve_feature_enabled(
    cli_override: bool | None,
    env_name: str,
    *,
    default: bool,
) -> bool:
    if cli_override is not None:
        return cli_override
    return env_flag_enabled(env_name, default=default)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Airtable production workflow (default), or ingest new catalog "
            "rows from Google Sheets."
        )
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=DEFAULT_CREDENTIALS,
        help="Path to OAuth client credentials.json.",
    )
    parser.add_argument(
        "--token",
        type=Path,
        default=DEFAULT_TOKEN,
        help="Path to store the OAuth token after first sign-in.",
    )
    parser.add_argument(
        "--auth-port",
        type=int,
        default=DEFAULT_AUTH_PORT,
        help=f"Local port for OAuth callback (default: {DEFAULT_AUTH_PORT}).",
    )
    parser.add_argument(
        "--console-auth",
        action="store_true",
        help="Use manual code entry instead of a localhost callback.",
    )
    parser.add_argument(
        "--check-auth",
        action="store_true",
        help="Validate credentials.json and exit.",
    )
    parser.add_argument(
        "--smartcat-login",
        action="store_true",
        help="Open a browser to log in to Smartcat and save the session for ingest.",
    )
    parser.add_argument(
        "--smartcat-import-session",
        metavar="COOKIES_JSON",
        help=(
            "Import Smartcat cookies exported from your browser (Cookie-Editor JSON) "
            "into smartcat-state.json."
        ),
    )
    parser.add_argument(
        "--canva-auth",
        action="store_true",
        help="Print the Canva authorization URL and save pending PKCE state.",
    )
    parser.add_argument(
        "--canva-auth-code",
        metavar="CODE",
        help="Exchange a Canva authorization code for a saved canva-token.json.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan workflow actions without modifying Airtable or Drive.",
    )

    subparsers = parser.add_subparsers(dest="command")

    workflow_parser = subparsers.add_parser(
        "workflow",
        help="Run the daily Airtable workflow orchestrator (also the default).",
    )
    workflow_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan actions without modifying Airtable or Drive.",
    )

    ingest_parser = subparsers.add_parser(
        "ingest",
        help="Ingest new catalog rows from Google Sheets into Airtable.",
    )
    ingest_parser.add_argument(
        "--sheet-id",
        help="Google Sheet ID or full spreadsheet URL.",
    )
    ingest_parser.add_argument(
        "--sheet-name",
        help="Tab name to read (defaults to the first tab).",
    )
    ingest_parser.add_argument(
        "--range",
        dest="sheet_range",
        help="Optional A1 range, e.g. 'Products!A1:Z1000'.",
    )
    ingest_parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Path for the parsed JSON output.",
    )
    ingest_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            f"Maximum entries to include after filtering "
            f"(default: {DEFAULT_LIMIT}, 0 = no limit)."
        ),
    )
    ingest_parser.add_argument(
        "--min-duration",
        type=int,
        default=None,
        help=(
            "Minimum ctDuration in seconds to include. "
            "Defaults to the lower bound for the selected --type."
        ),
    )
    ingest_parser.add_argument(
        "--max-duration",
        type=int,
        default=None,
        help=(
            "Maximum ctDuration in seconds to include. "
            "Defaults to the upper bound for the selected --type."
        ),
    )
    ingest_parser.add_argument(
        "--type",
        dest="video_type",
        choices=[video_type.casefold() for video_type in VIDEO_TYPES],
        default=None,
        help=(
            "Video type to include based on ctDuration: "
            "Reel (<=90s), Short (91-180s), or Video (>180s). "
            f"Default: {DEFAULT_VIDEO_TYPE}."
        ),
    )
    ingest_parser.add_argument(
        "--count",
        type=int,
        default=None,
        help=(
            "Number of eligible rows to ingest into Airtable. "
            f"With --unassigned, defaults to {DEFAULT_UNASSIGNED_INGEST_COUNT}."
        ),
    )
    ingest_parser.add_argument(
        "--unassigned",
        action="store_true",
        help=(
            "Create Airtable rows without assigning a translator "
            "(Status: 7. Not Assigned)."
        ),
    )
    ingest_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview eligible rows without writing to Airtable.",
    )
    ingest_parser.add_argument(
        "--require-pkg-tn",
        action="store_true",
        help=(
            "Only consider SM catalog rows whose pkgTn mark is present and not X. "
            "By default ingest still prefers pkgTn-marked rows first, then unmarked."
        ),
    )
    smartcat_group = ingest_parser.add_mutually_exclusive_group()
    smartcat_group.add_argument(
        "--smartcat",
        action="store_true",
        dest="smartcat",
        default=None,
        help="Resolve Bulgarian SRT links from pkgSmLk using Smartcat.",
    )
    smartcat_group.add_argument(
        "--no-smartcat",
        action="store_false",
        dest="smartcat",
        help="Skip Smartcat enrichment.",
    )
    ingest_parser.add_argument(
        "--smartcat-api",
        action="store_true",
        help="Use the Smartcat company integration API instead of the web UI.",
    )
    ingest_parser.add_argument(
        "--smartcat-language",
        default=None,
        help=(
            "Target language for SRT lookup "
            f"(default: {DEFAULT_TARGET_LANGUAGE} or SMARTCAT_TARGET_LANGUAGE)."
        ),
    )
    ingest_parser.add_argument(
        "--smartcat-headed",
        action="store_true",
        help="Show the browser window while resolving Smartcat links.",
    )
    airtable_group = ingest_parser.add_mutually_exclusive_group()
    airtable_group.add_argument(
        "--airtable",
        action="store_true",
        dest="airtable",
        default=None,
        help="Sync new rows to Airtable (enabled by default).",
    )
    airtable_group.add_argument(
        "--no-airtable",
        action="store_false",
        dest="airtable",
        help="Skip Airtable sync.",
    )
    drive_docs_group = ingest_parser.add_mutually_exclusive_group()
    drive_docs_group.add_argument(
        "--drive-docs",
        action="store_true",
        dest="drive_docs",
        default=None,
        help="Read TITLE - YT and Description tables from pkgLink Drive folders.",
    )
    drive_docs_group.add_argument(
        "--no-drive-docs",
        action="store_false",
        dest="drive_docs",
        help="Skip Drive/Docs enrichment.",
    )
    ingest_parser.add_argument(
        "--sheet-only",
        action="store_true",
        help="Parse the Google Sheet only (disables Smartcat, Drive/Docs, and Airtable).",
    )
    return parser


def run_unassigned_ingest(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    count = args.count if args.count is not None else DEFAULT_UNASSIGNED_INGEST_COUNT
    if count < 1:
        parser.error("--count must be at least 1")

    airtable_token = os.getenv("AIRTABLE_TOKEN", "").strip()
    airtable_base_id = os.getenv("AIRTABLE_BASE_ID", "").strip()
    airtable_table_name = os.getenv("AIRTABLE_TABLE_NAME", "").strip()
    if not airtable_token or not airtable_base_id or not airtable_table_name:
        parser.error(
            "Unassigned ingest requires AIRTABLE_TOKEN, AIRTABLE_BASE_ID, and "
            "AIRTABLE_TABLE_NAME in .env"
        )

    video_type = parse_video_type(
        args.video_type or os.getenv("VIDEO_TYPE") or DEFAULT_VIDEO_TYPE
    )
    from catalog_parser.workflow.config import load_workflow_config
    from catalog_parser.workflow.ingest import ingest_batch_unassigned

    config = load_workflow_config(PROJECT_ROOT)
    airtable_client = AirtableClient(
        token=airtable_token,
        base_id=airtable_base_id,
        table_name=airtable_table_name,
        api_base=os.getenv("AIRTABLE_API_BASE", "https://api.airtable.com/v0").strip()
        or "https://api.airtable.com/v0",
    )

    print(
        f"Unassigned ingest: {count} {video_type}(s)"
        f"{' (dry-run)' if args.dry_run else ''}"
    )
    try:
        created_ids = ingest_batch_unassigned(
            airtable_client,
            desired_type=video_type,
            target_count=count,
            max_video_seconds=config.max_video_seconds,
            credentials_path=args.credentials,
            token_path=args.token,
            use_console=args.console_auth,
            dry_run=args.dry_run,
            require_pkg_tn=bool(getattr(args, "require_pkg_tn", False)),
            log=print,
        )
    except RuntimeError as exc:
        parser.error(str(exc))

    if args.dry_run:
        print("Dry-run complete.")
        return 0

    if not created_ids:
        print("No eligible catalog rows found.")
        return 1

    print(f"Done: created {len(created_ids)} unassigned row(s).")
    return 0


def run_ingest(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.unassigned:
        return run_unassigned_ingest(args, parser)

    sheet_id = args.sheet_id or os.getenv("SHEET_ID")
    if not sheet_id:
        parser.error("Provide --sheet-id or set SHEET_ID in .env")

    sheet_name = args.sheet_name or os.getenv("SHEET_NAME") or None
    sheet_range = args.sheet_range or os.getenv("SHEET_RANGE") or None
    if sheet_range == "":
        sheet_range = None

    sheet_id = extract_sheet_id(sheet_id)

    limit = args.limit
    if limit is None:
        env_limit = os.getenv("LIMIT")
        limit = int(env_limit) if env_limit else DEFAULT_LIMIT
    if limit < 0:
        parser.error("--limit must be 0 or greater")

    video_type = parse_video_type(
        args.video_type or os.getenv("VIDEO_TYPE") or DEFAULT_VIDEO_TYPE
    )
    type_min_duration, type_max_duration = type_duration_bounds(video_type)

    min_duration = args.min_duration if args.min_duration is not None else type_min_duration
    max_duration = args.max_duration if args.max_duration is not None else type_max_duration

    if min_duration < 0:
        parser.error("--min-duration must be 0 or greater")
    if max_duration < 0:
        parser.error("--max-duration must be 0 or greater")
    if min_duration > max_duration:
        parser.error("--min-duration cannot be greater than --max-duration")

    service = get_sheets_service(
        args.credentials,
        args.token,
        auth_port=args.auth_port,
        use_console=args.console_auth,
    )

    smartcat_enabled = resolve_feature_enabled(
        False if args.sheet_only else args.smartcat,
        "SMARTCAT_ENRICH",
        default=not args.sheet_only,
    )
    drive_docs_enabled = resolve_feature_enabled(
        False if args.sheet_only else args.drive_docs,
        "DRIVE_DOCS_ENRICH",
        default=not args.sheet_only,
    )
    airtable_enabled = resolve_feature_enabled(
        False if args.sheet_only else args.airtable,
        "AIRTABLE_SYNC",
        default=not args.sheet_only,
    )
    if args.dry_run:
        airtable_enabled = False
    require_mixable_media = not args.sheet_only

    target_count = limit if limit > 0 else DEFAULT_LIMIT
    parse_limit = limit if args.sheet_only else 0

    records = parse_catalog(
        service,
        sheet_id,
        sheet_name=sheet_name,
        sheet_range=sheet_range,
        limit=parse_limit,
        min_duration=min_duration,
        max_duration=max_duration,
        video_type=video_type,
    )
    if args.require_pkg_tn:
        before = len(records)
        records = filter_by_pkg_tn(records, require_marked=True)
        print(
            f"pkgTn filter: {len(records)}/{before} {video_type} candidate(s) "
            f"have pkgTn marked (not empty/X)."
        )
    else:
        marked = sum(1 for row in records if tn_is_marked(row.get("pkgTn")))
        print(
            f"Ingest order: {marked} pkgTn-marked {video_type} candidate(s) first, "
            f"then {len(records) - marked} unmarked."
        )

    if args.sheet_only:
        smartcat_language = (
            args.smartcat_language
            or os.getenv("SMARTCAT_TARGET_LANGUAGE")
            or DEFAULT_TARGET_LANGUAGE
        )
        if smartcat_enabled:
            storage_state_path = Path(
                os.getenv("SMARTCAT_STORAGE_STATE", str(DEFAULT_SMARTCAT_STATE))
            )
            ui_base = os.getenv("SMARTCAT_UI_BASE", DEFAULT_UI_BASE).strip() or DEFAULT_UI_BASE

            if args.smartcat_api:
                smartcat_account_id = os.getenv("SMARTCAT_ACCOUNT_ID", "").strip()
                smartcat_api_key = os.getenv("SMARTCAT_API_KEY", "").strip()
                if not smartcat_account_id or not smartcat_api_key:
                    parser.error(
                        "Smartcat API mode requires SMARTCAT_ACCOUNT_ID and "
                        "SMARTCAT_API_KEY in .env"
                    )
                from catalog_parser.smartcat import enrich_records_with_bulgarian_srt_links

                api_client = SmartcatApiClient(
                    account_id=smartcat_account_id,
                    api_key=smartcat_api_key,
                    api_base=os.getenv("SMARTCAT_API_BASE", "https://ea.smartcat.ai").strip()
                    or "https://ea.smartcat.ai",
                )
                records = enrich_records_with_bulgarian_srt_links(
                    records,
                    api_client,
                    language=smartcat_language,
                )
            else:
                web_client = SmartcatWebClient(
                    ui_base=ui_base,
                    storage_state_path=storage_state_path,
                    headless=not args.smartcat_headed,
                    language=smartcat_language,
                )
                records = enrich_records_with_bulgarian_srt_links_web(
                    records,
                    web_client,
                    language=smartcat_language,
                )

        if drive_docs_enabled:
            drive_service = get_drive_service(
                args.credentials,
                args.token,
                auth_port=args.auth_port,
                use_console=args.console_auth,
            )
            docs_service = get_docs_service(
                args.credentials,
                args.token,
                auth_port=args.auth_port,
                use_console=args.console_auth,
            )
            canva_client = build_canva_client_from_env(project_root=PROJECT_ROOT)
            records = enrich_records_with_yt_titles(
                records,
                drive_service,
                docs_service,
            )
            records = enrich_records_with_original_video_thumbnails(
                records,
                drive_service,
                docs_service,
                canva_client=canva_client,
            )
    else:
        smartcat_language = (
            args.smartcat_language
            or os.getenv("SMARTCAT_TARGET_LANGUAGE")
            or DEFAULT_TARGET_LANGUAGE
        )
        storage_state_path = Path(
            os.getenv("SMARTCAT_STORAGE_STATE", str(DEFAULT_SMARTCAT_STATE))
        )
        ui_base = os.getenv("SMARTCAT_UI_BASE", DEFAULT_UI_BASE).strip() or DEFAULT_UI_BASE

        if smartcat_enabled and args.smartcat_api:
            smartcat_account_id = os.getenv("SMARTCAT_ACCOUNT_ID", "").strip()
            smartcat_api_key = os.getenv("SMARTCAT_API_KEY", "").strip()
            if not smartcat_account_id or not smartcat_api_key:
                parser.error(
                    "Smartcat API mode requires SMARTCAT_ACCOUNT_ID and "
                    "SMARTCAT_API_KEY in .env"
                )

        drive_service = None
        docs_service = None
        if drive_docs_enabled or require_mixable_media:
            drive_service = get_drive_service(
                args.credentials,
                args.token,
                auth_port=args.auth_port,
                use_console=args.console_auth,
            )
        if drive_docs_enabled:
            docs_service = get_docs_service(
                args.credentials,
                args.token,
                auth_port=args.auth_port,
                use_console=args.console_auth,
            )

        canva_client = build_canva_client_from_env(project_root=PROJECT_ROOT)

        existing_titles = load_existing_airtable_titles()
        if not existing_titles:
            print(
                "Eligibility: no Airtable titles loaded; duplicate-title check skipped."
            )
        existing_folder_ids = load_existing_airtable_video_folder_ids()
        existing_original_video_names = load_existing_airtable_original_video_names()
        existing_original_video_keys = load_existing_airtable_original_video_keys()

        web_client = None
        if smartcat_enabled and not args.smartcat_api:
            web_client = SmartcatWebClient(
                ui_base=ui_base,
                storage_state_path=storage_state_path,
                headless=not args.smartcat_headed,
                language=smartcat_language,
            )

        records, scanned = build_eligible_catalog_records(
            records,
            target_count=target_count,
            existing_titles=existing_titles,
            existing_folder_ids=existing_folder_ids,
            existing_original_video_names=existing_original_video_names,
            existing_original_video_keys=existing_original_video_keys,
            smartcat_enabled=smartcat_enabled,
            smartcat_api=args.smartcat_api,
            smartcat_language=smartcat_language,
            web_client=web_client,
            drive_docs_enabled=drive_docs_enabled,
            drive_service=drive_service,
            docs_service=docs_service,
            canva_client=canva_client,
            require_mixable_media=require_mixable_media,
            video_type=video_type,
        )
        if len(records) < target_count:
            print(
                f"Warning: found only {len(records)} eligible row(s) after scanning "
                f"{scanned} candidate(s) (target {target_count})."
            )

    if airtable_enabled:
        airtable_token = os.getenv("AIRTABLE_TOKEN", "").strip()
        airtable_base_id = os.getenv("AIRTABLE_BASE_ID", "").strip()
        airtable_table_name = os.getenv("AIRTABLE_TABLE_NAME", "").strip()
        if not airtable_token or not airtable_base_id or not airtable_table_name:
            parser.error(
                "Airtable sync requires AIRTABLE_TOKEN, AIRTABLE_BASE_ID, and "
                "AIRTABLE_TABLE_NAME in .env"
            )
        airtable_client = AirtableClient(
            token=airtable_token,
            base_id=airtable_base_id,
            table_name=airtable_table_name,
            api_base=os.getenv("AIRTABLE_API_BASE", "https://api.airtable.com/v0").strip()
            or "https://api.airtable.com/v0",
        )
        created, skipped = airtable_client.sync_catalog_records(records)
        print(f"Airtable: created {created} row(s), skipped {skipped} existing or invalid.")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(records, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Parsed {len(records)} catalog rows ({video_type}).")
    print(f"Wrote {args.output}")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="replace")

    load_env_file(PROJECT_ROOT / ".env")
    materialize_credentials(PROJECT_ROOT)

    parser = build_parser()
    args = parser.parse_args()

    if args.check_auth:
        info = inspect_credentials(args.credentials)
        print("credentials.json looks valid.")
        print(f"  client type: {info['client_type']}")
        print(f"  client id:   {info['client_id']}")
        print(f"  redirects:   {', '.join(info['redirect_uris'])}")
        print()
        print("If browser login still fails, confirm your Google account is a Test user")
        print("on the OAuth consent screen (required while the app is in Testing).")
        return 0

    if args.smartcat_login:
        login_interactive(
            ui_base=os.getenv("SMARTCAT_UI_BASE", DEFAULT_UI_BASE).strip() or DEFAULT_UI_BASE,
            storage_state_path=Path(
                os.getenv("SMARTCAT_STORAGE_STATE", str(DEFAULT_SMARTCAT_STATE))
            ),
        )
        return 0

    if args.smartcat_import_session:
        ui_base = os.getenv("SMARTCAT_UI_BASE", DEFAULT_UI_BASE).strip() or DEFAULT_UI_BASE
        storage_state_path = Path(
            os.getenv("SMARTCAT_STORAGE_STATE", str(DEFAULT_SMARTCAT_STATE))
        )
        source_path = Path(args.smartcat_import_session)
        if not source_path.is_file():
            print(f"Cookie export file not found: {source_path}", file=sys.stderr)
            print()
            print_smartcat_import_instructions(ui_base=ui_base)
            return 1
        try:
            import_browser_session_file(
                source_path,
                storage_state_path,
                ui_base=ui_base,
                probe_project_id=os.getenv("SMARTCAT_PROBE_PROJECT_ID", "").strip() or None,
            )
        except SmartcatError as exc:
            print(f"Smartcat session import failed: {exc}", file=sys.stderr)
            print()
            print_smartcat_import_instructions(ui_base=ui_base)
            return 1
        print(f"Saved Smartcat session to {storage_state_path}")
        print("Session verified — corpus export can use cookie mode without Playwright.")
        return 0

    if args.canva_auth:
        canva_client = build_canva_client_from_env(project_root=PROJECT_ROOT)
        if canva_client is None:
            parser.error(
                "Canva auth requires CANVA_CLIENT_ID and CANVA_CLIENT_SECRET in .env"
            )
        canva_client.start_auth_flow()
        return 0

    if args.canva_auth_code:
        canva_client = build_canva_client_from_env(project_root=PROJECT_ROOT)
        if canva_client is None:
            parser.error(
                "Canva auth requires CANVA_CLIENT_ID and CANVA_CLIENT_SECRET in .env"
            )
        canva_client.complete_auth_flow(args.canva_auth_code)
        return 0

    if args.command == "ingest":
        return run_ingest(args, parser)

    from catalog_parser.workflow.orchestrator import run_workflow

    dry_run = bool(getattr(args, "dry_run", False))
    return run_workflow(
        project_root=PROJECT_ROOT,
        credentials_path=args.credentials,
        token_path=args.token,
        dry_run=dry_run,
        use_console=bool(getattr(args, "console_auth", False)),
    )


if __name__ == "__main__":
    exit_code = main()
    try:
        message = maybe_persist_canva_token(PROJECT_ROOT)
        if message:
            print(message)
    except RuntimeError as exc:
        print(f"Warning: {exc}", file=sys.stderr)
    sys.exit(exit_code)
