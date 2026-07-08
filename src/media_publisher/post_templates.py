from __future__ import annotations

import re
from dataclasses import replace

from media_publisher.models import PlatformName, PublishJob, VideoFormat
from media_publisher.sources.airtable import FIELD_ORIGINAL_VIDEO, FIELD_TITLE

DEFAULT_YOUTUBE_CHANNEL_URL = "https://www.youtube.com/channel/UCg8jXnEr8ZKmuwm3S9J4e-Q"
DEFAULT_FACEBOOK_PAGE_URL = "https://www.facebook.com/SadhguruBulgarian"
DEFAULT_INSTAGRAM_PROFILE_URL = "https://www.instagram.com/sadhguru.bulgarian/"

YOUTUBE_TAGS_POST = (
    "садгуру на български език",
    "садгуру",
    "садгуру на български",
    "садгуру бг превод",
    "садгуру бг",
    "медитация",
    "садгуру медитация",
    "садгуру йога",
    "йога практика",
    "вътрешно изграждане",
    "чудото на ума",
    "садгуру българия",
)

YOUTUBE_TAGS_SHORT = (
    "садгуру на български език",
    "садгуру",
    "садгуру на български",
    "садгуру бг превод",
    "садгуру бг",
    "медитация",
    "садгуру медитация",
    "садгуру йога",
    "йога практика",
    "вътрешно изграждане",
    "духовно развитие",
)

SOCIAL_FOOTER_FACEBOOK_LABEL = (
    "Официална страница на български във Facebook: {facebook_url}"
)
SOCIAL_FOOTER_INSTAGRAM_LABEL = (
    "Официален профил на български в Instagram: {instagram_url}"
)
SOCIAL_FOOTER_YOUTUBE_LABEL = (
    "Садгуру - официален канал на български език в YouTube {youtube_channel_url}"
)
ORIGINAL_VIDEO_LABEL = "Original video: {original_video_url}"


def _original_video_url(job: PublishJob) -> str | None:
    url = job.video_url or job.metadata.get(FIELD_ORIGINAL_VIDEO)
    if url and url.strip():
        return url.strip()
    return None


def _social_footer(
    *,
    facebook_url: str,
    instagram_url: str,
    youtube_channel_url: str,
    include_original_video: bool,
    original_video_url: str | None,
) -> str:
    lines: list[str] = []
    if include_original_video and original_video_url:
        lines.append(ORIGINAL_VIDEO_LABEL.format(original_video_url=original_video_url))
        lines.append("")
    lines.extend(
        [
            SOCIAL_FOOTER_FACEBOOK_LABEL.format(facebook_url=facebook_url),
            "",
            SOCIAL_FOOTER_INSTAGRAM_LABEL.format(instagram_url=instagram_url),
            "",
            SOCIAL_FOOTER_YOUTUBE_LABEL.format(youtube_channel_url=youtube_channel_url),
        ]
    )
    return "\n".join(lines)


def _youtube_body_text(job: PublishJob) -> str:
    return job.description.strip() or job.title.strip()


def build_long_form_description(
    job: PublishJob,
    *,
    facebook_url: str = DEFAULT_FACEBOOK_PAGE_URL,
    instagram_url: str = DEFAULT_INSTAGRAM_PROFILE_URL,
    youtube_channel_url: str = DEFAULT_YOUTUBE_CHANNEL_URL,
) -> str:
    body = _youtube_body_text(job)
    footer = _social_footer(
        facebook_url=facebook_url,
        instagram_url=instagram_url,
        youtube_channel_url=youtube_channel_url,
        include_original_video=True,
        original_video_url=_original_video_url(job),
    )
    if body:
        return f"{body}\n\n{footer}"
    return footer


YOUTUBE_TITLE_MAX_LENGTH = 100


