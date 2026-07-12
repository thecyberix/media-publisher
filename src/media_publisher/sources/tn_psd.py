from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass, replace
from pathlib import Path

try:
    from PIL import Image
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("TN PSD helpers require Pillow") from exc

ASPECT_TOLERANCE = 0.02
JUSTIFICATION_MAP = {0: "left", 1: "right", 2: "center", 3: "justify"}
LINE_BREAK_CHARS = "\r\n\x03\n"


class TnPsdError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImageSize:
    width: int
    height: int
    source: str

    @property
    def aspect(self) -> float:
        return self.width / self.height


@dataclass(frozen=True)
class MatchedArtboard:
    name: str
    width: int
    height: int
    source: str


@dataclass(frozen=True)
class TnTextSegment:
    text: str
    font_size_px: float
    color_hex: str
    font_index: int | None = None
    faux_bold: bool = False


@dataclass(frozen=True)
class TnLineStyle:
    placeholder_text: str
    rendered_text: str
    bbox: tuple[int, int, int, int]
    font_size_px: float
    color_hex: str
    font_index: int | None = None
    layer_name: str = "text"
    alignment: str = "center"
    faux_bold: bool = False
    segments: tuple[TnTextSegment, ...] = ()
    max_grow_factor: float | None = None
    allow_auto_bold: bool = True
    block_line_parts: tuple[str, ...] = ()
    stacked_line_gap_factor: float | None = None
    fixed_font_size_px: float | None = None
    stacked_line_backgrounds: tuple[str | None, ...] = ()
    stacked_line_match_widths: bool = False
    stacked_line_font_sizes: tuple[int, ...] = ()


@dataclass(frozen=True)
class TnTextStyle:
    bbox: tuple[int, int, int, int]
    font_size_px: float
    color_hex: str
    placeholder_lines: tuple[str, ...]
    layer_name: str
    line_styles: tuple[TnLineStyle, ...] = ()


def safe_cache_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", name).strip(" .")
    return cleaned or "tn-file"


def aspect_ratio_label(width: int, height: int) -> str:
    ratio = width / height
    candidates = {
        "16:9": 16 / 9,
        "9:16": 9 / 16,
        "4:5": 4 / 5,
        "1:1": 1.0,
        "4:3": 4 / 3,
        "3:4": 3 / 4,
    }
    best_name = f"{width}:{height}"
    best_delta = math.inf
    for name, target in candidates.items():
        delta = abs(ratio - target)
        if delta < best_delta:
            best_delta = delta
            best_name = name
    if best_delta <= ASPECT_TOLERANCE:
        return best_name
    return f"{ratio:.3f}"


def aspects_match(left: ImageSize, right: ImageSize) -> bool:
    return abs(left.aspect - right.aspect) <= ASPECT_TOLERANCE


def read_pillow_size(path: Path) -> ImageSize | None:
    try:
        with Image.open(path) as image:
            return ImageSize(width=image.size[0], height=image.size[1], source="pillow")
    except OSError:
        return None


def read_psd_header_size(data: bytes) -> ImageSize | None:
    if len(data) < 26 or data[:4] != b"8BPS":
        return None
    height, width = struct.unpack(">II", data[14:22])
    if width <= 0 or height <= 0:
        return None
    return ImageSize(width=width, height=height, source="psd-header")


def _is_artboard(layer) -> bool:
    return type(layer).__name__ == "Artboard" or getattr(layer, "kind", "") == "artboard"


def list_artboard_sizes(path: Path) -> list[ImageSize]:
    try:
        from psd_tools import PSDImage
    except ImportError as exc:
        raise TnPsdError(
            "PSD support requires psd-tools. Install with `pip install psd-tools`."
        ) from exc

    psd = PSDImage.open(path)
    artboards = [layer for layer in psd if _is_artboard(layer)]
    if artboards:
        return [
            ImageSize(
                width=int(layer.width),
                height=int(layer.height),
                source=f"artboard:{layer.name}",
            )
            for layer in artboards
            if int(layer.width) > 0 and int(layer.height) > 0
        ]

    return [ImageSize(width=psd.width, height=psd.height, source="psd-document")]


