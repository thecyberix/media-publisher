"""CLI for BM25 corpus index + RAG EN→BG subtitle translation."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from catalog_parser.smartcat import (  # noqa: E402
    DEFAULT_TARGET_LANGUAGE,
    SmartcatError,
    parse_pkg_sm_link,
    parse_smartcat_resource_link,
    resolve_language_id,
)
from catalog_parser.smartcat_export import (  # noqa: E402
    SmartcatDocumentContext,
    SmartcatWebSrtExporter,
    build_cookie_client_from_env,
)
from catalog_parser.translation.index import (  # noqa: E402
    DEFAULT_HOLDOUT_PATH,
    DEFAULT_INDEX_PATH,
    DEFAULT_PAIRS_PATH,
    build_index,
    load_or_build_index,
    save_index,
)
from catalog_parser.translation.rag_translate import (  # noqa: E402
    DEFAULT_BATCH_SIZE,
    DEFAULT_TOP_K,
    chat_config_from_env,
    compare_srt_pair,
    translate_srt_text,
)
from catalog_parser.translation.srt import Cue, align_cues, parse_srt, write_srt  # noqa: E402

DEFAULT_EVAL_DIR = PROJECT_ROOT / "data" / "corpus" / "eval"


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


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.strip()).strip("-").lower()
    return slug or "untitled"


def load_holdout_entry(holdout_path: Path, title: str) -> dict[str, Any]:
    payload = json.loads(holdout_path.read_text(encoding="utf-8"))
    videos = payload.get("videos") if isinstance(payload, dict) else None
    if not isinstance(videos, list):
        raise SystemExit(f"Invalid holdout manifest: {holdout_path}")
    wanted = title.strip().casefold()
    for item in videos:
        if not isinstance(item, dict):
            continue
        item_title = item.get("title")
        if isinstance(item_title, str) and item_title.strip().casefold() == wanted:
            return item
    raise SystemExit(f"Holdout title not found: {title!r}")


def context_from_holdout_entry(
    entry: dict[str, Any],
    *,
    target_language: str = DEFAULT_TARGET_LANGUAGE,
) -> SmartcatDocumentContext:
    link = entry.get("smartcat_link")
    title = entry.get("title") if isinstance(entry.get("title"), str) else ""
    if not isinstance(link, str) or not link.strip():
        raise SmartcatError(f"Holdout entry missing smartcat_link: {entry!r}")

    parsed_project = parse_pkg_sm_link(link)
    parsed_editor = parse_smartcat_resource_link(link)
    target_language_id = str(resolve_language_id(target_language))
    if parsed_editor and parsed_editor.target_language_id is not None:
        target_language_id = str(parsed_editor.target_language_id)

    if parsed_editor is not None and parsed_editor.document_id:
        return SmartcatDocumentContext(
            project_id=parsed_editor.project_id
            or (parsed_project.project_id if parsed_project else ""),
            document_id=parsed_editor.document_id,
            document_name=title,
            search=parsed_editor.search or (parsed_project.search if parsed_project else title),
            source_language_id="9",
            target_language_id=target_language_id,
        )

    raise SmartcatError(f"Could not resolve document id from holdout link: {link!r}")


def cmd_build_index(args: argparse.Namespace) -> int:
    pairs_path = Path(args.pairs)
    holdout_path = Path(args.holdout)
    index_path = Path(args.index)
    print(f"Building BM25 index from {pairs_path} (excluding {holdout_path})...")
    index = build_index(pairs_path, holdout_path=holdout_path)
    save_index(index, index_path)
    print(f"Wrote {len(index.docs)} docs -> {index_path}")
    return 0


def cmd_translate(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    output_path = Path(args.output)
    if not input_path.exists():
        raise SystemExit(f"Input SRT not found: {input_path}")

    index = load_or_build_index(
        index_path=Path(args.index),
        pairs_path=Path(args.pairs),
        holdout_path=Path(args.holdout),
    )
    config = chat_config_from_env()
    source_srt = input_path.read_text(encoding="utf-8")
    print(
        f"Translating {input_path.name} with provider={config.provider} "
        f"model={config.model} top_k={args.top_k} batch_size={args.batch_size}"
        f"{f' type={args.type}' if args.type else ''}..."
    )
    ai_srt = translate_srt_text(
        source_srt,
        index,
        config,
        top_k=args.top_k,
        batch_size=args.batch_size,
        record_type=args.type,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(ai_srt, encoding="utf-8")
    print(f"Wrote {output_path}")
    return 0


def cmd_eval_holdout(args: argparse.Namespace) -> int:
    holdout_path = Path(args.holdout)
    entry = load_holdout_entry(holdout_path, args.title)
    title = str(entry["title"])
    out_dir = Path(args.output_dir) if args.output_dir else DEFAULT_EVAL_DIR / slugify(title)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Fetching human EN/BG from Smartcat for {title!r}...")
    cookie_client = build_cookie_client_from_env(project_root=PROJECT_ROOT)
    context = context_from_holdout_entry(entry, target_language=args.target_language)
    exporter = SmartcatWebSrtExporter(cookie_client)
    source_srt, target_srt = exporter.export_bilingual_pair(context)

    en_path = out_dir / "en.srt"
    human_path = out_dir / "human.bg.srt"
    ai_path = out_dir / "ai.bg.srt"
    comparison_path = out_dir / "comparison.jsonl"
    summary_path = out_dir / "summary.json"

    en_path.write_text(source_srt, encoding="utf-8")
    human_path.write_text(target_srt, encoding="utf-8")

    # Prefer aligned EN cues (joined fragments) for RAG when alignment works.
    source_cues = parse_srt(source_srt)
    target_cues = parse_srt(target_srt)
    aligned, align_issues = align_cues(source_cues, target_cues)
    if aligned:
        translate_input = write_srt(
            [
                Cue(
                    index=pair.cue_index,
                    start=pair.start,
                    end=pair.end,
                    text=pair.source_text,
                )
                for pair in aligned
            ]
        )
        human_aligned = write_srt(
            [
                Cue(
                    index=pair.cue_index,
                    start=pair.start,
                    end=pair.end,
                    text=pair.target_text,
                )
                for pair in aligned
            ]
        )
        human_path.write_text(human_aligned, encoding="utf-8")
        en_path.write_text(translate_input, encoding="utf-8")
        source_for_translate = translate_input
        human_for_compare = human_aligned
    else:
        source_for_translate = source_srt
        human_for_compare = target_srt

    index = load_or_build_index(
        index_path=Path(args.index),
        pairs_path=Path(args.pairs),
        holdout_path=holdout_path,
    )
    config = chat_config_from_env()
    record_type = entry.get("record_type")
    record_type_text = (
        str(record_type).strip() if isinstance(record_type, str) and record_type.strip() else None
    )
    print(
        f"Translating with provider={config.provider} model={config.model} "
        f"top_k={args.top_k} batch_size={args.batch_size}"
        f"{f' type={record_type_text}' if record_type_text else ''}..."
    )
    ai_srt = translate_srt_text(
        source_for_translate,
        index,
        config,
        top_k=args.top_k,
        batch_size=args.batch_size,
        record_type=record_type_text,
    )
    ai_path.write_text(ai_srt, encoding="utf-8")

    comparison = compare_srt_pair(human_for_compare, ai_srt)
    en_cues = parse_srt(source_for_translate)
    for row, en_cue in zip(comparison["rows"], en_cues):
        row["en"] = en_cue.text

    with comparison_path.open("w", encoding="utf-8") as handle:
        for row in comparison["rows"]:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    summary = {
        "title": title,
        "record_id": entry.get("record_id"),
        "record_type": entry.get("record_type"),
        "output_dir": str(out_dir),
        "provider": config.provider,
        "model": config.model,
        "top_k": args.top_k,
        "batch_size": args.batch_size,
        "align_issues": align_issues,
        "mean_token_jaccard": comparison["mean_token_jaccard"],
        "human_cues": comparison["human_cues"],
        "ai_cues": comparison["ai_cues"],
        "compared_cues": comparison["compared_cues"],
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"Eval complete: mean token Jaccard={comparison['mean_token_jaccard']:.4f} "
        f"({comparison['compared_cues']} cues) -> {out_dir}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="BM25 corpus RAG translator for English→Bulgarian subtitles"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build-index", help="Build/refresh BM25 index from corpus JSONL")
    build.add_argument("--pairs", type=Path, default=PROJECT_ROOT / DEFAULT_PAIRS_PATH)
    build.add_argument("--holdout", type=Path, default=PROJECT_ROOT / DEFAULT_HOLDOUT_PATH)
    build.add_argument("--index", type=Path, default=PROJECT_ROOT / DEFAULT_INDEX_PATH)
    build.set_defaults(func=cmd_build_index)

    translate = sub.add_parser("translate", help="Translate a local English SRT")
    translate.add_argument("--input", type=Path, required=True)
    translate.add_argument("--output", type=Path, required=True)
    translate.add_argument("--pairs", type=Path, default=PROJECT_ROOT / DEFAULT_PAIRS_PATH)
    translate.add_argument("--holdout", type=Path, default=PROJECT_ROOT / DEFAULT_HOLDOUT_PATH)
    translate.add_argument("--index", type=Path, default=PROJECT_ROOT / DEFAULT_INDEX_PATH)
    translate.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    translate.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    translate.add_argument(
        "--type",
        choices=["Reel", "Short", "Video"],
        default=None,
        help="Airtable record type; Reel/Short force ALL CAPS Bulgarian output",
    )
    translate.set_defaults(func=cmd_translate)

    evaluate = sub.add_parser(
        "eval-holdout",
        help="Fetch holdout Reel from Smartcat, translate EN, compare to human BG",
    )
    evaluate.add_argument(
        "--title",
        default="Be The Boss Of Your Life",
        help="Holdout video title (default: Be The Boss Of Your Life)",
    )
    evaluate.add_argument("--holdout", type=Path, default=PROJECT_ROOT / DEFAULT_HOLDOUT_PATH)
    evaluate.add_argument("--pairs", type=Path, default=PROJECT_ROOT / DEFAULT_PAIRS_PATH)
    evaluate.add_argument("--index", type=Path, default=PROJECT_ROOT / DEFAULT_INDEX_PATH)
    evaluate.add_argument("--output-dir", type=Path, default=None)
    evaluate.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    evaluate.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    evaluate.add_argument("--target-language", default=DEFAULT_TARGET_LANGUAGE)
    evaluate.set_defaults(func=cmd_eval_holdout)

    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    load_env(PROJECT_ROOT)
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (SmartcatError, FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
