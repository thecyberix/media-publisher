from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from media_publisher.events.templates import (
    EVENT_TYPE_BHUTA_SHUDDHI,
    EVENT_TYPE_SURYA_KRIYA,
    EVENT_TYPE_YOGASANA,
    PROGRAMS,
    ProgramTemplate,
    get_program,
    normalize_event_type,
)
from media_publisher.sources.drive_layout import resolve_events_folder_id
from media_publisher.sources.google_drive import (
    DOCX_MIME_TYPE,
    FOLDER_MIME_TYPE,
    GOOGLE_DOC_MIME_TYPE,
    DriveFile,
    GoogleDriveClient,
    GoogleDriveError,
)

TEMPLATE_CACHE_NAME = "hatha-message-template.docx"
ENGLISH_HEADING_RE = re.compile(
    r"(?P<name>Surya Kriya|Bhuta Shuddhi|Yogasanas?|Angamardana)\s+Programme",
    re.IGNORECASE,
)
PROGRAM_NAME_RE = re.compile(
    r'Програма\s+[“"„«]\s*(?P<name>[^”"»“]+?)\s*[”"»“]',
)
LEARN_MORE_URL_RE = re.compile(r"https?://\S+")
QUOTE_ATTRIBUTION_RE = re.compile(r"^[-–—]\s*садгуру\s*$", re.IGNORECASE)
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

EVENT_TYPE_BY_ENGLISH_NAME = {
    "surya kriya": EVENT_TYPE_SURYA_KRIYA,
    "bhuta shuddhi": EVENT_TYPE_BHUTA_SHUDDHI,
    "yogasana": EVENT_TYPE_YOGASANA,
    "yogasanas": EVENT_TYPE_YOGASANA,
    "angamardana": "angamardana",
}


class EventTemplateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParsedProgramCopy:
    event_type: str
    program_name: str
    title_emoji: str
    quote: str
    body: str
    benefits: tuple[str, ...]
    benefit_bullet: str
    learn_more_intro: str
    learn_more_url: str
    learn_more_label: str


def event_template_cache_path(project_root: Path) -> Path:
    return project_root / "downloads" / "event-images" / TEMPLATE_CACHE_NAME


def find_event_template_file(
    drive_client: GoogleDriveClient,
    folder_id: str = "",
) -> DriveFile:
    if not folder_id.strip():
        folder_id = resolve_events_folder_id(drive_client)
    try:
        children = drive_client.list_children(folder_id)
    except GoogleDriveError as exc:
        raise EventTemplateError(
            f"Failed to list event templates in Drive folder {folder_id}: {exc}"
        ) from exc
    docs = [
        item
        for item in children
        if item.mime_type != FOLDER_MIME_TYPE
        and (
            item.mime_type in {GOOGLE_DOC_MIME_TYPE, DOCX_MIME_TYPE}
            or item.name.casefold().endswith(".docx")
        )
    ]
    if not docs:
        raise EventTemplateError(
            f"No Hatha message template (.docx / Google Doc) found in Drive folder {folder_id}"
        )

    def _rank(item: DriveFile) -> tuple[int, int, int, str]:
        name = item.name.casefold()
        return (
            0 if "hatha" in name else 1,
            0 if "template" in name else 1,
            0 if item.mime_type == GOOGLE_DOC_MIME_TYPE else 1,
            name,
        )

    return sorted(docs, key=_rank)[0]


def load_program_from_drive(
    drive_client: GoogleDriveClient,
    event_type: str,
    *,
    project_root: Path,
    folder_id: str = "",
) -> ProgramTemplate:
    base = get_program(event_type)
    copies = load_program_copies_from_drive(
        drive_client,
        project_root=project_root,
        folder_id=folder_id,
    )
    copy = copies.get(base.event_type)
    if copy is None:
        raise EventTemplateError(
            f"Drive template has no Bulgarian copy for {base.event_type!r}"
        )
    return apply_parsed_copy(base, copy)


def load_program_copies_from_drive(
    drive_client: GoogleDriveClient,
    *,
    project_root: Path,
    folder_id: str = "",
) -> dict[str, ParsedProgramCopy]:
    template = find_event_template_file(drive_client, folder_id)
    destination = event_template_cache_path(project_root)
    try:
        drive_client.download_document(template.id, destination)
    except GoogleDriveError as exc:
        raise EventTemplateError(
            f"Failed to download event template {template.name!r}: {exc}"
        ) from exc
    return parse_hatha_template_docx(destination)


