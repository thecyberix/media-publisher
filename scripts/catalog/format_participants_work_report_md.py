from __future__ import annotations

import json
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    inp = root / "_tmp_participants_work_report.json"
    if not inp.exists():
        raise SystemExit(f"Missing input JSON: {inp}")

    data = json.loads(inp.read_text(encoding="utf-8"))
    participants = sorted(
        data["participants"], key=lambda p: p.get("total_seconds", 0), reverse=True
    )

    md: list[str] = []
    md.append("# Participants work report (Video/Reel, Duration set)")
    md.append("")
    md.append(f"Records scanned: {data.get('records_scanned')}")
    md.append("")

    md.append(
        "Weekly buckets are relative to each participant's oldest readiness-comment timestamp."
    )
    md.append(
        "Readiness markers are detected from short comments matching translator/editor-ready phrases."
    )
    md.append("")

    for p in participants:
        name = p.get("participant_name", p.get("participant_stable", "(unknown)"))
        vids = int(p.get("processed_videos", 0))
        reels = int(p.get("processed_reels", 0))
        total_records = int(p.get("processed_records_total", vids + reels))
        sec = int(p.get("total_seconds", 0))
        md.append(f"## {name}")
        md.append(f"- Processed: **{total_records}** (videos: {vids}, reels: {reels})")
        md.append(f"- Total content: **{sec} sec**")

        weekly = p.get("weekly", {}) or {}
        items: list[str] = []
        for w in sorted(weekly.keys(), key=lambda x: int(x)):
            ww = weekly[w]
            if int(ww.get("videos", 0)) == 0 and int(ww.get("reels", 0)) == 0:
                continue
            items.append(
                f"W{w}: {int(ww.get('videos', 0))}V {int(ww.get('reels', 0))}R {int(ww.get('seconds', 0))}s"
            )
        md.append(
            "- Weekly buckets: "
            + (", ".join(items) if items else "(none)")
        )
        md.append("")

    out = root / "_tmp_participants_work_report.md"
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