def build_short_form_youtube_title(title: str) -> str:
    clean = title.strip()
    if not clean:
        return "Садгуру #shorts"
    if "#shorts" in clean.lower():
        return clean[:YOUTUBE_TITLE_MAX_LENGTH]
    if clean.endswith("| Садгуру"):
        suffix = " #shorts"
    else:
        suffix = " | Садгуру #shorts"
    max_base = YOUTUBE_TITLE_MAX_LENGTH - len(suffix)
    if max_base < 1:
        return "Садгуру #shorts"[:YOUTUBE_TITLE_MAX_LENGTH]
    base = clean[:max_base].rstrip(" -|")
    if not base:
        return "Садгуру #shorts"[:YOUTUBE_TITLE_MAX_LENGTH]
    return f"{base}{suffix}"


def build_short_form_youtube_description(description: str) -> str:
    clean = description.strip()
    header = "#shorts #садгуру"
    if clean:
        return f"{header}\n{clean}"
    return header


def _description_after_hashtag(description: str) -> str:
    if description.startswith("Садгуру "):
        return description[len("Садгуру ") :]
    if description.startswith("садгуру "):
        return description[len("садгуру ") :]
    return description


def build_long_form_social_caption(job: PublishJob) -> str:
    """Format long-form Facebook/Instagram captions like published Sadhguru BG posts."""
    title = job.title.strip().rstrip(".")
    description = job.description.strip()
    if not title:
        if not description:
            return QUOTE_HASHTAG
        return f"{QUOTE_HASHTAG} {_description_after_hashtag(description)}"
    if not description:
        return f"{title}. {QUOTE_HASHTAG}"
    if description.startswith(title):
        rest = description[len(title) :].lstrip(" .")
        if rest.startswith(QUOTE_HASHTAG):
            return description
        if rest.startswith(LEGACY_QUOTE_HASHTAG):
            rest = rest[len(LEGACY_QUOTE_HASHTAG) :].lstrip()
        rest = _description_after_hashtag(rest)
        return f"{title}. {QUOTE_HASHTAG} {rest}" if rest else f"{title}. {QUOTE_HASHTAG}"
    rest = _description_after_hashtag(description)
    return f"{title}. {QUOTE_HASHTAG} {rest}"


def inject_published_video_url(description: str, video_id: str) -> str:
    """Insert the published YouTube short link before the long-form footer."""
    short_url = f"https://youtu.be/{video_id}"
    if short_url in description:
        return description
    marker = "\n\nOriginal video:"
    if marker in description:
        body, footer = description.split(marker, 1)
        return f"{body.rstrip()}\n\n{short_url}{marker}{footer}"
    body = description.rstrip()
    return f"{body}\n\n{short_url}" if body else short_url


QUOTE_HASHTAG = "#Садгуру"
LEGACY_QUOTE_HASHTAG = "[#Садгуру]"


def build_quote_post_caption(caption: str) -> str:
    """Format quote posts like the Sadhguru Bulgarian Facebook quote template."""
    clean = caption.strip()
    if not clean:
        return QUOTE_HASHTAG
    if clean.endswith(QUOTE_HASHTAG):
        return clean
    if clean.endswith(LEGACY_QUOTE_HASHTAG):
        return f"{clean[: -len(LEGACY_QUOTE_HASHTAG)].rstrip()} {QUOTE_HASHTAG}"
    return f"{clean} {QUOTE_HASHTAG}"


def _quote_body(caption: str) -> str:
    clean = caption.strip()
    if clean.endswith(QUOTE_HASHTAG):
        return clean[: -len(QUOTE_HASHTAG)].rstrip()
    if clean.endswith(LEGACY_QUOTE_HASHTAG):
        return clean[: -len(LEGACY_QUOTE_HASHTAG)].rstrip()
    return clean


def build_quote_youtube_title(caption: str) -> str:
    """Build a YouTube title from quote text with trailing #Садгуру."""
    body = _quote_body(caption)
    suffix = f" {QUOTE_HASHTAG}"
    max_body_len = YOUTUBE_TITLE_MAX_LENGTH - len(suffix)
    if not body:
        return QUOTE_HASHTAG

    if len(body) <= max_body_len:
        return f"{body}{suffix}"

    match = re.match(r"^(.+?[.!?])(?:\s|$)", body)
    first_sentence = match.group(1).strip() if match else body
    if len(first_sentence) <= max_body_len:
        return f"{first_sentence}{suffix}"

    ellipsis = "..."
    max_len = max_body_len - len(ellipsis)
    return f"{first_sentence[:max_len].rstrip()}{ellipsis}{suffix}"