def parse_hatha_template_docx(path: Path) -> dict[str, ParsedProgramCopy]:
    try:
        from docx import Document
    except ImportError as exc:
        raise EventTemplateError(
            "Parsing the Hatha template requires python-docx"
        ) from exc
    document = Document(str(path))
    copies: dict[str, ParsedProgramCopy] = {}
    tables = list(document.tables)
    for index, table in enumerate(tables):
        if len(table.rows) != 1 or len(table.columns) != 1:
            continue
        english_cell = table.rows[0].cells[0]
        english_lines = _cell_lines(english_cell)
        heading = next((line for line in english_lines if line.strip()), "")
        event_type = event_type_from_english_heading(heading)
        if event_type is None or event_type not in PROGRAMS:
            continue
        bulgarian_cell = _find_bulgarian_cell(tables[index + 1 : index + 4])
        if bulgarian_cell is None:
            continue
        copies[event_type] = parse_program_copy(
            event_type=event_type,
            english_lines=english_lines,
            bulgarian_lines=_cell_lines(bulgarian_cell),
            youtube_url=(
                _cell_youtube_url(bulgarian_cell) or _cell_youtube_url(english_cell)
            ),
        )
    if not copies:
        raise EventTemplateError("Hatha template did not contain any programme copy")
    return copies


def event_type_from_english_heading(heading: str) -> str | None:
    match = ENGLISH_HEADING_RE.search(heading)
    if match is None:
        return None
    return EVENT_TYPE_BY_ENGLISH_NAME.get(match.group("name").casefold())


def parse_program_copy(
    *,
    event_type: str,
    english_lines: list[str],
    bulgarian_lines: list[str],
    youtube_url: str | None = None,
) -> ParsedProgramCopy:
    english = _parse_language_block(
        english_lines,
        benefits_headings=("benefits", "this programme offers"),
    )
    bulgarian = _parse_language_block(
        bulgarian_lines,
        benefits_headings=("ползи", "те спомагат за"),
    )
    title = next((line for line in bulgarian_lines if line.strip()), "")
    program_name = _program_name_from_title(title)
    if not program_name:
        raise EventTemplateError(
            f"Could not parse Bulgarian programme name for {event_type!r}"
        )
    quote = bulgarian.quote
    learn_more_url = _normalize_youtube_url(
        (youtube_url or "").strip()
        or bulgarian.learn_more_url
        or english.learn_more_url
    )
    if not learn_more_url:
        raise EventTemplateError(
            f"Template is missing a YouTube URL for {event_type!r}"
        )
    if not quote and not bulgarian.body:
        raise EventTemplateError(
            f"Bulgarian template is missing quote/body for {event_type!r}"
        )
    if not bulgarian.benefits:
        raise EventTemplateError(
            f"Bulgarian template is missing benefits for {event_type!r}"
        )
    return ParsedProgramCopy(
        event_type=normalize_event_type(event_type),
        program_name=program_name,
        title_emoji=_title_emoji(title) or _title_emoji(
            next((line for line in english_lines if line.strip()), "")
        ),
        quote=quote,
        body=bulgarian.body,
        benefits=bulgarian.benefits,
        benefit_bullet=bulgarian.benefit_bullet,
        learn_more_intro=bulgarian.learn_more_intro,
        learn_more_url=learn_more_url,
        learn_more_label=bulgarian.learn_more_label,
    )


def apply_parsed_copy(base: ProgramTemplate, copy: ParsedProgramCopy) -> ProgramTemplate:
    return replace(
        base,
        program_name=copy.program_name,
        title_emoji=copy.title_emoji or base.title_emoji,
        quote=copy.quote,
        body=copy.body,
        benefits=copy.benefits,
        benefit_bullet=copy.benefit_bullet or base.benefit_bullet,
        learn_more_intro=copy.learn_more_intro,
        learn_more_url=copy.learn_more_url,
        learn_more_label=copy.learn_more_label,
    )


@dataclass(frozen=True)
class _LanguageBlock:
    quote: str
    body: str
    benefits: tuple[str, ...]
    benefit_bullet: str
    learn_more_intro: str
    learn_more_url: str
    learn_more_label: str


