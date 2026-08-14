from __future__ import annotations

import re
from typing import Iterator

from media_publisher.sources.tn_docx import english_lines_for_render
from media_publisher.sources.tn_psd import TnLineStyle, TnTextSegment


def _similar_font_size(left: float, right: float) -> bool:
    return abs(left - right) / max(left, right, 1.0) <= 0.15


def _merge_line_group(group: list[TnLineStyle]) -> TnLineStyle:
    first = group[0]
    last = group[-1]
    merged_text = " ".join(style.placeholder_text.strip() for style in group)
    merged_bbox = (
        min(style.bbox[0] for style in group),
        min(style.bbox[1] for style in group),
        max(style.bbox[2] for style in group),
        max(style.bbox[3] for style in group),
    )
    max_size = max(style.font_size_px for style in group)
    return TnLineStyle(
        placeholder_text=merged_text,
        rendered_text=merged_text,
        bbox=merged_bbox,
        font_size_px=max_size,
        color_hex=first.color_hex,
        font_index=first.font_index,
        layer_name=first.layer_name,
        alignment=first.alignment,
        faux_bold=any(style.faux_bold for style in group),
        segments=(
            TnTextSegment(
                text=merged_text,
                font_size_px=max_size,
                color_hex=first.color_hex,
                font_index=first.font_index,
                faux_bold=any(style.faux_bold for style in group),
            ),
        ),
    )


def _is_mahashiv_layout(line_styles: list[TnLineStyle]) -> bool:
    placeholders = {style.placeholder_text.casefold().strip() for style in line_styles}
    return (
        any("mystical" in placeholder for placeholder in placeholders)
        and "magical" in placeholders
        and "night" in placeholders
    )


def consolidate_line_styles(line_styles: list[TnLineStyle]) -> list[TnLineStyle]:
    """Merge long runs of similarly styled PSD lines (e.g. Consciousness title block)."""
    if _is_consciousness_six_line_layout(line_styles):
        return line_styles
    if _is_mahashiv_layout(line_styles):
        return line_styles
    if len(line_styles) < 3:
        return line_styles

    groups: list[list[TnLineStyle]] = [[line_styles[0]]]
    for style in line_styles[1:]:
        previous = groups[-1][-1]
        if (
            style.color_hex.lower() == previous.color_hex.lower()
            and _similar_font_size(style.font_size_px, previous.font_size_px)
        ):
            groups[-1].append(style)
        else:
            groups.append([style])

    result: list[TnLineStyle] = []
    for group in groups:
        if len(group) >= 3:
            result.append(_merge_line_group(group))
        else:
            result.extend(group)
    return result


def _has_professor_prefix(text: str) -> bool:
    return bool(
        re.match(r"^(?:Prof\.|Professor|Проф\.|проф\.)\s*", text.strip(), re.IGNORECASE)
    )


def _preserve_placeholder_prefix(rendered: str, placeholder: str) -> str:
    rendered = rendered.strip()
    placeholder = placeholder.strip()
    if placeholder.startswith("Prof.") and not rendered.startswith("Prof."):
        if _has_professor_prefix(rendered):
            return rendered
        return f"Prof. {rendered.removeprefix('Prof.').strip()}"
    return rendered


def _merge_english_for_two_blocks(english_lines: list[str]) -> list[str] | None:
    question_index = next(
        (index for index, line in enumerate(english_lines) if "?" in line),
        None,
    )
    if question_index is None:
        return None
    title = " ".join(line.strip() for line in english_lines[: question_index + 1])
    rest = " ".join(line.strip() for line in english_lines[question_index + 1 :])
    return [title, rest]


def _placeholder_word_counts(lines: tuple[str, ...]) -> list[int]:
    return [max(1, len(line.split())) for line in lines]


def _segment_word_counts(segments: tuple[TnTextSegment, ...]) -> list[int]:
    return [max(1, len(segment.text.split())) for segment in segments]


