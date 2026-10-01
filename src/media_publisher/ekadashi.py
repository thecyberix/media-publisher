"""Ekadashi dates (Isha Google Calendar) and hard-coded Bulgarian captions.

Dates come from the public Isha calendar ICS feed
(``ishacalendar@gmail.com``). Captions are taken from the SadhguruBulgarian
Page posts for 2025 (same chronological slot within the year).
"""
from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Sequence

DEFAULT_ISHA_CALENDAR_ID = "ishacalendar@gmail.com"
ISHA_CALENDAR_EMBED_URL = (
    "https://calendar.google.com/calendar/embed"
    f"?src={urllib.parse.quote(DEFAULT_ISHA_CALENDAR_ID)}&ctz=Europe%2FSofia"
)
CANVA_EKADASHI_FOLDER_ID = "FAHWrBd1fUE"
CANVA_EKADASHI_FOLDER_URL = f"https://www.canva.com/folder/{CANVA_EKADASHI_FOLDER_ID}"

_USER_AGENT = "media-publisher/ekadashi-calendar"
_SUMMARY_RE = re.compile(r"^SUMMARY(?:;[^:]*)?:(.*)$", re.MULTILINE)
_DTSTART_DATE_RE = re.compile(
    r"^DTSTART(?:;VALUE=DATE)?(?:;[^:]*)?:(\d{8})(?:T\d{6}Z?)?$",
    re.MULTILINE,
)


class EkadashiCalendarError(RuntimeError):
    pass


@dataclass(frozen=True)
class CalendarEkadashi:
    event_date: date
    summary: str


@dataclass(frozen=True)
class EkadashiEntry:
    """One Ekadashi occurrence."""

    year: int
    month: int
    day: int
    name: str
    caption: str
    page_number: int  # 1-based Canva page in ``Ekadashi {year}``

    @property
    def event_date(self) -> date:
        return date(self.year, self.month, self.day)

    @property
    def stem(self) -> str:
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"