def collect_image_sizes(path: Path) -> list[ImageSize]:
    suffix = path.suffix.casefold()
    sizes: list[ImageSize] = []
    if suffix == ".psd":
        sizes.extend(list_artboard_sizes(path))
        if not sizes:
            data = path.read_bytes()
            header = read_psd_header_size(data)
            if header is not None:
                sizes.append(header)
    pillow = read_pillow_size(path)
    if pillow is not None and all(
        item.width != pillow.width or item.height != pillow.height for item in sizes
    ):
        sizes.append(pillow)

    deduped: list[ImageSize] = []
    seen: set[tuple[int, int, str]] = set()
    for item in sizes:
        key = (item.width, item.height, item.source)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def best_aspect_matches(
    original: ImageSize,
    candidates: list[ImageSize],
) -> list[ImageSize]:
    return [item for item in candidates if aspects_match(original, item)]


def _psd_color_to_hex(fill_color: object) -> str:
    getter = getattr(fill_color, "get", None)
    if not callable(getter):
        return "#FFFFFF"
    values = getter("Values") or []
    if len(values) >= 4:
        red, green, blue = (float(values[1]), float(values[2]), float(values[3]))
    elif len(values) >= 3:
        red, green, blue = (float(values[0]), float(values[1]), float(values[2]))
    else:
        return "#FFFFFF"
    red_byte = int(min(255, max(0, round(red * 255))))
    green_byte = int(min(255, max(0, round(green * 255))))
    blue_byte = int(min(255, max(0, round(blue * 255))))
    return f"#{red_byte:02x}{green_byte:02x}{blue_byte:02x}"