def build_quote_youtube_description(caption: str) -> str:
    body = _quote_body(caption)
    if not body:
        return "#садгуру"
    return f"#садгуру\n{body}"


def build_quote_social_caption(caption: str) -> str:
    return build_quote_post_caption(caption)


def _append_trailing_hashtag(text: str) -> str:
    clean = text.strip()
    if not clean:
        return QUOTE_HASHTAG
    if clean.endswith(QUOTE_HASHTAG):
        return clean
    if clean.endswith(LEGACY_QUOTE_HASHTAG):
        return f"{clean[: -len(LEGACY_QUOTE_HASHTAG)].rstrip()} {QUOTE_HASHTAG}"
    return f"{clean} {QUOTE_HASHTAG}"


def build_facebook_video_caption(job: PublishJob) -> str:
    """Facebook caption: title, optional description, then #Садгуру at the end."""
    title = job.title.strip().rstrip(".")
    description = job.description.strip()
    if not title:
        return build_quote_post_caption(description)
    if not description:
        return f"{title}. {QUOTE_HASHTAG}"
    if description.startswith(title):
        body = description
    else:
        body = f"{title}. {description}"
    return _append_trailing_hashtag(body)


def build_short_form_social_caption(job: PublishJob) -> str:
    return build_facebook_video_caption(job)


def prepare_publish_job(
    job: PublishJob,
    platform: PlatformName,
    *,
    facebook_url: str = DEFAULT_FACEBOOK_PAGE_URL,
    instagram_url: str = DEFAULT_INSTAGRAM_PROFILE_URL,
    youtube_channel_url: str = DEFAULT_YOUTUBE_CHANNEL_URL,
) -> PublishJob:
    """Apply Sadhguru Bulgarian post templates for the target platform."""
    if job.video_format == "short_form":
        return _prepare_short_form_job(
            job,
            platform,
            facebook_url=facebook_url,
            instagram_url=instagram_url,
            youtube_channel_url=youtube_channel_url,
        )
    return _prepare_post_format_job(
        job,
        platform,
        facebook_url=facebook_url,
        instagram_url=instagram_url,
        youtube_channel_url=youtube_channel_url,
    )


def _prepare_post_format_job(
    job: PublishJob,
    platform: PlatformName,
    *,
    facebook_url: str,
    instagram_url: str,
    youtube_channel_url: str,
) -> PublishJob:
    description = build_long_form_description(
        job,
        facebook_url=facebook_url,
        instagram_url=instagram_url,
        youtube_channel_url=youtube_channel_url,
    )
    if platform == "youtube":
        return replace(job, description=description, tags=list(YOUTUBE_TAGS_POST))
    if platform == "facebook":
        return replace(job, description=build_facebook_video_caption(job))
    return replace(job, description=build_long_form_social_caption(job))


def _prepare_short_form_job(
    job: PublishJob,
    platform: PlatformName,
    *,
    facebook_url: str,
    instagram_url: str,
    youtube_channel_url: str,
) -> PublishJob:
    if platform == "youtube":
        if job.content_kind == "image":
            title = job.title.strip()
            description = job.description.strip()
            if title and description:
                return replace(
                    job,
                    title=title,
                    description=description,
                    tags=list(YOUTUBE_TAGS_SHORT),
                )
            source = description or title
            return replace(
                job,
                title=build_quote_youtube_title(source),
                description=build_quote_youtube_description(source),
                tags=list(YOUTUBE_TAGS_SHORT),
            )
        return replace(
            job,
            title=build_short_form_youtube_title(job.title),
            description=build_short_form_youtube_description(
                job.description or job.title
            ),
            tags=list(YOUTUBE_TAGS_POST),
        )
    if job.content_kind == "image" and job.description.strip():
        return replace(job, description=build_quote_post_caption(job.description))
    if platform == "facebook":
        return replace(job, description=build_facebook_video_caption(job))
    return replace(job, description=build_short_form_social_caption(job))