def _parse_language_block(
    lines: list[str],
    *,
    benefits_headings: tuple[str, ...],
) -> _LanguageBlock:
    stripped = [line.strip() for line in lines]
    quote = ""
    body = ""
    benefits: list[str] = []
    benefit_bullet = ""
    learn_more_intro = ""
    learn_more_url = ""
    learn_more_label = ""
    mode = "pre"
    heading_set = {heading.casefold().rstrip(":") for heading in benefits_headings}
    for line in stripped:
        if not line:
            continue
        if line.startswith("🗓") or line.startswith("🗓️"):
            mode = "quote"
            continue
        if line.casefold().rstrip(":") in heading_set:
            mode = "benefits"
            continue
        if _is_learn_more_line(line):
            intro, url, label = _split_learn_more(line)
            learn_more_intro = intro
            learn_more_url = url
            learn_more_label = label
            mode = "done"
            continue
        if line.startswith("👉") or line.startswith("💫"):
            mode = "done"
            continue
        if mode == "quote":
            quote = _clean_quote(line)
            mode = "quote_tail"
            continue
        if mode == "quote_tail" and QUOTE_ATTRIBUTION_RE.match(line):
            quote = f"{quote} {line}".strip()
            mode = "body"
            continue
        if mode in {"quote_tail", "body"} and not body:
            body = line
            mode = "body"
            continue
        if mode == "benefits":
            bullet, text = _split_benefit(line)
            if text:
                if bullet and not benefit_bullet:
                    benefit_bullet = bullet
                benefits.append(text)
    return _LanguageBlock(
        quote=quote,
        body=body,
        benefits=tuple(benefits),
        benefit_bullet=benefit_bullet,
        learn_more_intro=learn_more_intro,
        learn_more_url=learn_more_url,
        learn_more_label=learn_more_label,
    )


def _find_bulgarian_cell(tables):
    for table in tables:
        if not table.rows:
            continue
        header = table.rows[0].cells[0].text.strip().casefold()
        if header != "bulgarian" or len(table.rows) < 2:
            continue
        return table.rows[1].cells[0]
    return None


def _cell_youtube_url(cell) -> str:
    from docx.oxml.ns import qn

    rels = getattr(getattr(cell, "part", None), "rels", None)
    if rels is None:
        return ""
    getter = getattr(rels, "get", None)
    for node in cell._tc.iter():
        if not str(node.tag).endswith("}hyperlink"):
            continue
        rid = node.get(qn("r:id"))
        if not rid:
            continue
        relationship = getter(rid) if callable(getter) else None
        if relationship is None:
            continue
        target = str(getattr(relationship, "target_ref", "") or "")
        if "youtu" in target.casefold():
            return _normalize_youtube_url(target)
    return ""


def _normalize_youtube_url(url: str) -> str:
    cleaned = url.strip()
    for separator in ("?", "#"):
        if separator in cleaned:
            cleaned = cleaned.split(separator, 1)[0]
    return cleaned.rstrip("/")


def _cell_lines(cell) -> list[str]:
    lines: list[str] = []
    for paragraph in cell.paragraphs:
        lines.extend(_paragraph_lines(paragraph))
    return lines


def _paragraph_lines(paragraph) -> list[str]:
    parts: list[str] = [""]
    for node in paragraph._element.iter():
        if node.tag in {f"{W_NS}br", f"{W_NS}cr"}:
            parts.append("")
        elif node.tag == f"{W_NS}t" and node.text:
            parts[-1] += node.text
    if len(parts) == 1 and "\n" in parts[0]:
        return parts[0].splitlines()
    return parts


def _program_name_from_title(title: str) -> str:
    match = PROGRAM_NAME_RE.search(title)
    if match is None:
        return ""
    return match.group("name").strip()


def _title_emoji(title: str) -> str:
    match = re.match(r"^(\S+)\s+Програма", title.strip())
    if match:
        return match.group(1)
    match = re.match(r"^(\S+)\s+", title.strip())
    return match.group(1) if match else ""


def _clean_quote(text: str) -> str:
    return text.strip().strip("_").strip()


def _is_learn_more_line(line: str) -> bool:
    folded = line.casefold()
    return bool(LEARN_MORE_URL_RE.search(line)) or folded.startswith(
        ("вижте", "watch", "научете", "learn more", "find out")
    )


def _split_learn_more(line: str) -> tuple[str, str, str]:
    url_match = LEARN_MORE_URL_RE.search(line)
    url = url_match.group(0).rstrip(".,)") if url_match else ""
    remainder = line
    if url:
        remainder = (line[: url_match.start()] + line[url_match.end() :]).strip()
    intro, _, label = remainder.partition(":")
    intro = intro.strip()
    if intro and not intro.endswith(":"):
        intro = f"{intro}:"
    return intro, url, label.strip()


def _split_benefit(line: str) -> tuple[str, str]:
    parts = line.strip().split(None, 1)
    if len(parts) == 2 and not parts[0][:1].isalnum():
        return parts[0], parts[1]
    return "", line.strip()
