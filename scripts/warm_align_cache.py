"""Download the WhisperX English align model into TORCH_HOME / HF_HOME."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Warm the WhisperX wav2vec2 align-model cache without audio."
    )
    parser.add_argument(
        "--language",
        default=os.getenv("WHISPERX_ALIGN_LANGUAGE", "en").strip() or "en",
        help="Alignment language code (default: en).",
    )
    parser.add_argument(
        "--device",
        default=os.getenv("WHISPERX_ALIGN_DEVICE", "cpu").strip() or "cpu",
        help="cpu or cuda (default: cpu).",
    )
    args = parser.parse_args()

    from catalog_parser.translation.srt_retime import warm_whisperx_align_model

    print(f"TORCH_HOME={os.environ.get('TORCH_HOME', '(default)')}")
    print(f"HF_HOME={os.environ.get('HF_HOME', '(default)')}")
    warm_whisperx_align_model(language=args.language, device=args.device)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
