"""Retiming SRT cues against word-level audio timestamps."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from catalog_parser.drive_combine import resolve_ffmpeg_path
from catalog_parser.translation.srt import Cue, ms_to_timestamp, timestamp_to_ms

TOKEN_PATTERN = re.compile(r"[a-z0-9']+")
HOUR_MS = 3_600_000
DEFAULT_LOOKAHEAD = 48
DEFAULT_MIN_MATCH_RATIO = 0.55
DEFAULT_PAD_START_MS = 80
DEFAULT_PAD_END_MS = 120
DEFAULT_WHISPERX_WINDOW_PAD_MS = 750
DEFAULT_OPENAI_TRANSCRIBE_MODEL = "whisper-1"
_WHISPERX_ALIGN_MODEL_CACHE: dict[tuple[str, str], tuple[Any, Any]] = {}
OPENAI_TRANSCRIBE_URL = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_MAX_UPLOAD_BYTES = 24 * 1024 * 1024


class SrtRetimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class WordTiming:
    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class CueRetimeResult:
    cue: Cue
    original_start_ms: int
    original_end_ms: int
    new_start_ms: int
    new_end_ms: int
    matched_tokens: int
    token_count: int
    used_audio: bool

    @property
    def start_delta_ms(self) -> int:
        return self.new_start_ms - self.original_start_ms

    @property
    def end_delta_ms(self) -> int:
        return self.new_end_ms - self.original_end_ms


def normalize_token(text: str) -> str:
    return "".join(TOKEN_PATTERN.findall(text.casefold()))


def tokenize_caption(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in text.replace("\n", " ").split():
        token = normalize_token(raw)
        if token:
            tokens.append(token)
    return tokens


def _tokens_match(expected: str, spoken: str) -> bool:
    if not expected or not spoken:
        return False
    if expected == spoken:
        return True
    if len(expected) >= 4 and (spoken.startswith(expected) or expected.startswith(spoken)):
        return True
    return False


def _match_cue_words(
    tokens: list[str],
    words: list[WordTiming],
    start_index: int,
    *,
    lookahead: int,
) -> tuple[list[WordTiming], int]:
    cursor = start_index
    matched: list[WordTiming] = []
    for token in tokens:
        found: int | None = None
        limit = min(len(words), cursor + max(lookahead, 8))
        for index in range(cursor, limit):
            spoken = normalize_token(words[index].text)
            if _tokens_match(token, spoken):
                found = index
                break
        if found is None:
            continue
        matched.append(words[found])
        cursor = found + 1
    return matched, cursor


def retime_cues(
    cues: list[Cue],
    words: list[WordTiming],
    *,
    min_match_ratio: float = DEFAULT_MIN_MATCH_RATIO,
    lookahead: int = DEFAULT_LOOKAHEAD,
    pad_start_ms: int = DEFAULT_PAD_START_MS,
    pad_end_ms: int = DEFAULT_PAD_END_MS,
) -> list[CueRetimeResult]:
    """Map each cue onto spoken word timings. Weak matches keep original times."""
    cursor = 0
    previous_end = 0
    results: list[CueRetimeResult] = []
    for cue in cues:
        original_start, original_end = timestamp_to_ms(cue.start), timestamp_to_ms(cue.end)
        tokens = tokenize_caption(cue.text)
        matched, next_cursor = _match_cue_words(
            tokens,
            words,
            cursor,
            lookahead=lookahead,
        )
        ratio = (len(matched) / len(tokens)) if tokens else 0.0
        if tokens and matched and ratio >= min_match_ratio:
            start_ms = max(0, matched[0].start_ms - pad_start_ms)
            end_ms = matched[-1].end_ms + pad_end_ms
            if start_ms < previous_end:
                start_ms = previous_end
            if end_ms <= start_ms:
                end_ms = start_ms + max(40, original_end - original_start)
            used_audio = True
            cursor = next_cursor
        else:
            start_ms, end_ms = original_start, original_end
            used_audio = False
        previous_end = max(previous_end, end_ms)
        results.append(
            CueRetimeResult(
                cue=Cue(
                    index=cue.index,
                    start=ms_to_timestamp(start_ms),
                    end=ms_to_timestamp(end_ms),
                    text=cue.text,
                ),
                original_start_ms=original_start,
                original_end_ms=original_end,
                new_start_ms=start_ms,
                new_end_ms=end_ms,
                matched_tokens=len(matched),
                token_count=len(tokens),
                used_audio=used_audio,
            )
        )
    return results


def retimed_cues(results: list[CueRetimeResult]) -> list[Cue]:
    return [item.cue for item in results]


def shift_cues(cues: list[Cue], delta_ms: int) -> list[Cue]:
    if delta_ms == 0:
        return list(cues)
    shifted: list[Cue] = []
    for cue in cues:
        shifted.append(
            Cue(
                index=cue.index,
                start=ms_to_timestamp(timestamp_to_ms(cue.start) + delta_ms),
                end=ms_to_timestamp(timestamp_to_ms(cue.end) + delta_ms),
                text=cue.text,
            )
        )
    return shifted


def detect_hour_offset_ms(cues: list[Cue], audio_duration_ms: int) -> int:
    """Return whole hours to subtract when SRT times sit past the audio length."""
    if not cues or audio_duration_ms <= 0:
        return 0
    min_start = min(timestamp_to_ms(cue.start) for cue in cues)
    if min_start < audio_duration_ms:
        return 0
    return (min_start // HOUR_MS) * HOUR_MS


def audio_duration_ms(path: Path, *, ffmpeg_path: str | None = None) -> int:
    ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    probe = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    command = [
        str(probe if probe.exists() else "ffprobe"),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise SrtRetimeError(f"Failed to run ffprobe: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown ffprobe error").strip()
        raise SrtRetimeError(f"ffprobe duration failed: {detail}")
    try:
        seconds = float((result.stdout or "").strip())
    except ValueError as exc:
        raise SrtRetimeError(f"ffprobe returned a non-numeric duration: {result.stdout!r}") from exc
    return int(round(seconds * 1000))


def _whisperx_device(requested: str | None = None) -> str:
    if requested:
        return requested
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _words_from_whisperx_result(payload: dict[str, Any]) -> list[WordTiming]:
    raw_words = payload.get("word_segments")
    if not isinstance(raw_words, list):
        raw_words = []
        for segment in payload.get("segments") or []:
            if isinstance(segment, dict):
                words = segment.get("words")
                if isinstance(words, list):
                    raw_words.extend(words)
    out: list[WordTiming] = []
    for item in raw_words:
        if not isinstance(item, dict):
            continue
        text = item.get("word") or item.get("text")
        start = item.get("start")
        end = item.get("end")
        if not isinstance(text, str) or not isinstance(start, (int, float)):
            continue
        end_value = end if isinstance(end, (int, float)) else start
        out.append(
            WordTiming(
                text=text,
                start_ms=int(round(float(start) * 1000)),
                end_ms=int(round(float(end_value) * 1000)),
            )
        )
    if not out:
        raise SrtRetimeError("WhisperX alignment returned no word timestamps")
    return out


def load_whisperx_align_model(
    *,
    language: str = "en",
    device: str | None = None,
) -> tuple[Any, Any]:
    """Load (or return cached) WhisperX wav2vec2 align model.

    The first call downloads ~360 MB into TORCH_HOME / HF_HOME. Later calls
    reuse the in-process cache and those on-disk directories.
    """
    try:
        import whisperx
    except ImportError as exc:
        raise SrtRetimeError(
            "whisperx is not installed. Run: pip install -e \".[align]\""
        ) from exc

    chosen_device = _whisperx_device(device)
    cache_key = (language, chosen_device)
    cached = _WHISPERX_ALIGN_MODEL_CACHE.get(cache_key)
    if cached is None:
        print(
            f"Loading WhisperX align model for {language!r} on {chosen_device} "
            "(first run downloads ~360 MB wav2vec2; later runs use the cache)...",
            flush=True,
        )
        cached = whisperx.load_align_model(
            language_code=language,
            device=chosen_device,
        )
        _WHISPERX_ALIGN_MODEL_CACHE[cache_key] = cached
        print("WhisperX align model ready.", flush=True)
    return cached


def warm_whisperx_align_model(
    *,
    language: str = "en",
    device: str | None = None,
) -> None:
    """Download the align model into the local cache without aligning audio."""
    load_whisperx_align_model(language=language, device=device)


def align_words_whisperx(
    audio_path: Path,
    cues: list[Cue],
    *,
    language: str = "en",
    device: str | None = None,
    window_pad_ms: int = DEFAULT_WHISPERX_WINDOW_PAD_MS,
    ffmpeg_path: str | None = None,
) -> list[WordTiming]:
    """Forced-align existing cue text to audio with WhisperX wav2vec2."""
    try:
        import whisperx
    except ImportError as exc:
        raise SrtRetimeError(
            "whisperx is not installed. Run: pip install whisperx"
        ) from exc

    duration_ms = audio_duration_ms(audio_path, ffmpeg_path=ffmpeg_path)
    segments: list[dict[str, Any]] = []
    for cue in cues:
        start_ms = max(0, timestamp_to_ms(cue.start) - window_pad_ms)
        end_ms = min(duration_ms, timestamp_to_ms(cue.end) + window_pad_ms)
        if end_ms <= start_ms:
            end_ms = min(duration_ms, start_ms + max(400, window_pad_ms))
        segments.append(
            {
                "text": " ".join(cue.text.split()),
                "start": start_ms / 1000.0,
                "end": end_ms / 1000.0,
            }
        )

    chosen_device = _whisperx_device(device)
    model_a, metadata = load_whisperx_align_model(
        language=language,
        device=chosen_device,
    )
    print("Aligning subtitle cues to audio...", flush=True)
    audio = whisperx.load_audio(str(audio_path))
    result = whisperx.align(
        segments,
        model_a,
        metadata,
        audio,
        chosen_device,
        return_char_alignments=False,
    )
    if not isinstance(result, dict):
        raise SrtRetimeError(f"Unexpected WhisperX align payload: {result!r}")
    return _words_from_whisperx_result(result)


def format_retime_summary(results: list[CueRetimeResult]) -> str:
    used = sum(1 for item in results if item.used_audio)
    lines = [
        f"cues={len(results)} retimed={used} kept_original={len(results) - used}",
        "cue\tstart_delta_ms\tend_delta_ms\tmatched\taudio",
    ]
    for item in results:
        lines.append(
            f"{item.cue.index}\t{item.start_delta_ms}\t{item.end_delta_ms}\t"
            f"{item.matched_tokens}/{item.token_count}\t{int(item.used_audio)}"
        )
    return "\n".join(lines) + "\n"


def compress_audio_for_transcription(
    source: Path,
    destination: Path,
    *,
    ffmpeg_path: str | None = None,
) -> Path:
    """Downmix to 16 kHz mono MP3 so the file fits OpenAI's upload limit."""
    ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(source),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "64k",
        str(destination),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise SrtRetimeError(f"Failed to run ffmpeg: {exc}") from exc
    if result.returncode != 0 or not destination.exists():
        detail = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
        raise SrtRetimeError(f"ffmpeg audio compress failed: {detail}")
    if destination.stat().st_size > OPENAI_MAX_UPLOAD_BYTES:
        raise SrtRetimeError(
            f"Compressed audio is still too large for OpenAI ({destination.stat().st_size} bytes)"
        )
    return destination


