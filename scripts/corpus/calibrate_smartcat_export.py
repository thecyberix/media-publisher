"""Calibrate Smartcat web SRT export params for one or more videos."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from catalog_parser.airtable import AirtableClient
from catalog_parser.smartcat import DEFAULT_TARGET_LANGUAGE, SmartcatError, resolve_language_id
from catalog_parser.smartcat_export import (
    SmartcatDocumentContext,
    build_cookie_client_from_env,
    decode_srt_bytes,
    export_document_srt_via_web_api,
)
from catalog_parser.translation.corpus import (
    CorpusCandidate,
    build_corpus_selection,
    default_current_year,
)
from catalog_parser.translation.quality import score_srt_text, scorecard_pair
from catalog_parser.translation.srt import align_cues, parse_srt

# Reuse resolve_context_web from the export script.
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "corpus"))
from export_smartcat_pairs import (  # noqa: E402
    configure_stdio,
    load_env,
    resolve_context_web,
)

DEFAULT_OUT = PROJECT_ROOT / "data" / "corpus" / "calibration"
CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")


def build_airtable_client() -> AirtableClient:
    token = os.getenv("AIRTABLE_TOKEN", "").strip()
    base_id = os.getenv("AIRTABLE_BASE_ID", "").strip()
    table_name = os.getenv("AIRTABLE_TABLE_NAME", "").strip()
    if not token or not base_id or not table_name:
        raise SystemExit(
            "Requires AIRTABLE_TOKEN, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME in .env"
        )
    return AirtableClient(token=token, base_id=base_id, table_name=table_name)


def find_candidates_by_title(
    selection_candidates: list[CorpusCandidate],
    titles: list[str],
) -> list[CorpusCandidate]:
    wanted = {t.casefold().strip() for t in titles}
    found: list[CorpusCandidate] = []
    for candidate in selection_candidates:
        if candidate.title.casefold().strip() in wanted:
            found.append(candidate)
    missing = wanted - {c.title.casefold().strip() for c in found}
    if missing:
        print(f"WARNING: titles not in corpus selection: {sorted(missing)}", file=sys.stderr)
    return found


def slugify(title: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", "_", cleaned.strip())[:80]
    return cleaned or "video"


def export_matrix(
    client: Any,
    context: SmartcatDocumentContext,
    *,
    out_dir: Path,
) -> list[dict[str, Any]]:
    language_ids = [
        int(context.target_language_id),
        int(context.source_language_id or "9"),
        9,
        1026,
    ]
    # Unique preserve order
    seen: set[int] = set()
    unique_langs: list[int] = []
    for lang in language_ids:
        if lang not in seen:
            seen.add(lang)
            unique_langs.append(lang)

    results: list[dict[str, Any]] = []
    out_dir.mkdir(parents=True, exist_ok=True)

    for export_type in (0, 1, 2):
        for segment_mode in (0, 1, 2):
            for language_id in unique_langs:
                label = f"type{export_type}_seg{segment_mode}_lang{language_id}"
                path = out_dir / f"{label}.srt"
                row: dict[str, Any] = {
                    "label": label,
                    "type": export_type,
                    "segmentExportMode": segment_mode,
                    "languageId": language_id,
                    "path": str(path),
                }
                try:
                    text = export_document_srt_via_web_api(
                        client,
                        context.document_id,
                        language_id,
                        export_type=export_type,
                        segment_export_mode=segment_mode,
                    )
                    path.write_text(text, encoding="utf-8")
                    score = score_srt_text(text)
                    row.update(score)
                    row["ok"] = True
                    row["sample"] = (parse_srt(text)[0].text[:120] if parse_srt(text) else "")
                except (SmartcatError, OSError, ValueError, json.JSONDecodeError) as exc:
                    row["ok"] = False
                    row["error"] = str(exc)
                    row["cue_count"] = 0
                    row["cyrillic_rate"] = 0.0
                results.append(row)
                status = "OK" if row.get("ok") else "FAIL"
                cyr = row.get("cyrillic_rate", 0.0)
                cues = row.get("cue_count", 0)
                err = row.get("error", "")
                print(
                    f"  {label}: {status} cues={cues} cyr={cyr:.0%}"
                    + (f" err={err[:80]}" if err else "")
                )
    return results


def pick_best_target(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    ok = [r for r in results if r.get("ok") and r.get("cue_count", 0) > 0]
    if not ok:
        return None
    # Prefer highest Cyrillic rate, then more cues, prefer type=1.
    return max(
        ok,
        key=lambda r: (
            float(r.get("cyrillic_rate") or 0.0),
            1 if r.get("type") == 1 else 0,
            int(r.get("cue_count") or 0),
        ),
    )


def pick_best_source(
    results: list[dict[str, Any]],
    *,
    target_label: str | None,
) -> dict[str, Any] | None:
    ok = [
        r
        for r in results
        if r.get("ok")
        and r.get("cue_count", 0) > 0
        and r.get("label") != target_label
    ]
    if not ok:
        return None
    # Prefer low Cyrillic (English source), type 0, more cues.
    return max(
        ok,
        key=lambda r: (
            1.0 - float(r.get("cyrillic_rate") or 0.0),
            1 if r.get("type") == 0 else 0,
            int(r.get("cue_count") or 0),
        ),
    )


def calibrate_one(
    candidate: CorpusCandidate,
    client: Any,
    *,
    out_root: Path,
    target_language: str,
) -> dict[str, Any]:
    print(f"\n=== {candidate.title} [{candidate.source}] ===")
    context = resolve_context_web(client, candidate, target_language=target_language)
    print(
        f"document_id={context.document_id} "
        f"source_lang={context.source_language_id} "
        f"target_lang={context.target_language_id}"
    )
    out_dir = out_root / slugify(candidate.title)
    matrix = export_matrix(client, context, out_dir=out_dir)
    best_target = pick_best_target(matrix)
    best_source = pick_best_source(
        matrix, target_label=best_target["label"] if best_target else None
    )

    alignment: dict[str, Any] = {}
    if best_source and best_target:
        source_text = Path(best_source["path"]).read_text(encoding="utf-8")
        target_text = Path(best_target["path"]).read_text(encoding="utf-8")
        paired = scorecard_pair(source_text, target_text)
        aligned, issues = align_cues(parse_srt(source_text), parse_srt(target_text))
        alignment = {
            **paired,
            "aligned_cues": len(aligned),
            "issues": issues[:12],
            "best_source": best_source["label"],
            "best_target": best_target["label"],
        }
        print(
            f"  BEST source={best_source['label']} "
            f"(cyr={best_source.get('cyrillic_rate', 0):.0%}) "
            f"target={best_target['label']} "
            f"(cyr={best_target.get('cyrillic_rate', 0):.0%}) "
            f"aligned={len(aligned)} identical={paired['identical_rate']:.0%}"
        )

    payload = {
        "title": candidate.title,
        "source": candidate.source,
        "document_id": context.document_id,
        "project_id": context.project_id,
        "matrix": matrix,
        "best_source": best_source,
        "best_target": best_target,
        "alignment": alignment,
    }
    (out_dir / "scorecard.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def main() -> int:
    configure_stdio()
    load_env(PROJECT_ROOT)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--title",
        action="append",
        default=[],
        help="Video title to calibrate (repeatable).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Calibration output directory (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--language",
        default=os.getenv("SMARTCAT_TARGET_LANGUAGE", DEFAULT_TARGET_LANGUAGE),
    )
    parser.add_argument(
        "--current-year",
        default=os.getenv("CORPUS_CURRENT_YEAR", default_current_year()),
    )
    args = parser.parse_args()
    titles = args.title or [
        "2 Ingredients That Clean Your Gut Naturally",
        "3 Things Everyone Actually Wants | Sadhguru",
        "1 Tip To Sleep Better",
        "Yoga In Ancient Civilizations Across The World | Sadhguru",
    ]

    airtable = build_airtable_client()
    selection = build_corpus_selection(
        airtable,
        current_year=str(args.current_year).strip() or default_current_year(),
        holdout_count=0,
    )
    candidates = find_candidates_by_title(selection.export_candidates, titles)
    if not candidates:
        raise SystemExit("No matching candidates found.")

    client = build_cookie_client_from_env(project_root=PROJECT_ROOT)
    client.verify_session(
        probe_project_id=os.getenv("SMARTCAT_PROBE_PROJECT_ID", "").strip() or None
    )

    summaries: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            summaries.append(
                calibrate_one(
                    candidate,
                    client,
                    out_root=args.out,
                    target_language=args.language.strip() or DEFAULT_TARGET_LANGUAGE,
                )
            )
        except (SmartcatError, OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"  ERROR: {exc}", file=sys.stderr)
            summaries.append({"title": candidate.title, "error": str(exc)})

    summary_path = args.out / "calibration_summary.json"
    args.out.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\nWrote {summary_path}")

    # Consensus: which target labels won?
    winners: dict[str, int] = {}
    for item in summaries:
        best = item.get("best_target") or {}
        label = best.get("label")
        if label:
            winners[label] = winners.get(label, 0) + 1
    if winners:
        print("Target winners:", winners)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
