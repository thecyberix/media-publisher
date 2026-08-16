from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time

from media_publisher.events.format import format_event_datetime, format_iso_local
from media_publisher.languages import LanguageDefinition, get_language, selected_language

EVENT_TYPE_SURYA_KRIYA = "surya_kriya"
EVENT_TYPE_BHUTA_SHUDDHI = "bhuta_shuddhi"
EVENT_TYPE_YOGASANA = "yogasana"

# Programme images and the Hatha WhatsApp template live in the "Events" child of DRIVE_URL.

SURYA_KRIYA_LEARN_MORE_LABEL = "Суря крия - Запалете Слънцето във вас! | Садгуру"
SURYA_KRIYA_QUOTE = (
    "„Суря крия е мощен процес за активиране на слънчевата енергия във вас.“ – Садгуру"
)
SURYA_KRIYA_BODY = (
    "„Суря“ означава Слънце, а „крия“ – вътрешен енергиен процес. "
    "Суря крия активира слънчевия сплит, което води до повишаване на "
    "„самат прана“, или слънчевата топлина, в системата."
)
SURYA_KRIYA_BENEFITS = (
    "Умствена яснота и фокус",
    "Подобрено физическо здраве",
    "Укрепване на отслабения организъм",
    "Повишена енергия и жизненост",
    "Балансирани хормонални нива",
)

BHUTA_SHUDDHI_LEARN_MORE_LABEL = (
    "Бута Шудди - Основното Пречистване | Садгуру на Български"
)
BHUTA_SHUDDHI_QUOTE = (
    "„Бута Шудди цели премахването на всичко, което сте натрупали, "
    "за да може творението на Твореца да се издигне и да засияе във вас.“ – Садгуру"
)
BHUTA_SHUDDHI_BODY = (
    "Бута Шудди е йога система, фокусирана върху пречистването на петте елемента "
    "(бути), които съставляват нашето тяло: земя, вода, въздух, огън и пространство."
)
BHUTA_SHUDDHI_BENEFITS = (
    "Хармония и баланс между тялото и ума",
    "Увеличен капацитет на цялата ви система",
    "Пречистване на петте елемента във вас",
)

YOGASANA_LEARN_MORE_LABEL = "Йогасани - пози за издигане на съзнанието | Садгуру"
YOGASANA_QUOTE = (
    "„Ако сте в една асана осъзнато, тя може да промени начина, по който мислите, "
    "чувствате и преживявате живота. Това може да постигне Хата йога.“ – Садгуру"
)
YOGASANA_BODY = (
    "Йогасаните са мощна поредица от 21 пози, или асани, структурирани така, "
    "че да способстват за това вашето тяло да поддържа високи енергийни нива. "
    "Не се изисква предишен опит в йога или гъвкавост."
)
YOGASANA_BENEFITS = (
    "Облекчаване на хронични здравословни проблеми",
    "Развиване на тялото и ума до пълния им потенциал",
    "Стабилизиране на тялото, ума и енергийната система",
    "Забавяне на процеса на стареене",
)


@dataclass(frozen=True)
class ProgramTemplate:
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
    facebook_image_folder: str
    benefits_heading: str = ""


PROGRAMS: dict[str, ProgramTemplate] = {
    EVENT_TYPE_SURYA_KRIYA: ProgramTemplate(
        event_type=EVENT_TYPE_SURYA_KRIYA,
        program_name="Суря крия",
        title_emoji="☀️",
        quote=SURYA_KRIYA_QUOTE,
        body=SURYA_KRIYA_BODY,
        benefits=SURYA_KRIYA_BENEFITS,
        benefit_bullet="✅",
        learn_more_intro="Вижте какво казва Садгуру:",
        learn_more_url="",
        learn_more_label=SURYA_KRIYA_LEARN_MORE_LABEL,
        facebook_image_folder="Surya Kriya",
    ),
    EVENT_TYPE_BHUTA_SHUDDHI: ProgramTemplate(
        event_type=EVENT_TYPE_BHUTA_SHUDDHI,
        program_name="Бута Шудди",
        title_emoji="💫",
        quote=BHUTA_SHUDDHI_QUOTE,
        body=BHUTA_SHUDDHI_BODY,
        benefits=BHUTA_SHUDDHI_BENEFITS,
        benefit_bullet="🎯",
        learn_more_intro="Вижте видеото:",
        learn_more_url="",
        learn_more_label=BHUTA_SHUDDHI_LEARN_MORE_LABEL,
        facebook_image_folder="Bhuta Shuddhi",
    ),
    EVENT_TYPE_YOGASANA: ProgramTemplate(
        event_type=EVENT_TYPE_YOGASANA,
        program_name="Йогасани",
        title_emoji="🧘‍♀️",
        quote=YOGASANA_QUOTE,
        body=YOGASANA_BODY,
        benefits=YOGASANA_BENEFITS,
        benefit_bullet="✅",
        learn_more_intro="Научете повече тук:",
        learn_more_url="",
        learn_more_label=YOGASANA_LEARN_MORE_LABEL,
        facebook_image_folder="Yogasanas",
    ),
}