def extract_mono_wav(
    source: Path,
    destination: Path,
    *,
    sample_rate: int = 16000,
    ffmpeg_path: str | None = None,
) -> Path:
    """Extract 16 kHz mono WAV from a video or audio file for local alignment."""
    ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        str(destination),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise SrtRetimeError(f"Failed to run ffmpeg: {exc}") from exc
    if result.returncode != 0 or not destination.exists():
        detail = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
        raise SrtRetimeError(f"ffmpeg audio extract failed: {detail}")
    return destination


def _parse_openai_words(payload: dict[str, Any]) -> list[WordTiming]:
    raw_words = payload.get("words")
    if not isinstance(raw_words, list) or not raw_words:
        raise SrtRetimeError(
            "OpenAI transcription did not return word timestamps. "
            "Use whisper-1 with response_format=verbose_json."
        )
    words: list[WordTiming] = []
    for item in raw_words:
        if not isinstance(item, dict):
            continue
        text = item.get("word") or item.get("text")
        start = item.get("start")
        end = item.get("end")
        if not isinstance(text, str) or not isinstance(start, (int, float)):
            continue
        end_value = end if isinstance(end, (int, float)) else start
        words.append(
            WordTiming(
                text=text,
                start_ms=int(round(float(start) * 1000)),
                end_ms=int(round(float(end_value) * 1000)),
            )
        )
    if not words:
        raise SrtRetimeError("OpenAI transcription word list was empty")
    return words


def transcribe_words_openai(
    audio_path: Path,
    *,
    api_key: str | None = None,
    model: str = DEFAULT_OPENAI_TRANSCRIBE_MODEL,
    timeout_seconds: int = 600,
) -> list[WordTiming]:
    key = (api_key or os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        raise SrtRetimeError("OPENAI_API_KEY is required to transcribe word timings")

    import requests

    with audio_path.open("rb") as handle:
        response = requests.post(
            OPENAI_TRANSCRIBE_URL,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (audio_path.name, handle)},
            data={
                "model": model,
                "response_format": "verbose_json",
                "timestamp_granularities[]": "word",
            },
            timeout=timeout_seconds,
        )
    if response.status_code >= 400:
        raise SrtRetimeError(
            f"OpenAI transcription failed (HTTP {response.status_code}): "
            f"{response.text[:500]}"
        )
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise SrtRetimeError("OpenAI transcription returned non-JSON") from exc
    if not isinstance(payload, dict):
        raise SrtRetimeError(f"Unexpected OpenAI transcription payload: {payload!r}")
    return _parse_openai_words(payload)
