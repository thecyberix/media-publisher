"""Inspect PSD text layers: lines, alignment, per-run styles."""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from media_publisher.sources.tn_docx import normalize_psd_text, english_lines_for_render
from media_publisher.sources.tn_psd import extract_line_styles, resolve_psd_target, collect_image_sizes, read_pillow_size
from psd_tools import PSDImage

CACHE = PROJECT_ROOT / "downloads" / "tn-cache"
ORIGINAL = PROJECT_ROOT / "downloads" / "original-thumbnails"

SAMPLES = [
    ("Shiva", "TN_Does Shiva Linga Look Like A Sexual Organ_ _ Sadhguru.psd", "Does Shiva Linga Look Like A Sexual Organ_.original-thumb.jpg", "YT"),
    ("Inspiring", "TN_Inspiring the World Towards Wellbeing_ Sadhguru in 2023 _ Sadhguru.psd", "Inspiring the World Towards Wellbeing_ Sadhguru in 2023 _ Sadhguru.original-thumb.jpg", "YT"),
    ("Farm", "TN_Farm vs Supermarket_ Ian Somerhalder & Sadhguru Guess.psd", "Farm vs Supermarket_ Ian Somerhalder & Sadhguru Guess.tn-render.jpg", None),
    ("Consciousness", None, "Is Consciousness a Miracle_ _ Harvard's Cognitive Scientist Steven Pinker & Sadhguru _ Full Talk.original-thumb.jpg", "YT"),
    ("Past", "TN_Don't Let Your Past Hurt You.psd", "Dont Let Your Past Hurt You.original-thumb.jpg", None),
]

JUSTIFY = {0: "left", 1: "right", 2: "center", 3: "justify"}


def dump_type_layer(layer, indent=0) -> None:
    prefix = "  " * indent
    raw = str(layer.text or "")
    norm = normalize_psd_text(raw)
    lines = english_lines_for_render(norm)
    engine = layer.engine_dict or {}
    para = (engine.get("ParagraphRun") or {}).get("RunArray") or []
    print(f"{prefix}LAYER {layer.name!r} bbox={layer.bbox}")
    print(f"{prefix}  raw={raw!r}")
    print(f"{prefix}  lines({len(lines)}): {lines}")
    style_run = engine.get("StyleRun") or {}
    runs = style_run.get("RunArray") or []
    lengths = style_run.get("RunLengthArray") or []
    print(f"{prefix}  RunLengthArray={lengths}")
    for i, run in enumerate(runs):
        data = run.get("StyleSheet", {}).get("StyleSheetData", {})
        color = data.get("FillColor")
        print(
            f"{prefix}  run[{i}]: size={data.get('FontSize')} "
            f"font={data.get('Font')} color={color}"
        )
    for i, pr in enumerate(para):
        props = pr.get("ParagraphSheet", {}).get("Properties", {})
        just = props.get("Justification")
        print(f"{prefix}  para[{i}]: Justification={just} ({JUSTIFY.get(just, '?')})")


def main() -> int:
    for label, psd_name, _, artboard_hint in SAMPLES:
        print(f"\n{'='*60}\n{label}")
        if psd_name:
            path = CACHE / psd_name
        else:
            path = next(CACHE.glob("*Consciousness*"), None)
        if path is None or not path.exists():
            print("  PSD missing", psd_name)
            continue
        psd = PSDImage.open(path)
        if artboard_hint:
            for layer in psd:
                if artboard_hint in layer.name:
                    print(f"ARTBOARD {layer.name}")
                    for child in layer:
                        if getattr(child, "kind", None) == "type":
                            dump_type_layer(child, 1)
                    styles = extract_line_styles(layer)
                    print(f"  extracted {len(styles)} line styles:")
                    for s in styles:
                        print(f"    {s.placeholder_text!r} size={s.font_size_px} color={s.color_hex} bbox={s.bbox}")
                    break
        else:
            for layer in psd:
                if getattr(layer, "kind", None) == "type":
                    dump_type_layer(layer)
            styles = extract_line_styles(psd)
            print(f"  extracted {len(styles)} line styles:")
            for s in styles:
                print(f"    {s.placeholder_text!r} size={s.font_size_px} color={s.color_hex} bbox={s.bbox}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
