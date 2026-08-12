from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time

from media_publisher.events.format import format_bulgarian_datetime, format_iso_local

EVENT_TYPE_SURYA_KRIYA = "surya_kriya"

SURYA_KRIYA_LEARN_MORE_URL = "https://youtu.be/Lh0ZucHjp14"
SURYA_KRIYA_LEARN_MORE_LABEL = "Суря крия - Запалете Слънцето във вас! | Садгуру"

# Bulgarian rendering of the English template quote.
SURYA_KRIYA_QUOTE = (
    "„Суря крия е мощен процес за активиране на слънчевата сила във вас.“ – Садгуру"
)


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


def supported_event_types() -> tuple[str, ...]:
    return (EVENT_TYPE_SURYA_KRIYA,)


def city_preposition(city: str) -> str:
    """Bulgarian 'в' becomes 'във' before cities that start with В/в."""
    first = (city or "").strip()[:1]
    if first.casefold() == "в":
        return "във"
    return "в"


def render_event(
    *,
    event_type: str,
    city: str,
    country: str,
    event_date: date,
    event_time: time,
    registration_link: str,
) -> RenderedEvent:
    normalized_type = event_type.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized_type != EVENT_TYPE_SURYA_KRIYA:
        raise ValueError(
            f"Unsupported event type {event_type!r}. "
            f"Supported: {', '.join(supported_event_types())}"
        )

    city_text = city.strip()
    country_text = country.strip()
    link = registration_link.strip()
    if not city_text:
        raise ValueError("city is required")
    if not country_text:
        raise ValueError("country is required")
    if not link:
        raise ValueError("registration_link is required")

    datetime_display = format_bulgarian_datetime(event_date, event_time)
    datetime_iso = format_iso_local(event_date, event_time)
    title_line1 = '☀️ Програма "Суря крия" ☀️'
    title_line2 = f"{city_preposition(city_text)} {city_text}, {country_text}"
    title = f"{title_line1}\n{title_line2}"
    title_one_line = (
        f'☀️ Програма "Суря крия" {city_preposition(city_text)} '
        f"{city_text}, {country_text} ☀️"
    )

    body_lines = [
        title_line1,
        title_line2,
        "",
        f"🗓: {datetime_display}",
        "",
        SURYA_KRIYA_QUOTE,
        "",
        "„Суря“ означава Слънце, а „крия“ – вътрешен енергиен процес. "
        "Суря крия активира слънчевия сплит, което води до повишаване на "
        "„самат прана“, или слънчевата топлина, в системата.",
        "",
        "Ползи:",
        "",
        "✅ Умствена яснота и фокус",
        "✅ Подобрено физическо здраве",
        "✅ Укрепване на отслабения организъм",
        "✅ Повишена енергия и жизненост",
        "✅ Балансирани хормонални нива",
        "",
        f"Вижте какво казва Садгуру: {SURYA_KRIYA_LEARN_MORE_LABEL}",
        SURYA_KRIYA_LEARN_MORE_URL,
        "",
        f"👉 Регистрация тук: {link}",
        "",
        "💫 С любов, светлина и смях,",
        "Доброволци от Иша",
    ]
    full_text = "\n".join(body_lines)

    # Facebook caption matches the page/template, but:
    # - title is one line (as in the shared Doc template)
    # - YouTube line is URL-only (no video title) so Facebook can linkify it
    facebook_post_lines = [
        title_one_line,
        "",
        f"🗓: {datetime_display}",
        "",
        SURYA_KRIYA_QUOTE,
        "",
        "„Суря“ означава Слънце, а „крия“ – вътрешен енергиен процес. "
        "Суря крия активира слънчевия сплит, което води до повишаване на "
        "„самат прана“, или слънчевата топлина, в системата.",
        "",
        "Ползи:",
        "",
        "✅ Умствена яснота и фокус",
        "✅ Подобрено физическо здраве",
        "✅ Укрепване на отслабения организъм",
        "✅ Повишена енергия и жизненост",
        "✅ Балансирани хормонални нива",
        "",
        f"Вижте какво казва Садгуру: {SURYA_KRIYA_LEARN_MORE_URL}",
        "",
        f"👉 Регистрация тук: {link}",
        "",
        "💫 С любов, светлина и смях,",
        "Доброволци от Иша",
    ]
    facebook_post_text = "\n".join(facebook_post_lines)

    html_body = _html_section(
        title_line1=title_line1,
        title_line2=title_line2,
        datetime_display=datetime_display,
        quote=SURYA_KRIYA_QUOTE,
        learn_more_url=SURYA_KRIYA_LEARN_MORE_URL,
        learn_more_label=SURYA_KRIYA_LEARN_MORE_LABEL,
        registration_link=link,
    )

    return RenderedEvent(
        event_type=EVENT_TYPE_SURYA_KRIYA,
        title=title,
        city=city_text,
        country=country_text,
        event_date=event_date,
        event_time=event_time,
        datetime_display=datetime_display,
        datetime_iso=datetime_iso,
        registration_link=link,
        learn_more_url=SURYA_KRIYA_LEARN_MORE_URL,
        full_text=full_text,
        facebook_post_text=facebook_post_text,
        html_body=html_body,
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
    learn_more_url: str,
    learn_more_label: str,
    registration_link: str,
) -> str:
    return "\n".join(
        [
            f"<h2>{_html_escape(title_line1)}<br>{_html_escape(title_line2)}</h2>",
            f'<p class="when">🗓 {_html_escape(datetime_display)}</p>',
            f'<p class="quote">{_html_escape(quote)}</p>',
            "<p>„Суря“ означава Слънце, а „крия“ – вътрешен енергиен процес. "
            "Суря крия активира слънчевия сплит, което води до повишаване на "
            "„самат прана“, или слънчевата топлина, в системата.</p>",
            "<p><strong>Ползи:</strong></p>",
            "<ul>",
            "<li>Умствена яснота и фокус</li>",
            "<li>Подобрено физическо здраве</li>",
            "<li>Укрепване на отслабения организъм</li>",
            "<li>Повишена енергия и жизненост</li>",
            "<li>Балансирани хормонални нива</li>",
            "</ul>",
            "<p class=\"learn-more\">Вижте какво казва Садгуру:<br>"
            f'<a class="yt-link" href="{_html_escape(learn_more_url)}" '
            'target="_blank" rel="noopener noreferrer">'
            '<svg class="yt-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
            '<path fill="#FF0000" d="M23.5 6.2a3 3 0 0 0-2.1-2.1C19.5 3.5 12 3.5 12 3.5s-7.5 0-9.4.6A3 3 0 0 0 .5 6.2 31.5 31.5 0 0 0 0 12a31.5 31.5 0 0 0 .5 5.8 3 3 0 0 0 2.1 2.1c1.9.6 9.4.6 9.4.6s7.5 0 9.4-.6a3 3 0 0 0 2.1-2.1A31.5 31.5 0 0 0 24 12a31.5 31.5 0 0 0-.5-5.8z"/>'
            '<path fill="#fff" d="M9.75 15.5v-7L16 12z"/>'
            "</svg>"
            f'<span class="yt-title">{_html_escape(learn_more_label)}</span>'
            "</a></p>",
            '<p class="cta">'
            f'<a href="{_html_escape(registration_link)}">Регистрация</a></p>',
            "<p>💫 С любов, светлина и смях,<br>Доброволци от Иша</p>",
        ]
    )