def map_english_to_placeholder_lines(
    english: str,
    placeholder_lines: tuple[str, ...],
) -> list[str]:
    """Map English TN text onto the same line count as the PSD placeholder."""
    target_count = len(placeholder_lines)
    if target_count == 0:
        return english_lines_for_render(english)

    english_lines = english_lines_for_render(english)
    if len(english_lines) == target_count:
        return english_lines

    if _is_consciousness_six_line_placeholders(placeholder_lines) and len(english_lines) == 5:
        return _map_consciousness_five_to_six(english_lines, placeholder_lines)

    if target_count == 2 and len(english_lines) == 4:
        return [
            " ".join(line.strip() for line in english_lines[:2]),
            " ".join(line.strip() for line in english_lines[2:]),
        ]

    if target_count == 2 and len(english_lines) > target_count:
        merged_blocks = _merge_english_for_two_blocks(english_lines)
        if merged_blocks is not None:
            return merged_blocks

    if len(english_lines) == 1:
        words = english_lines[0].split()
        if not words:
            return [""] * target_count
        weights = _placeholder_word_counts(placeholder_lines)
        total_weight = sum(weights)
        mapped: list[str] = []
        cursor = 0
        for index, weight in enumerate(weights):
            if index == len(weights) - 1:
                mapped.append(" ".join(words[cursor:]))
                break
            share = max(1, round(len(words) * weight / total_weight))
            mapped.append(" ".join(words[cursor : cursor + share]))
            cursor += share
        return mapped

    if len(english_lines) > target_count:
        merged = list(english_lines)
        while len(merged) > target_count:
            shortest_index = min(
                range(len(merged) - 1),
                key=lambda index: len(merged[index]) + len(merged[index + 1]),
            )
            merged[shortest_index] = f"{merged[shortest_index]} {merged[shortest_index + 1]}".strip()
            del merged[shortest_index + 1]
        return merged

    expanded = list(english_lines)
    while len(expanded) < target_count:
        longest_index = max(range(len(expanded)), key=lambda index: len(expanded[index]))
        words = expanded[longest_index].split()
        if len(words) < 2:
            expanded.insert(longest_index + 1, "")
            continue
        split_at = max(1, len(words) // 2)
        expanded[longest_index] = " ".join(words[:split_at])
        expanded.insert(longest_index + 1, " ".join(words[split_at:]))
    return expanded


def _map_line_to_segments(
    english_line: str,
    placeholder_segments: tuple[TnTextSegment, ...],
) -> tuple[TnTextSegment, ...]:
    if not placeholder_segments:
        return ()

    if len(placeholder_segments) == 1:
        segment = placeholder_segments[0]
        return (
            TnTextSegment(
                text=english_line,
                font_size_px=segment.font_size_px,
                color_hex=segment.color_hex,
                font_index=segment.font_index,
                faux_bold=segment.faux_bold,
            ),
        )

    words = english_line.split()
    if not words:
        return tuple(
            TnTextSegment(
                text="",
                font_size_px=segment.font_size_px,
                color_hex=segment.color_hex,
                font_index=segment.font_index,
                faux_bold=segment.faux_bold,
            )
            for segment in placeholder_segments
        )

    weights = _segment_word_counts(placeholder_segments)
    total_weight = sum(weights)
    mapped: list[TnTextSegment] = []
    cursor = 0
    for index, segment in enumerate(placeholder_segments):
        if index == len(placeholder_segments) - 1:
            chunk_words = words[cursor:]
        else:
            share = max(1, round(len(words) * weights[index] / total_weight))
            chunk_words = words[cursor : cursor + share]
            cursor += share
        text = " ".join(chunk_words)
        if segment.text.startswith(" ") and text and not text.startswith(" "):
            text = " " + text
        if segment.text.endswith(" ") and text and not text.endswith(" "):
            text = text + " "
        mapped.append(
            TnTextSegment(
                text=text,
                font_size_px=segment.font_size_px,
                color_hex=segment.color_hex,
                font_index=segment.font_index,
                faux_bold=segment.faux_bold,
            )
        )
    return tuple(mapped)


def _merge_pair_styles(first: TnLineStyle, second: TnLineStyle) -> TnLineStyle:
    merged_text = f"{first.placeholder_text} {second.placeholder_text}".strip()
    merged_bbox = (
        min(first.bbox[0], second.bbox[0]),
        min(first.bbox[1], second.bbox[1]),
        max(first.bbox[2], second.bbox[2]),
        max(first.bbox[3], second.bbox[3]),
    )
    segments = tuple(first.segments) + tuple(second.segments)
    if not segments:
        segments = (
            TnTextSegment(
                text=merged_text,
                font_size_px=max(first.font_size_px, second.font_size_px),
                color_hex=first.color_hex,
                font_index=first.font_index,
                faux_bold=first.faux_bold or second.faux_bold,
            ),
        )
    return TnLineStyle(
        placeholder_text=merged_text,
        rendered_text=merged_text,
        bbox=merged_bbox,
        font_size_px=max(first.font_size_px, second.font_size_px),
        color_hex=first.color_hex,
        font_index=first.font_index,
        layer_name=first.layer_name,
        alignment=first.alignment,
        faux_bold=first.faux_bold or second.faux_bold,
        segments=segments,
        max_grow_factor=first.max_grow_factor,
    )


def _is_farm_layout(line_styles: list[TnLineStyle]) -> bool:
    if len(line_styles) != 4:
        return False
    head = " ".join(style.placeholder_text.casefold() for style in line_styles[:2])
    return "spot" in head


def _is_krishna_layout(line_styles: list[TnLineStyle]) -> bool:
    if len(line_styles) != 2:
        return False
    return "krishna" in line_styles[0].placeholder_text.casefold()


def _is_kailash_title(title: str) -> bool:
    lowered = title.casefold()
    return "kailash" in lowered and "rapid-fire" in lowered


def _is_kailash_layout(line_styles: list[TnLineStyle]) -> bool:
    if len(line_styles) != 2:
        return False
    joined = " ".join(style.placeholder_text for style in line_styles).casefold()
    return "rapid-fire" in joined and "ocean or" in joined


def kailash_template_line_styles(width: int, height: int) -> list[TnLineStyle]:
    """Layout for the flat JPG Kailash template (no PSD text layers)."""
    text_left = int(width * 0.40)
    text_right = width - int(width * 0.02)
    title_size = height * 0.100
    subtitle_size = height * 0.130
    return [
        TnLineStyle(
            placeholder_text="Rapid-Fire with Sadhguru",
            rendered_text="Rapid-Fire with Sadhguru",
            bbox=(text_left, int(height * 0.04), text_right, int(height * 0.36)),
            font_size_px=title_size,
            color_hex="#FEEEA2",
            alignment="right",
            faux_bold=True,
            block_line_parts=("Rapid-Fire", "with Sadhguru"),
            stacked_line_gap_factor=0.20,
            stacked_line_backgrounds=(None, "#FEEEA2"),
            stacked_line_match_widths=True,
            segments=(
                TnTextSegment(
                    text="Rapid-Fire",
                    font_size_px=title_size,
                    color_hex="#FEEEA2",
                    faux_bold=True,
                ),
                TnTextSegment(
                    text="with Sadhguru",
                    font_size_px=title_size * 0.72,
                    color_hex="#1A4731",
                    faux_bold=True,
                ),
            ),
        ),
        TnLineStyle(
            placeholder_text="Ocean or Mountains?",
            rendered_text="Ocean or Mountains?",
            bbox=(text_left, int(height * 0.34), text_right, int(height * 0.58)),
            font_size_px=subtitle_size,
            color_hex="#FFFFFF",
            alignment="right",
            faux_bold=True,
            block_line_parts=("Ocean or", "Mountains?"),
            stacked_line_gap_factor=0.06,
        ),
    ]


def _is_shiva_layout(line_styles: list[TnLineStyle]) -> bool:
    if len(line_styles) != 4:
        return False
    return "does" in line_styles[0].placeholder_text.casefold()


def _is_past_layout(line_styles: list[TnLineStyle]) -> bool:
    if len(line_styles) != 2:
        return False
    return line_styles[0].alignment == "left"


def _is_adolescence_layout(line_styles: list[TnLineStyle]) -> bool:
    if len(line_styles) != 3:
        return False
    joined = " ".join(style.placeholder_text.casefold() for style in line_styles)
    return "teenagers" in joined or "happening" in joined


def prepare_layout_line_styles(line_styles: list[TnLineStyle]) -> list[TnLineStyle]:
    if _is_farm_layout(line_styles):
        return [
            _merge_pair_styles(line_styles[0], line_styles[1]),
            _merge_pair_styles(line_styles[2], line_styles[3]),
        ]
    if _is_krishna_layout(line_styles):
        first, second = line_styles
        first_boost = first.font_size_px * 1.55
        second_boost = second.font_size_px * 1.35
        first_segments = tuple(
            TnTextSegment(
                text=segment.text,
                font_size_px=first_boost,
                color_hex=segment.color_hex,
                font_index=segment.font_index,
                faux_bold=segment.faux_bold,
            )
            for segment in (first.segments or ())
        ) or first.segments
        second_segments = tuple(
            TnTextSegment(
                text=segment.text,
                font_size_px=second_boost,
                color_hex=segment.color_hex,
                font_index=segment.font_index,
                faux_bold=segment.faux_bold,
            )
            for segment in (second.segments or ())
        ) or second.segments
        return [
            TnLineStyle(
                placeholder_text=first.placeholder_text,
                rendered_text=first.rendered_text,
                bbox=first.bbox,
                font_size_px=first_boost,
                color_hex=first.color_hex,
                font_index=first.font_index,
                layer_name=first.layer_name,
                alignment=first.alignment,
                faux_bold=first.faux_bold,
                segments=first_segments,
            ),
            TnLineStyle(
                placeholder_text=second.placeholder_text,
                rendered_text=second.rendered_text,
                bbox=second.bbox,
                font_size_px=second_boost,
                color_hex=second.color_hex,
                font_index=second.font_index,
                layer_name=second.layer_name,
                alignment=second.alignment,
                faux_bold=second.faux_bold,
                segments=second_segments,
            ),
        ]
    return line_styles


def _format_farm_title(text: str) -> str:
    parts = text.split()
    if not parts:
        return text
    if parts[0].casefold().rstrip(".,") == "spot":
        parts[0] = "SPOT"
    for index, part in enumerate(parts):
        if part.casefold().rstrip(".,") == "difference":
            parts[index] = "Difference"
    return " ".join(parts)


def _format_krishna_text(text: str) -> str:
    return text.upper()


def _map_farm_title_segments(
    title: str,
    placeholder_segments: tuple[TnTextSegment, ...],
) -> tuple[TnTextSegment, ...]:
    words = title.split()
    if not words or not placeholder_segments:
        return placeholder_segments

    gold_segments = [
        segment
        for segment in placeholder_segments
        if segment.color_hex.lower() not in {"#ffffff", "#fffeff"}
    ]
    white_segments = [
        segment
        for segment in placeholder_segments
        if segment.color_hex.lower() in {"#ffffff", "#fffeff"}
    ]
    spot_segment = gold_segments[0] if gold_segments else placeholder_segments[0]
    the_segment = white_segments[0] if white_segments else spot_segment
    diff_segment = white_segments[-1] if white_segments else placeholder_segments[-1]

    difference_index = next(
        (index for index, word in enumerate(words) if word.casefold() == "difference"),
        len(words) - 1,
    )
    middle = " ".join(words[1:difference_index])
    difference = " ".join(words[difference_index:])

    mapped: list[TnTextSegment] = [
        TnTextSegment(
            text=f"{words[0]} ",
            font_size_px=spot_segment.font_size_px,
            color_hex=spot_segment.color_hex,
            font_index=spot_segment.font_index,
            faux_bold=True,
        )
    ]
    if middle:
        mapped.append(
            TnTextSegment(
                text=f"{middle} ",
                font_size_px=the_segment.font_size_px,
                color_hex=the_segment.color_hex,
                font_index=the_segment.font_index,
                faux_bold=True,
            )
        )
    mapped.append(
        TnTextSegment(
            text=difference,
            font_size_px=diff_segment.font_size_px,
            color_hex=diff_segment.color_hex,
            font_index=diff_segment.font_index,
            faux_bold=True,
        )
    )
    return tuple(mapped)


def _is_inspiring_layout(line_styles: list[TnLineStyle]) -> bool:
    return len(line_styles) == 2 and all(style.font_size_px >= 85 for style in line_styles)


def _is_consciousness_six_line_layout(line_styles: list[TnLineStyle]) -> bool:
    if len(line_styles) != 6:
        return False
    joined = " ".join(style.placeholder_text for style in line_styles).casefold()
    return "miracle" in joined and "pinker" in joined


def _is_consciousness_six_line_placeholders(placeholder_lines: tuple[str, ...]) -> bool:
    if len(placeholder_lines) != 6:
        return False
    joined = " ".join(placeholder_lines).casefold()
    return "miracle" in joined and "pinker" in joined


def _consciousness_prof_line(person_line: str, placeholder: str) -> str:
    person_line = person_line.strip()
    placeholder = placeholder.strip()
    if placeholder.startswith("Prof."):
        first_name = person_line.split()[0] if person_line.split() else "Steven"
        return f"Prof. {first_name}"
    return person_line


def _consciousness_five_line_is_english_split(lines: list[str]) -> bool:
    if len(lines) < 5:
        return False
    return lines[4].strip().startswith("&")


def _map_consciousness_five_to_six(
    english_lines: list[str],
    placeholder_lines: tuple[str, ...],
) -> list[str]:
    if not _consciousness_five_line_is_english_split(english_lines):
        # Translated captions use 2 title lines + 3 credit lines (no "& Sadhguru" split).
        return [
            english_lines[0].strip(),
            english_lines[1].strip(),
            "",
            english_lines[2].strip(),
            english_lines[3].strip(),
            english_lines[4].strip(),
        ]

    tail = english_lines[4].strip().lstrip("&").strip()
    return [
        english_lines[0].strip(),
        english_lines[1].strip(),
        english_lines[2].strip(),
        _consciousness_prof_line(english_lines[3], placeholder_lines[3]),
        placeholder_lines[4].strip(),
        tail,
    ]


def _is_consciousness_layout(line_styles: list[TnLineStyle]) -> bool:
    if _is_consciousness_six_line_layout(line_styles):
        return True
    if len(line_styles) != 2:
        return False
    return any("miracle" in style.placeholder_text.casefold() for style in line_styles)


def _consciousness_title_parts(text: str) -> tuple[str, ...]:
    words = text.split()
    if len(words) >= 3:
        return (words[0], words[1], " ".join(words[2:]))
    if len(words) == 2:
        return (words[0], words[1])
    cleaned = text.strip()
    return (cleaned,) if cleaned else ("",)


def _consciousness_subtitle_parts(text: str) -> tuple[str, ...]:
    cleaned = text.strip()
    if " & " not in cleaned:
        return (cleaned,) if cleaned else ("",)
    head, tail = cleaned.split(" & ", 1)
    head = head.strip()
    tail = tail.strip()
    head_words = head.split()
    if len(head_words) >= 3 and head_words[0].casefold().rstrip(".") == "prof":
        return ("Prof. Steven", "Pinker &", tail)
    if len(head_words) >= 2:
        return (" ".join(head_words[:-1]), f"{head_words[-1]} &", tail)
    return (head, f"& {tail}")


def _consciousness_block_parts(
    rendered: str,
    placeholder: str,
) -> tuple[str, ...]:
    if "miracle" in placeholder.casefold():
        return _consciousness_title_parts(rendered)
    return _consciousness_subtitle_parts(rendered)


def _scale_style(style: TnLineStyle, factor: float) -> TnLineStyle:
    if factor == 1.0:
        return style
    scaled_segments = style.segments or ()
    if scaled_segments:
        scaled_segments = tuple(
            TnTextSegment(
                text=segment.text,
                font_size_px=segment.font_size_px * factor,
                color_hex=segment.color_hex,
                font_index=segment.font_index,
                faux_bold=segment.faux_bold,
            )
            for segment in scaled_segments
        )
    return TnLineStyle(
        placeholder_text=style.placeholder_text,
        rendered_text=style.rendered_text,
        bbox=style.bbox,
        font_size_px=style.font_size_px * factor,
        color_hex=style.color_hex,
        font_index=style.font_index,
        layer_name=style.layer_name,
        alignment=style.alignment,
        faux_bold=style.faux_bold,
        segments=scaled_segments,
        max_grow_factor=style.max_grow_factor,
        allow_auto_bold=style.allow_auto_bold,
        block_line_parts=style.block_line_parts,
        stacked_line_gap_factor=style.stacked_line_gap_factor,
        fixed_font_size_px=style.fixed_font_size_px,
        stacked_line_backgrounds=style.stacked_line_backgrounds,
        stacked_line_match_widths=style.stacked_line_match_widths,
        stacked_line_font_sizes=style.stacked_line_font_sizes,
    )


def _with_bold(style: TnLineStyle, *, bold: bool) -> TnLineStyle:
    segments = style.segments or ()
    if not segments:
        segments = (
            TnTextSegment(
                text=style.rendered_text,
                font_size_px=style.font_size_px,
                color_hex=style.color_hex,
                font_index=style.font_index,
                faux_bold=bold,
            ),
        )
    else:
        segments = tuple(
            TnTextSegment(
                text=segment.text,
                font_size_px=segment.font_size_px,
                color_hex=segment.color_hex,
                font_index=segment.font_index,
                faux_bold=bold,
            )
            for segment in segments
        )
    return TnLineStyle(
        placeholder_text=style.placeholder_text,
        rendered_text=style.rendered_text,
        bbox=style.bbox,
        font_size_px=style.font_size_px,
        color_hex=style.color_hex,
        font_index=style.font_index,
        layer_name=style.layer_name,
        alignment=style.alignment,
        faux_bold=bold,
        segments=segments,
        max_grow_factor=style.max_grow_factor,
        allow_auto_bold=style.allow_auto_bold,
        block_line_parts=style.block_line_parts,
        stacked_line_gap_factor=style.stacked_line_gap_factor,
        fixed_font_size_px=style.fixed_font_size_px,
        stacked_line_backgrounds=style.stacked_line_backgrounds,
        stacked_line_match_widths=style.stacked_line_match_widths,
        stacked_line_font_sizes=style.stacked_line_font_sizes,
    )


def apply_typography_preferences(
    line_styles: list[TnLineStyle],
    *,
    farm_layout: bool = False,
    krishna_layout: bool = False,
    inspiring_layout: bool = False,
    consciousness_layout: bool = False,
    shiva_layout: bool = False,
    past_layout: bool = False,
    adolescence_layout: bool = False,
    kailash_layout: bool = False,
) -> list[TnLineStyle]:
    consciousness_six_line = consciousness_layout and len(line_styles) == 6
    styled: list[TnLineStyle] = []
    for index, style in enumerate(line_styles):
        rendered = style.rendered_text
        segments = style.segments

        if farm_layout:
            if index == 0:
                rendered = _format_farm_title(rendered)
                segments = _map_farm_title_segments(rendered, style.segments)
                styled.append(
                    _scale_style(
                        _with_bold(
                            TnLineStyle(
                                placeholder_text=style.placeholder_text,
                                rendered_text=rendered,
                                bbox=style.bbox,
                                font_size_px=style.font_size_px,
                                color_hex=style.color_hex,
                                font_index=style.font_index,
                                layer_name=style.layer_name,
                                alignment=style.alignment,
                                faux_bold=True,
                                segments=segments,
                                max_grow_factor=1.32,
                                block_line_parts=("SPOT the", "Difference"),
                                stacked_line_gap_factor=0.28,
                            ),
                            bold=True,
                        ),
                        2.48,
                    )
                )
                continue
            rendered = rendered.strip()
            segments = (
                TnTextSegment(
                    text=rendered,
                    font_size_px=style.font_size_px,
                    color_hex=style.color_hex,
                    font_index=style.font_index,
                    faux_bold=False,
                ),
            )
            styled.append(
                _scale_style(
                    TnLineStyle(
                        placeholder_text=style.placeholder_text,
                        rendered_text=rendered,
                        bbox=style.bbox,
                        font_size_px=style.font_size_px,
                        color_hex=style.color_hex,
                        font_index=style.font_index,
                        layer_name=style.layer_name,
                        alignment=style.alignment,
                        faux_bold=False,
                        segments=segments,
                        max_grow_factor=1.06,
                        block_line_parts=("Ian Somerhalder", "with Sadhguru"),
                    ),
                    3.15,
                )
            )
            continue

        if kailash_layout:
            block_parts = style.block_line_parts
            if index == 0 and block_parts and len(block_parts) >= 2:
                segments = (
                    TnTextSegment(
                        text=block_parts[0],
                        font_size_px=style.font_size_px,
                        color_hex="#FEEEA2",
                        faux_bold=True,
                    ),
                    TnTextSegment(
                        text=block_parts[1],
                        font_size_px=style.font_size_px * 0.72,
                        color_hex="#1A4731",
                        faux_bold=True,
                    ),
                )
            styled.append(
                _scale_style(
                    _with_bold(
                        TnLineStyle(
                            placeholder_text=style.placeholder_text,
                            rendered_text=rendered,
                            bbox=style.bbox,
                            font_size_px=style.font_size_px,
                            color_hex=style.color_hex,
                            font_index=style.font_index,
                            layer_name=style.layer_name,
                            alignment=style.alignment,
                            faux_bold=True,
                            segments=segments,
                            max_grow_factor=1.28 if index == 0 else 1.26,
                            block_line_parts=block_parts,
                            stacked_line_gap_factor=style.stacked_line_gap_factor,
                            stacked_line_backgrounds=(
                                (None, "#FEEEA2")
                                if index == 0
                                else style.stacked_line_backgrounds
                            ),
                            stacked_line_match_widths=(
                                index == 0 or style.stacked_line_match_widths
                            ),
                        ),
                        bold=True,
                    ),
                    1.32,
                )
            )
            continue

        if krishna_layout:
            rendered = _format_krishna_text(rendered)
            line_scale = 1.75 if index == 0 else 2.05
            segments = (
                TnTextSegment(
                    text=rendered,
                    font_size_px=style.font_size_px,
                    color_hex=style.color_hex,
                    font_index=style.font_index,
                    faux_bold=False,
                ),
            )
            styled.append(
                _scale_style(
                    _with_bold(
                        TnLineStyle(
                            placeholder_text=style.placeholder_text,
                            rendered_text=rendered,
                            bbox=style.bbox,
                            font_size_px=style.font_size_px,
                            color_hex=style.color_hex,
                            font_index=style.font_index,
                            layer_name=style.layer_name,
                            alignment=style.alignment,
                            faux_bold=False,
                            segments=segments,
                            max_grow_factor=1.05 if index == 0 else 1.22,
                            allow_auto_bold=False,
                        ),
                        bold=False,
                    ),
                    line_scale,
                )
            )
            continue

        if inspiring_layout:
            styled.append(_scale_style(_with_bold(style, bold=True), 1.32))
            continue

        if adolescence_layout:
            line_scales = (1.38, 1.05, 1.38)
            grow_factors = (1.08, 1.04, 1.08)
            styled.append(
                _scale_style(
                    _with_bold(
                        TnLineStyle(
                            placeholder_text=style.placeholder_text,
                            rendered_text=style.rendered_text,
                            bbox=style.bbox,
                            font_size_px=style.font_size_px,
                            color_hex=style.color_hex,
                            font_index=style.font_index,
                            layer_name=style.layer_name,
                            alignment=style.alignment,
                            faux_bold=True,
                            segments=style.segments,
                            max_grow_factor=grow_factors[index],
                        ),
                        bold=True,
                    ),
                    line_scales[index],
                )
            )
            continue

        if shiva_layout:
            grow_factors = (1.0, 1.06, 1.06, 1.0)
            styled.append(
                _scale_style(
                    _with_bold(
                        TnLineStyle(
                            placeholder_text=style.placeholder_text,
                            rendered_text=style.rendered_text,
                            bbox=style.bbox,
                            font_size_px=style.font_size_px,
                            color_hex=style.color_hex,
                            font_index=style.font_index,
                            layer_name=style.layer_name,
                            alignment=style.alignment,
                            faux_bold=True,
                            segments=style.segments,
                            max_grow_factor=grow_factors[index],
                        ),
                        bold=True,
                    ),
                    1.0,
                )
            )
            continue

        if past_layout:
            styled.append(
                _scale_style(
                    _with_bold(
                        TnLineStyle(
                            placeholder_text=style.placeholder_text,
                            rendered_text=rendered,
                            bbox=style.bbox,
                            font_size_px=style.font_size_px,
                            color_hex=style.color_hex,
                            font_index=style.font_index,
                            layer_name=style.layer_name,
                            alignment=style.alignment,
                            faux_bold=style.faux_bold,
                            segments=segments,
                            max_grow_factor=1.02,
                            fixed_font_size_px=style.fixed_font_size_px,
                            stacked_line_backgrounds=style.stacked_line_backgrounds,
                        ),
                        bold=True,
                    ),
                    1.0,
                )
            )
            continue

        if consciousness_layout:
            if consciousness_six_line:
                styled.append(
                    _scale_style(
                        _with_bold(
                            TnLineStyle(
                                placeholder_text=style.placeholder_text,
                                rendered_text=style.rendered_text,
                                bbox=style.bbox,
                                font_size_px=style.font_size_px,
                                color_hex=style.color_hex,
                                font_index=style.font_index,
                                layer_name=style.layer_name,
                                alignment=style.alignment,
                                faux_bold=True,
                                segments=style.segments,
                                max_grow_factor=1.10,
                            ),
                            bold=True,
                        ),
                        1.78,
                    )
                )
                continue
            block_parts = _consciousness_block_parts(
                style.rendered_text,
                style.placeholder_text,
            )
            styled.append(
                _scale_style(
                    _with_bold(
                        TnLineStyle(
                            placeholder_text=style.placeholder_text,
                            rendered_text=style.rendered_text,
                            bbox=style.bbox,
                            font_size_px=style.font_size_px,
                            color_hex=style.color_hex,
                            font_index=style.font_index,
                            layer_name=style.layer_name,
                            alignment=style.alignment,
                            faux_bold=True,
                            segments=style.segments,
                            max_grow_factor=1.10,
                            block_line_parts=block_parts,
                            stacked_line_gap_factor=0.14,
                        ),
                        bold=True,
                    ),
                    1.78,
                )
            )
            continue

        styled.append(
            _scale_style(
                _with_bold(style, bold=True),
                1.14,
            )
        )
    return styled


def flatten_placeholder_lines(line_styles: list[TnLineStyle]) -> tuple[str, ...]:
    return tuple(style.placeholder_text for style in line_styles)


def assign_english_to_line_styles(
    english: str,
    line_styles: list[TnLineStyle],
) -> list[TnLineStyle]:
    if not line_styles:
        return []

    placeholders = flatten_placeholder_lines(line_styles)
    english_lines = english_lines_for_render(english)
    mapped_lines = map_english_to_placeholder_lines(english, placeholders)
    block_parts_by_index: list[tuple[str, ...]] | None = None
    if len(line_styles) == 2 and len(english_lines) == 4:
        block_parts_by_index = [
            (english_lines[0].strip(), english_lines[1].strip()),
            (english_lines[2].strip(), english_lines[3].strip()),
        ]
    assigned: list[TnLineStyle] = []
    for index, (style, rendered) in enumerate(zip(line_styles, mapped_lines, strict=False)):
        rendered = _preserve_placeholder_prefix(rendered, style.placeholder_text)
        mapped_segments = _map_line_to_segments(rendered, style.segments)
        block_line_parts = style.block_line_parts
        if block_parts_by_index is not None and index < len(block_parts_by_index):
            block_line_parts = block_parts_by_index[index]
        assigned.append(
            TnLineStyle(
                placeholder_text=style.placeholder_text,
                rendered_text=rendered,
                bbox=style.bbox,
                font_size_px=style.font_size_px,
                color_hex=style.color_hex,
                font_index=style.font_index,
                layer_name=style.layer_name,
                alignment=style.alignment,
                faux_bold=style.faux_bold,
                segments=mapped_segments,
                max_grow_factor=style.max_grow_factor,
                allow_auto_bold=style.allow_auto_bold,
                block_line_parts=block_line_parts,
                stacked_line_gap_factor=style.stacked_line_gap_factor,
                fixed_font_size_px=style.fixed_font_size_px,
                stacked_line_backgrounds=style.stacked_line_backgrounds,
                stacked_line_match_widths=style.stacked_line_match_widths,
                stacked_line_font_sizes=style.stacked_line_font_sizes,
            )
        )
    return assigned


def iter_render_lines(line_styles: list[TnLineStyle]) -> Iterator[TnLineStyle]:
    for style in line_styles:
        if style.rendered_text.strip():
            yield style