@dataclass(frozen=True)
class RenderedEvent:
    event_type: str
    title: str
    city: str
    country: str
    event_date: date
    event_time: time
    datetime_display: str
    datetime_iso: str
    registration_link: str
    learn_more_url: str
    full_text: str
    facebook_post_text: str
    html_body: str
    facebook_image_folder: str


def supported_event_types() -> tuple[str, ...]:
    return tuple(PROGRAMS.keys())


def normalize_event_type(event_type: str) -> str:
    normalized = event_type.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized == "yogasanas":
        return EVENT_TYPE_YOGASANA
    return normalized


def get_program(event_type: str) -> ProgramTemplate:
    normalized = normalize_event_type(event_type)
    program = PROGRAMS.get(normalized)
    if program is None:
        raise ValueError(
            f"Unsupported event type {event_type!r}. "
            f"Supported: {', '.join(supported_event_types())}"
        )
    return program


def city_preposition(city: str, language: LanguageDefinition | None = None) -> str:
    events = (language or selected_language()).require_events()
    first = (city or "").strip()[:1].casefold()
    for prefix, preposition in events.city_preposition_before:
        if first == prefix.casefold():
            return preposition
    return events.city_preposition


def render_event(
    *,
    event_type: str,
    city: str,
    country: str,
    event_date: date,
    event_time: time,
    registration_link: str,
    program: ProgramTemplate | None = None,
    language: str = "bg",
) -> RenderedEvent:
    program = program or get_program(event_type)
    definition = get_language(language) or selected_language()
    events = definition.require_events()

    city_text = city.strip()
    country_text = country.strip()
    link = registration_link.strip()
    if not city_text:
        raise ValueError("city is required")
    if not country_text:
        raise ValueError("country is required")
    if not link:
        raise ValueError("registration_link is required")

    datetime_display = format_event_datetime(
        event_date, event_time, language=definition.alias
    )
    datetime_iso = format_iso_local(event_date, event_time)
    prep = city_preposition(city_text, definition)
    emoji = program.title_emoji
    title_line1 = (
        f'{emoji} {events.program_word} "{program.program_name}" {emoji}'
    )
    title_line2 = f"{prep} {city_text}, {country_text}"
    title = f"{title_line1}\n{title_line2}"
    title_one_line = (
        f'{emoji} {events.program_word} "{program.program_name}" {prep} '
        f"{city_text}, {country_text} {emoji}"
    )

    benefit_lines = tuple(
        f"{program.benefit_bullet} {benefit}" for benefit in program.benefits
    )
    benefits_heading = program.benefits_heading or events.benefits_headings[0]

    body_lines = [
        title_line1,
        title_line2,
        "",
        f"🗓: {datetime_display}",
    ]
    if program.quote:
        body_lines.extend(["", program.quote])
    if program.body:
        body_lines.extend(["", program.body])
    body_lines.extend(
        [
            "",
            benefits_heading,
            "",
            *benefit_lines,
            "",
            f"{program.learn_more_intro} {program.learn_more_label}",
            program.learn_more_url,
            "",
            f"{events.registration_cta} {link}",
            "",
            *events.closing_lines,
        ]
    )
    full_text = "\n".join(body_lines)

    # Facebook caption: one-line title; learn-more line is URL-only.
    facebook_post_lines = [
        title_one_line,
        "",
        f"🗓: {datetime_display}",
    ]
    if program.quote:
        facebook_post_lines.extend(["", program.quote])
    if program.body:
        facebook_post_lines.extend(["", program.body])
    facebook_post_lines.extend(
        [
            "",
            benefits_heading,
            "",
            *benefit_lines,
            "",
            f"{program.learn_more_intro} {program.learn_more_url}",
            "",
            f"{events.registration_cta} {link}",
            "",
            *events.closing_lines,
        ]
    )
    facebook_post_text = "\n".join(facebook_post_lines)

    html_body = _html_section(
        title_line1=title_line1,
        title_line2=title_line2,
        datetime_display=datetime_display,
        quote=program.quote,
        body=program.body,
        benefits=program.benefits,
        benefit_bullet=program.benefit_bullet,
        learn_more_intro=program.learn_more_intro,
        learn_more_url=program.learn_more_url,
        learn_more_label=program.learn_more_label,
        registration_link=link,
        benefits_heading=benefits_heading,
        html_registration_label=events.html_registration_label,
    )

    return RenderedEvent(
        event_type=program.event_type,
        title=title,
        city=city_text,
        country=country_text,
        event_date=event_date,
        event_time=event_time,
        datetime_display=datetime_display,
        datetime_iso=datetime_iso,
        registration_link=link,
        learn_more_url=program.learn_more_url,
        full_text=full_text,
        facebook_post_text=facebook_post_text,
        html_body=html_body,
        facebook_image_folder=program.facebook_image_folder,
    )


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _html_section(
    *,
    title_line1: str,
    title_line2: str,
    datetime_display: str,
    quote: str,
    body: str,
    benefits: tuple[str, ...],
    benefit_bullet: str,
    learn_more_intro: str,
    learn_more_url: str,
    learn_more_label: str,
    benefits_heading: str,
    html_registration_label: str,
    registration_link: str,
) -> str:
    mark = f"{_html_escape(benefit_bullet)} " if benefit_bullet else ""
    benefit_items = "\n".join(
        f"<li>{mark}{_html_escape(benefit)}</li>" for benefit in benefits
    )
    heading = _html_escape(benefits_heading)
    # Fixed section order (one DOM node per aligned subgrid row).
    return "\n".join(
        [
            f"<h2>{_html_escape(title_line1)}<br>{_html_escape(title_line2)}</h2>",
            f'<p class="when">🗓 {_html_escape(datetime_display)}</p>',
            f'<p class="quote">{_html_escape(quote)}</p>',
            f'<p class="description">{_html_escape(body)}</p>',
            '<div class="benefits-block">',
            f"<p><strong>{heading}</strong></p>",
            '<ul class="benefits">',
            benefit_items,
            "</ul>",
            "</div>",
            f'<p class="learn-more">{_html_escape(learn_more_intro)}<br>'
            f'<a class="yt-link" href="{_html_escape(learn_more_url)}" '
            'target="_blank" rel="noopener noreferrer">'
            '<svg class="yt-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
            '<path fill="#FF0000" d="M23.5 6.2a3 3 0 0 0-2.1-2.1C19.5 3.5 12 3.5 12 3.5s-7.5 0-9.4.6A3 3 0 0 0 .5 6.2 31.5 31.5 0 0 0 0 12a31.5 31.5 0 0 0 .5 5.8 3 3 0 0 0 2.1 2.1c1.9.6 9.4.6 9.4.6s7.5 0 9.4-.6a3 3 0 0 0 2.1-2.1A31.5 31.5 0 0 0 24 12a31.5 31.5 0 0 0-.5-5.8z"/>'
            '<path fill="#fff" d="M9.75 15.5v-7L16 12z"/>'
            "</svg>"
            f'<span class="yt-title">{_html_escape(learn_more_label)}</span>'
            "</a></p>",
            '<p class="cta">'
            f'<a href="{_html_escape(registration_link)}">'
            f"{_html_escape(html_registration_label)}</a></p>",
        ]
    )