# Captions from Facebook Page posts in 2025, chronological order (24 slots).
# Source: SadhguruBulgarian feed, 2025-01-09 … 2025-12-29.
CAPTIONS_FROM_2025: tuple[str, ...] = (
    "Вайкунта Екадаши се смята за най-важния Екадаши през годината. Въпреки че спазването на пост по време на всяко екадаши е благоприятно за храносмилателната ни система и прави чудеса за цялостното ни благосъстояние, спазването на целодневен пост на това екадаши е от голямо духовно значение.\nЩе постите ли на този Вайкунта Екадаши? \n#Екадаши",
    "Екадаши, 11 дни след новолунието или пълнолунието, е подходящ ден за постене. След вечерята тази вечер постете до следващата. Използвайте Екадаши, за да пречистите организма си и да превърнете храненето в по-осъзнат процес. #Екадаши",
    "На Екадаши, единадесетия ден след новолунието или пълнолунието, се препоръчва да не се яде. Тази практика пречиства тялото, повишава бдителността и дава възможност за самоанализ. Ако целодневният пост е предизвикателство, помислете за плодове вместо това. #Екадаши",
    "Екадаши е ден, в който тялото ни естествено се нуждае от по-малко храна. Постът на този ден пречиства организма и насочва съзнанието ни навътре. След днешната вечеря постете до следващата. Ако пълното гладуване не е възможно, лека плодова диета също е полезна. #Екадаши",
    "Два пъти месечно има определени дни, в които организмът не изисква храна. Постенето в тези дни пречиства организма и повишава осъзнаването на връзката на тялото с храната и земята. Ако гладуването за цял ден ви се струва предизвикателство, изберете лека диета, основана на плодове. #Екадаши",
    "Екадаши, 11 дни след новолунието или пълнолунието, е идеален ден за пост. След днешната вечеря постете до следващата. Използвайте Екадаши, за да пречистите организма си и да развиете по-осъзнато отношение към храната. #Екадаши",
    "Два пъти месечно има определени дни, в които тялото не се нуждае от храна. Постенето през тези дни пречиства организма и дава възможност да се осъзнае връзката на тялото с храната и земята. Ако ви е трудно да постите цял ден, вместо това яжте лека плодова храна. #Екадаши",
    "Екадаши е ден, в който тялото не се нуждае от храна. Постенето в такива дни пречиства организма ни и насочва съзнанието ни навътре. За тези, които смятат, че постенето е предизвикателство, вместо това изберете лека плодова диета. #Екадаши",
    "Утре си вземете почивка от храната. Постът на Екадаши пречиства тялото, повишава бдителността и ни позволява да се обърнем навътре. Ако целодневното гладуване се окаже трудно, помислете за лека плодова диета. #Екадаши",
    "Екадаши е един от дните в месеца, когато тялото ни естествено не се нуждае от храна. Постенето на този ден помага за пречистване на организма и насочва вниманието ни навътре. След като вечеряте тази вечер, можете да постите до следващата вечеря по същото време. Ако не сте в състояние да проведете пълен пост, леката плодова диета на този ден също е полезна. #Екадаши",
    "Два пъти месечно има определени дни, в които тялото не се нуждае от храна. Постенето в тези дни пречиства организма и насочва вниманието ни навътре. За тези, които не могат да се справят без храна, леката плодова диета на този ден също се оказва полезна. #Екадаши",
    "По лунния календар Екадаши се отбелязва 11 дни след новолунието или пълнолунието. Този ден е идеален за постене. След като приключите с вечерята си тази вечер, можете да постите до следващата вечеря. Екадаши е възможност за пречистване на организма, както и за превръщане на храненето в по-осъзнат процес. #Екадаши",
    "Екадаши е един от дните в месеца, когато тялото ни не се нуждае от храна. Постенето в тези дни помага за пречистване на организма и насочва вниманието ни навътре. За тези, които не могат да се справят без храна, леката плодова диета на този ден също се оказва полезна. #Екадаши",
    "На Екадаши, единадесетия ден след новолунието и пълнолунието, е идеално да не се яде. Това ще пречисти тялото, ще помогне на човек да остане бодър и ще създаде възможност да се обърне навътре. Ако гладуването през целия ден се окаже трудно, вместо това може да се хапнат плодове. #Екадаши",
    "Екадаши е един от дните в месеца, когато тялото ни естествено не иска храна. Постенето на този ден помага за пречистване на организма и насочва вниманието ни навътре. След като вечеряте тази вечер, можете да постите до следващата вечеря по същото време. Ако не сте в състояние да проведете пълен пост, леката плодова диета на този ден също е полезна. #Екадаши",
    "В лунния календар Екадаши се отбелязва 11 дни след новолуние или пълнолуние. Този ден е идеален за пост. След като приключите с вечерята си тази вечер, можете да постите до следващата вечеря. Екадаши е възможност да очистите организма си и да направите отношението си към храната по-съзнателен процес. #Екадаши",
    "Екадаши е един от онези дни в месеца, когато тялото ни не се нуждае от храна. Постенето през тези дни помага за прочистването на организма ни и насочва съзнанието ни навътре. За тези, които не могат да се лишат от храна, леката плодова диета през този ден също се оказва полезна. #Екадаши",
    "На Екадаши, единадесетия ден след новолуние и пълнолуние, е идеално да се въздържате от храна. Това ще пречисти тялото, ще ви помогне да останете бдителни и ще ви даде възможност да се обърнете навътре. Ако се окаже трудно да постиш целия ден, можеш да ядеш плодове. #Екадаши",
    "Екадаши е един от онези дни в месеца, когато тялото ни естествено не се нуждае от храна. Постенето на този ден помага за прочистване на организма и насочване на съзнанието ни навътре. След като вечеряте тази вечер, можете да постите до следващата вечеря по същото време. Ако не можете да постите напълно, лека диета с плодове на този ден също е полезна. #Екадаши",
    "Два пъти месечно има определени дни, в които тялото не се нуждае от храна. Постенето през тези дни пречиства организма и насочва съзнанието ни навътре. За тези, които не могат да се лишат от храна, леката плодова диета през този ден също се оказва полезна. #Екадаши",
    "Екадаши е ден, в който тялото ни естествено се нуждае от по-малко храна. Постенето на този ден пречиства организма и насочва съзнанието ни навътре. След вечерята тази вечер постете до следващата. Ако не можете да постите напълно, лека диета с плодове също е полезна. #Екадаши",
    "Екадаши е един от онези дни в месеца, когато тялото ни естествено не се нуждае от храна. Постенето на този ден помага за прочистване на организма и насочване на съзнанието ни навътре. След като вечеряте тази вечер, можете да постите до следващата вечеря по същото време. Ако не можете да постите напълно, лека диета с плодове на този ден също е полезна. #Екадаши",
    "Два пъти месечно има определени дни, в които тялото не се нуждае от храна. Постенето през тези дни пречиства организма и повишава осъзнаването на връзката на тялото с храната и земята. Ако постенето през целия ден ви се струва трудно, изберете лека диета на основата на плодове. #Екадаши",
    "Вайкунда е метафора за мукти, или освобождение.\n\nВайкунда Екадаши се счита за най-важния екадаши в годината. Въпреки че спазването на пост по време на всеки екадаши е благоприятно за храносмилателната ни система и прави чудеса за цялостното ни благосъстояние, спазването на целодневен пост на този екадаши е от голямо духовно значение.\n\nПостите ли на този Вайкунда Екадаши? \n#Екадаши",
)


