from __future__ import annotations

import os
import re
from dataclasses import replace

from media_publisher.languages import selected_language
from media_publisher.models import PlatformName, PublishJob, VideoFormat
from media_publisher.sources.airtable import FIELD_ORIGINAL_VIDEO, FIELD_TITLE

DEFAULT_YOUTUBE_CHANNEL_URL = ""
DEFAULT_FACEBOOK_PAGE_URL = ""
DEFAULT_INSTAGRAM_PROFILE_URL = ""

ORIGINAL_VIDEO_LABEL = "Original video: {original_video_url}"
YOUTUBE_TITLE_MAX_LENGTH = 100


def _publish():
    return selected_language().require_publish()


def smartlink_url() -> str:
    return os.getenv("SMARTLINK_URL", "").strip()


def smartlink_cta() -> str:
    url = smartlink_url()
    if not url:
        return ""
    return f"{_publish().learn_more_label} {url}"


def quote_hashtag() -> str:
    return _publish().hashtag


def youtube_tags() -> tuple[str, ...]:
    return _publish().youtube_tags


def append_smartlink_cta(text: str) -> str:
    """Append the Smartlink CTA after a blank line when not already present."""
    cta = smartlink_cta()
    url = smartlink_url()
    clean = text.rstrip()
    if not cta:
        return clean
    if cta in clean or (url and url in clean):
        if cta in clean:
            return clean
        return clean.replace(url, cta)
    if not clean:
        return cta
    return f"{clean}\n\n{cta}"


def _original_video_url(job: PublishJob) -> str | None:
    url = job.video_url or job.metadata.get(FIELD_ORIGINAL_VIDEO)
    if url and url.strip():
        return url.strip()
    return None


def _long_form_youtube_footer(
    *,
    include_original_video: bool,
    original_video_url: str | None,
) -> str:
    lines: list[str] = []
    if include_original_video and original_video_url:
        lines.append(ORIGINAL_VIDEO_LABEL.format(original_video_url=original_video_url))
    cta = smartlink_cta()
    if cta:
        if lines:
            lines.append("")
        lines.append(cta)
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
    del facebook_url, instagram_url, youtube_channel_url
    body = _youtube_body_text(job)
    footer = _long_form_youtube_footer(
        include_original_video=True,
        original_video_url=_original_video_url(job),
    )
    if body:
        return f"{body}\n\n{footer}"
    return footer


def build_short_form_youtube_title(title: str) -> str:
    clean = title.strip()
    publish = _publish()
    fallback = f"{publish.display_name} #shorts"
    pipe = publish.youtube_title_pipe_suffix
    if not clean:
        return fallback
    if "#shorts" in clean.lower():
        return clean[:YOUTUBE_TITLE_MAX_LENGTH]
    if pipe and clean.endswith(pipe):
        suffix = " #shorts"
    else:
        suffix = f" {pipe} #shorts" if pipe else " #shorts"
    max_base = YOUTUBE_TITLE_MAX_LENGTH - len(suffix)
    if max_base < 1:
        return fallback[:YOUTUBE_TITLE_MAX_LENGTH]
    base = clean[:max_base].rstrip(" -|")
    if not base:
        return fallback[:YOUTUBE_TITLE_MAX_LENGTH]
    return f"{base}{suffix}"


def build_short_form_youtube_description(description: str) -> str:
    clean = description.strip()
    header = _publish().shorts_description_hashtags
    if clean:
        return append_smartlink_cta(f"{header}\n{clean}")
    return append_smartlink_cta(header)


def _description_after_hashtag(description: str) -> str:
    name = _publish().display_name
    prefix = f"{name} "
    if description.startswith(prefix):
        return description[len(prefix) :]
    if description.casefold().startswith(prefix.casefold()):
        return description[len(name) + 1 :]
    return description


def build_long_form_social_caption(job: PublishJob) -> str:
    """Format long-form Facebook/Instagram captions like published posts."""
    hashtag = quote_hashtag()
    title = job.title.strip().rstrip(".")
    description = job.description.strip()
    if not title:
        if not description:
            return append_smartlink_cta(hashtag)
        return append_smartlink_cta(
            f"{hashtag} {_description_after_hashtag(description)}"
        )
    if not description:
        return append_smartlink_cta(f"{title}. {hashtag}")
    if description.startswith(title):
        rest = description[len(title) :].lstrip(" .")
        if rest.startswith(hashtag):
            return append_smartlink_cta(description)
        rest = _description_after_hashtag(rest)
        caption = (
            f"{title}. {hashtag} {rest}" if rest else f"{title}. {hashtag}"
        )
        return append_smartlink_cta(caption)
    rest = _description_after_hashtag(description)
    return append_smartlink_cta(f"{title}. {hashtag} {rest}")


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


def build_quote_post_caption(caption: str) -> str:
    """Format quote posts with the configured trailing hashtag."""
    hashtag = quote_hashtag()
    clean = caption.strip()
    if not clean:
        return hashtag
    if clean.endswith(hashtag):
        return clean
    return f"{clean} {hashtag}"


def _quote_body(caption: str) -> str:
    hashtag = quote_hashtag()
    cta = smartlink_cta()
    url = smartlink_url()
    label = _publish().learn_more_label
    clean = caption.strip()
    if cta and cta in clean:
        clean = clean.split(cta, 1)[0].rstrip()
    elif url and url in clean:
        clean = clean.split(url, 1)[0].rstrip()
        if clean.endswith(label):
            clean = clean[: -len(label)].rstrip()
    if clean.endswith(hashtag):
        return clean[: -len(hashtag)].rstrip()
    return clean


def build_quote_youtube_title(caption: str) -> str:
    """Build a YouTube title from quote text with the configured hashtag."""
    hashtag = quote_hashtag()
    body = _quote_body(caption)
    suffix = f" {hashtag}"
    max_body_len = YOUTUBE_TITLE_MAX_LENGTH - len(suffix)
    if not body:
        return hashtag

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
    header = _publish().quote_youtube_description_hashtag
    if not body:
        return header
    return f"{header}\n{body}"


def build_quote_social_caption(caption: str) -> str:
    return build_quote_post_caption(caption)


def _append_trailing_hashtag(text: str) -> str:
    hashtag = quote_hashtag()
    clean = text.strip()
    if not clean:
        return hashtag
    if clean.endswith(hashtag):
        return clean
    return f"{clean} {hashtag}"


def build_facebook_video_caption(job: PublishJob) -> str:
    """Facebook caption: title, optional description, then the configured hashtag."""
    hashtag = quote_hashtag()
    title = job.title.strip().rstrip(".")
    description = job.description.strip()
    if not title:
        return build_quote_post_caption(description)
    if not description:
        return append_smartlink_cta(f"{title}. {hashtag}")
    if description.startswith(title):
        body = description
    else:
        body = f"{title}. {description}"
    return append_smartlink_cta(_append_trailing_hashtag(body))


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
        return replace(job, description=description, tags=list(youtube_tags()))
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
                    tags=list(youtube_tags()),
                )
            source = description or title
            return replace(
                job,
                title=build_quote_youtube_title(source),
                description=build_quote_youtube_description(source),
                tags=list(youtube_tags()),
            )
        return replace(
            job,
            title=build_short_form_youtube_title(job.title),
            description=build_short_form_youtube_description(
                job.description or job.title
            ),
            tags=list(youtube_tags()),
        )
    if job.content_kind == "image" and job.description.strip():
        return replace(job, description=build_quote_post_caption(job.description))
    if platform == "facebook":
        return replace(job, description=build_facebook_video_caption(job))
    return replace(job, description=build_short_form_social_caption(job))