def _engine_number(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _engine_bool(value: object) -> bool:
    if value is None:
        return False
    return bool(value)


def _engine_dict_value(node: object, key: str) -> object | None:
    if isinstance(node, dict):
        return node.get(key)
    getter = getattr(node, "get", None)
    if callable(getter):
        return getter(key)
    return None


def _style_data_from_run(run: object) -> tuple[float, str, int | None, bool]:
    style_sheet = _engine_dict_value(run, "StyleSheet") or {}
    style_data = _engine_dict_value(style_sheet, "StyleSheetData") or {}
    if "StyleSheetData" in style_data and "FontSize" not in style_data:
        nested = _engine_dict_value(style_data, "StyleSheetData")
        if isinstance(nested, dict):
            style_data = nested

    raw_size = _engine_number(_engine_dict_value(style_data, "FontSize"))
    size = raw_size if raw_size is not None and raw_size > 0 else 48.0
    color_hex = _psd_color_to_hex(_engine_dict_value(style_data, "FillColor"))
    font_index_raw = _engine_dict_value(style_data, "Font")
    font_index = int(font_index_raw) if _engine_number(font_index_raw) is not None else None
    faux_bold = _engine_bool(_engine_dict_value(style_data, "FauxBold"))
    return size, color_hex, font_index, faux_bold


def _normalize_segment_text(text: str) -> str:
    return re.sub(r"[\x03\r\n]+", " ", text)


def _clean_segment_text(text: str) -> str:
    return _normalize_segment_text(text).strip()


def _line_spans_from_raw(raw_text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for index, char in enumerate(raw_text):
        if char in LINE_BREAK_CHARS:
            if index > start:
                spans.append((start, index))
            start = index + 1
    if start < len(raw_text):
        spans.append((start, len(raw_text)))
    return spans


def _paragraph_alignments(engine: dict) -> list[str]:
    paragraph_run = engine.get("ParagraphRun") or {}
    run_array = paragraph_run.get("RunArray") or []
    alignments: list[str] = []
    for entry in run_array:
        properties = (
            _engine_dict_value(_engine_dict_value(entry, "ParagraphSheet"), "Properties")
            or {}
        )
        justification = _engine_dict_value(properties, "Justification")
        just_num = _engine_number(justification)
        just_int = 2 if just_num is None else int(just_num)
        alignments.append(JUSTIFICATION_MAP.get(just_int, "center"))
    return alignments


def _merge_adjacent_segments(segments: list[TnTextSegment]) -> list[TnTextSegment]:
    if not segments:
        return []
    merged: list[TnTextSegment] = []
    for segment in segments:
        if (
            merged
            and merged[-1].font_size_px == segment.font_size_px
            and merged[-1].color_hex == segment.color_hex
            and merged[-1].font_index == segment.font_index
            and merged[-1].faux_bold == segment.faux_bold
        ):
            previous = merged[-1]
            merged[-1] = TnTextSegment(
                text=previous.text + segment.text,
                font_size_px=previous.font_size_px,
                color_hex=previous.color_hex,
                font_index=previous.font_index,
                faux_bold=previous.faux_bold,
            )
        else:
            merged.append(segment)
    return merged


def _segments_for_span(
    raw_text: str,
    runs: list,
    lengths: list[int],
    span_start: int,
    span_end: int,
) -> list[TnTextSegment]:
    cursor = 0
    segments: list[TnTextSegment] = []
    for run, length in zip(runs, lengths, strict=False):
        run_start = cursor
        run_end = cursor + int(length)
        cursor = run_end
        overlap_start = max(run_start, span_start)
        overlap_end = min(run_end, span_end)
        if overlap_start >= overlap_end:
            continue
        text = _normalize_segment_text(raw_text[overlap_start:overlap_end])
        if not text:
            continue
        font_size, color_hex, font_index, faux_bold = _style_data_from_run(run)
        if not text.strip():
            if segments:
                previous = segments[-1]
                segments[-1] = TnTextSegment(
                    text=previous.text + text,
                    font_size_px=previous.font_size_px,
                    color_hex=previous.color_hex,
                    font_index=previous.font_index,
                    faux_bold=previous.faux_bold,
                )
            continue
        segments.append(
            TnTextSegment(
                text=text,
                font_size_px=font_size,
                color_hex=color_hex,
                font_index=font_index,
                faux_bold=faux_bold,
            )
        )
    return _merge_adjacent_segments(segments)


def _segments_from_run_lengths(raw_text: str, runs: list, lengths: list[int]) -> list[TnTextSegment]:
    cursor = 0
    segments: list[TnTextSegment] = []
    for run, length in zip(runs, lengths, strict=False):
        chunk = raw_text[cursor : cursor + int(length)]
        cursor += int(length)
        text = _clean_segment_text(chunk)
        if not text:
            continue
        font_size, color_hex, font_index, faux_bold = _style_data_from_run(run)
        segments.append(
            TnTextSegment(
                text=text,
                font_size_px=font_size,
                color_hex=color_hex,
                font_index=font_index,
                faux_bold=faux_bold,
            )
        )
    return segments


def _use_run_length_lines(raw_text: str, lengths: list[int]) -> bool:
    if "\r" in raw_text or "\x03" not in raw_text:
        return False
    if not lengths:
        return False
    average = sum(int(value) for value in lengths) / len(lengths)
    return average > 3.0


def _line_styles_from_type_layer(layer) -> list[TnLineStyle]:
    raw_text = str(getattr(layer, "text", "") or "")
    if not raw_text.strip():
        return []

    engine = getattr(layer, "engine_dict", None) or {}
    style_run = engine.get("StyleRun") or {}
    runs = style_run.get("RunArray") or []
    lengths = [int(value) for value in (style_run.get("RunLengthArray") or [])]
    alignments = _paragraph_alignments(engine)

    bbox = tuple(int(value) for value in layer.bbox)
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return []

    layer_name = str(getattr(layer, "name", "text"))
    line_entries: list[tuple[str, tuple[TnTextSegment, ...], str]] = []

    if lengths and len(lengths) == len(runs) and _use_run_length_lines(raw_text, lengths):
        segments = _segments_from_run_lengths(raw_text, runs, lengths)
        for index, segment in enumerate(segments):
            alignment = alignments[min(index, len(alignments) - 1)] if alignments else "center"
            line_entries.append((segment.text, (segment,), alignment))
    else:
        spans = _line_spans_from_raw(raw_text)
        for index, (span_start, span_end) in enumerate(spans):
            line_text = _clean_segment_text(raw_text[span_start:span_end])
            if not line_text:
                continue
            segments = tuple(
                _segments_for_span(raw_text, runs, lengths, span_start, span_end)
            )
            if not segments:
                font_size, color_hex, font_index, faux_bold = _style_data_from_run(runs[0]) if runs else (48.0, "#FFFFFF", None, False)
                segments = (
                    TnTextSegment(
                        text=line_text,
                        font_size_px=font_size,
                        color_hex=color_hex,
                        font_index=font_index,
                        faux_bold=faux_bold,
                    ),
                )
            alignment = alignments[min(index, len(alignments) - 1)] if alignments else "center"
            line_entries.append((line_text, segments, alignment))

    if not line_entries:
        return []

    line_bboxes = _line_bboxes(bbox, len(line_entries))
    result: list[TnLineStyle] = []
    for (line_text, segments, alignment), line_bbox in zip(
        line_entries, line_bboxes, strict=False
    ):
        segments = _scale_segment_sizes(segments, line_bbox)
        primary = segments[0]
        max_size = max(segment.font_size_px for segment in segments)
        any_bold = any(segment.faux_bold for segment in segments)
        result.append(
            TnLineStyle(
                placeholder_text=line_text,
                rendered_text=line_text,
                bbox=line_bbox,
                font_size_px=max_size,
                color_hex=primary.color_hex,
                font_index=primary.font_index,
                layer_name=layer_name,
                alignment=alignment,
                faux_bold=any_bold,
                segments=segments,
            )
        )
    return result


def _scaled_font_size(size: float, line_bbox: tuple[int, int, int, int]) -> float:
    if size >= 12:
        return size
    _, top, _, bottom = line_bbox
    line_height = bottom - top
    if line_height <= 0:
        return max(size, 48.0)
    return max(size, line_height * 0.72)


def _scale_segment_sizes(
    segments: tuple[TnTextSegment, ...],
    line_bbox: tuple[int, int, int, int],
) -> tuple[TnTextSegment, ...]:
    return tuple(
        TnTextSegment(
            text=segment.text,
            font_size_px=_scaled_font_size(segment.font_size_px, line_bbox),
            color_hex=segment.color_hex,
            font_index=segment.font_index,
            faux_bold=segment.faux_bold,
        )
        for segment in segments
    )


def _line_bboxes(
    bbox: tuple[int, int, int, int],
    line_count: int,
) -> list[tuple[int, int, int, int]]:
    left, top, right, bottom = bbox
    if line_count <= 1:
        return [bbox]
    total_height = bottom - top
    slice_height = total_height / line_count
    boxes: list[tuple[int, int, int, int]] = []
    for index in range(line_count):
        line_top = top + round(index * slice_height)
        line_bottom = top + round((index + 1) * slice_height)
        if index == line_count - 1:
            line_bottom = bottom
        boxes.append((left, line_top, right, line_bottom))
    return boxes


def _style_from_type_layer(layer) -> tuple[float, str]:
    engine = getattr(layer, "engine_dict", None) or {}
    style_run = engine.get("StyleRun") or {}
    run_array = style_run.get("RunArray") or []
    if run_array:
        font_size, color_hex, _, _ = _style_data_from_run(run_array[0])
        return font_size, color_hex
    return 48.0, "#FFFFFF"


def _iter_type_layers(layer) -> list:
    found: list = []

    def walk(node) -> None:
        if getattr(node, "kind", None) == "type":
            found.append(node)
        if node.is_group():
            for child in node:
                walk(child)

    walk(layer)
    found.sort(key=lambda item: (item.bbox[1], item.bbox[0]))
    return found


def _match_artboard_name(path: Path, matched: ImageSize) -> str | None:
    if not matched.source.startswith("artboard:"):
        return None
    return matched.source.removeprefix("artboard:")


def resolve_psd_target(path: Path, matched: ImageSize):
    try:
        from psd_tools import PSDImage
    except ImportError as exc:
        raise TnPsdError(
            "PSD support requires psd-tools. Install with `pip install psd-tools`."
        ) from exc

    psd = PSDImage.open(path)
    artboards = [layer for layer in psd if _is_artboard(layer)]
    if artboards:
        target_name = _match_artboard_name(path, matched)
        if target_name is not None:
            for layer in artboards:
                if layer.name == target_name:
                    return layer
        for layer in artboards:
            size = ImageSize(
                width=int(layer.width),
                height=int(layer.height),
                source=f"artboard:{layer.name}",
            )
            if aspects_match(matched, size):
                return layer
        raise TnPsdError(f"No artboard matches aspect ratio for {path.name}")
    return psd


def extract_text_styles(target) -> list[TnTextStyle]:
    from media_publisher.sources.tn_docx import english_lines_for_render, normalize_psd_text

    styles: list[TnTextStyle] = []
    for layer in _iter_type_layers(target):
        line_styles = _line_styles_from_type_layer(layer)
        if line_styles:
            first = line_styles[0]
            styles.append(
                TnTextStyle(
                    bbox=tuple(int(value) for value in layer.bbox),
                    font_size_px=first.font_size_px,
                    color_hex=first.color_hex,
                    placeholder_lines=tuple(item.placeholder_text for item in line_styles),
                    layer_name=str(getattr(layer, "name", "text")),
                    line_styles=tuple(line_styles),
                )
            )
            continue

        text = normalize_psd_text(str(getattr(layer, "text", "") or ""))
        font_size, color_hex = _style_from_type_layer(layer)
        bbox = tuple(int(value) for value in layer.bbox)
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            continue
        placeholder_lines = tuple(english_lines_for_render(text))
        styles.append(
            TnTextStyle(
                bbox=bbox,
                font_size_px=font_size,
                color_hex=color_hex,
                placeholder_lines=placeholder_lines,
                layer_name=str(getattr(layer, "name", "text")),
            )
        )
    return styles


def extract_line_styles(target) -> list[TnLineStyle]:
    lines: list[TnLineStyle] = []
    for layer in _iter_type_layers(target):
        for line_style in _line_styles_from_type_layer(layer):
            if line_style.placeholder_text.strip() or any(
                segment.text.strip() for segment in line_style.segments
            ):
                lines.append(line_style)
    return lines


def composite_without_text(target) -> Image.Image:
    image = target.composite(
        layer_filter=lambda layer: getattr(layer, "kind", None) != "type"
    )
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def _offset_line_styles(
    line_styles: list[TnLineStyle],
    offset_x: int,
    offset_y: int,
) -> list[TnLineStyle]:
    if offset_x == 0 and offset_y == 0:
        return line_styles
    translated: list[TnLineStyle] = []
    for style in line_styles:
        left, top, right, bottom = style.bbox
        translated.append(
            replace(
                style,
                bbox=(
                    left - offset_x,
                    top - offset_y,
                    right - offset_x,
                    bottom - offset_y,
                ),
            )
        )
    return translated


def load_template_image(path: Path, matched: ImageSize) -> tuple[Image.Image, list[TnLineStyle]]:
    suffix = path.suffix.casefold()
    if suffix == ".psd":
        target = resolve_psd_target(path, matched)
        offset_x, offset_y = (int(target.bbox[0]), int(target.bbox[1]))
        line_styles = _offset_line_styles(extract_line_styles(target), offset_x, offset_y)
        return composite_without_text(target), line_styles

    with Image.open(path) as image:
        return image.convert("RGB"), []