def canva_design_title_for_year(year: int) -> str:
    return f"Ekadashi {year}"


_EKADASHI_DESIGN_YEAR_RE = re.compile(r"^ekadashi\s+(\d{4})$", re.IGNORECASE)


def parse_ekadashi_design_year(title: str) -> int | None:
    """Return the year from a Canva title like ``Ekadashi 2026``, else None."""
    match = _EKADASHI_DESIGN_YEAR_RE.match((title or "").strip())
    if match is None:
        return None
    return int(match.group(1))


def isha_calendar_ics_url(calendar_id: str = DEFAULT_ISHA_CALENDAR_ID) -> str:
    encoded = urllib.parse.quote(calendar_id.strip(), safe="@")
    return f"https://calendar.google.com/calendar/ical/{encoded}/public/basic.ics"


def normalize_ics_text(text: str) -> str:
    """Normalize Google ICS quirks (CRLF + blank lines between every property)."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    while "\n\n" in normalized:
        normalized = normalized.replace("\n\n", "\n")
    return normalized


def _unescape_ics_text(value: str) -> str:
    return (
        value.replace(r"\\", "\\")
        .replace(r"\,", ",")
        .replace(r"\;", ";")
        .replace(r"\n", "\n")
        .strip()
    )


def _parse_ics_date(raw: str) -> date:
    return date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))


def parse_ekadashi_events_from_ics(
    text: str,
    *,
    year: int | None = None,
) -> list[CalendarEkadashi]:
    """Parse all-day Ekadashi VEVENTs from an ICS document."""
    normalized = normalize_ics_text(text)
    found: dict[date, str] = {}
    for block in normalized.split("BEGIN:VEVENT")[1:]:
        summary_match = _SUMMARY_RE.search(block)
        if summary_match is None:
            continue
        summary = _unescape_ics_text(summary_match.group(1))
        if "ekadashi" not in summary.casefold():
            continue
        dt_match = _DTSTART_DATE_RE.search(block)
        if dt_match is None:
            continue
        event_date = _parse_ics_date(dt_match.group(1))
        if year is not None and event_date.year != year:
            continue
        previous = found.get(event_date)
        if previous is None or (
            previous.casefold() == "ekadashi"
            and summary.casefold() != "ekadashi"
        ):
            found[event_date] = summary
    return [
        CalendarEkadashi(event_date=event_date, summary=found[event_date])
        for event_date in sorted(found)
    ]


def fetch_isha_calendar_ics(
    *,
    calendar_id: str = DEFAULT_ISHA_CALENDAR_ID,
    timeout: int = 120,
) -> str:
    url = isha_calendar_ics_url(calendar_id)
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise EkadashiCalendarError(
            f"Failed to download Isha calendar ICS ({url}): HTTP {exc.code}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise EkadashiCalendarError(
            f"Failed to download Isha calendar ICS ({url}): {exc.reason}"
        ) from exc


def load_ekadashi_events(
    *,
    year: int,
    calendar_id: str = DEFAULT_ISHA_CALENDAR_ID,
    ics_path: Path | None = None,
    ics_text: str | None = None,
) -> list[CalendarEkadashi]:
    """Load Ekadashi dates for ``year`` from ICS text, a local file, or the live feed."""
    if ics_text is not None:
        text = ics_text
    elif ics_path is not None:
        try:
            text = ics_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise EkadashiCalendarError(
                f"Cannot read calendar ICS file {ics_path}: {exc}"
            ) from exc
    else:
        text = fetch_isha_calendar_ics(calendar_id=calendar_id)
    events = parse_ekadashi_events_from_ics(text, year=year)
    if not events:
        raise EkadashiCalendarError(
            f"No Ekadashi events found for {year} in Isha calendar "
            f"({calendar_id}). Check {ISHA_CALENDAR_EMBED_URL}"
        )
    return events


def build_ekadashi_entries(
    events: Sequence[CalendarEkadashi],
    *,
    captions: Sequence[str] = CAPTIONS_FROM_2025,
) -> tuple[EkadashiEntry, ...]:
    """Pair calendar dates (chronological) with captions / Canva page numbers."""
    if len(events) != len(captions):
        raise EkadashiCalendarError(
            f"Calendar has {len(events)} Ekadashi date(s) but "
            f"{len(captions)} hard-coded caption(s); counts must match."
        )
    entries: list[EkadashiEntry] = []
    for index, (event, caption) in enumerate(zip(events, captions, strict=True), start=1):
        entries.append(
            EkadashiEntry(
                year=event.event_date.year,
                month=event.event_date.month,
                day=event.event_date.day,
                name=event.summary.strip() or "Ekadashi",
                caption=caption,
                page_number=index,
            )
        )
    return tuple(entries)


def ekadashi_entries_for_year(
    year: int,
    *,
    calendar_id: str = DEFAULT_ISHA_CALENDAR_ID,
    ics_path: Path | None = None,
    ics_text: str | None = None,
    events: Sequence[CalendarEkadashi] | None = None,
    captions: Sequence[str] = CAPTIONS_FROM_2025,
) -> tuple[EkadashiEntry, ...]:
    resolved = (
        list(events)
        if events is not None
        else load_ekadashi_events(
            year=year,
            calendar_id=calendar_id,
            ics_path=ics_path,
            ics_text=ics_text,
        )
    )
    wrong_year = [e for e in resolved if e.event_date.year != year]
    if wrong_year:
        raise EkadashiCalendarError(
            f"Ekadashi events must all be in {year}; got {wrong_year[0].event_date}"
        )
    return build_ekadashi_entries(resolved, captions=captions)


def ekadashi_entries_for_month(
    year: int,
    month: int,
    *,
    calendar_id: str = DEFAULT_ISHA_CALENDAR_ID,
    ics_path: Path | None = None,
    ics_text: str | None = None,
    events: Sequence[CalendarEkadashi] | None = None,
    captions: Sequence[str] = CAPTIONS_FROM_2025,
) -> tuple[EkadashiEntry, ...]:
    if month < 1 or month > 12:
        raise ValueError(f"month must be 1-12 (got {month})")
    return tuple(
        entry
        for entry in ekadashi_entries_for_year(
            year,
            calendar_id=calendar_id,
            ics_path=ics_path,
            ics_text=ics_text,
            events=events,
            captions=captions,
        )
        if entry.month == month
    )
