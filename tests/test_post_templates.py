from __future__ import annotations

import unittest

from media_publisher.models import PublishJob
from media_publisher.post_templates import (
    SMARTLINK_CTA,
    build_long_form_description,
    build_long_form_social_caption,
    build_quote_post_caption,
    build_quote_youtube_description,
    build_quote_youtube_title,
    build_short_form_social_caption,
    build_short_form_youtube_description,
    build_short_form_youtube_title,
    inject_published_video_url,
    prepare_publish_job,
)


class PostTemplateTests(unittest.TestCase):
    def test_long_form_youtube_description(self) -> None:
        job = PublishJob(
            title="Когато Садгуру откри Нагамани – бижуто на кобрата",
            description="Садгуру разказва за един увлекателен случай.",
            video_url="https://youtu.be/wCnnfKRycwI",
            video_format="post",
        )
        prepared = prepare_publish_job(job, "youtube")
        self.assertEqual(prepared.title, job.title)
        self.assertIn("Садгуру разказва", prepared.description)
        self.assertIn("Original video: https://youtu.be/wCnnfKRycwI", prepared.description)
        self.assertIn("Доброволец от Иша:", prepared.description)
        self.assertIn(SMARTLINK_CTA, prepared.description)
        self.assertNotIn("facebook.com/SadhguruBulgarian", prepared.description)
        self.assertNotIn("instagram.com/sadhguru.bulgarian", prepared.description)
        self.assertIn("садгуру българия", prepared.tags)
        self.assertNotIn("духовно развитие", prepared.tags)

    def test_quote_image_youtube_uses_first_sentence_title_and_full_description(self) -> None:
        quote = (
            "Животът е относно съзнателност, а не относно грижи, неосъзнати подтици или "
            "конфликти. Нека следващите месеци донесат онази дълбочина на човешкото "
            "съществувание, която води до блажен живот."
        )
        post_caption = build_quote_post_caption(quote)
        job = PublishJob(
            title=build_quote_youtube_title(post_caption),
            description=build_quote_youtube_description(post_caption),
            video_format="short_form",
            content_kind="image",
        )
        prepared = prepare_publish_job(job, "youtube")
        self.assertEqual(
            prepared.title,
            "Животът е относно съзнателност, а не относно грижи, неосъзнати подтици или конфликти. #Садгуру",
        )
        self.assertTrue(prepared.description.startswith("#садгуру\n"))
        self.assertIn("Нека следващите месеци", prepared.description)
        self.assertNotIn(SMARTLINK_CTA, prepared.description)
        self.assertNotIn("#Садгуру", prepared.description)
        self.assertTrue(prepared.title.endswith("#Садгуру"))

    def test_build_quote_youtube_title_truncates_long_first_sentence(self) -> None:
        long_sentence = "А" * 120 + "."
        title = build_quote_youtube_title(long_sentence)
        self.assertLessEqual(len(title), 100)
        self.assertTrue(title.endswith("... #Садгуру"))

    def test_build_quote_post_caption_matches_facebook_template(self) -> None:
        quote = (
            "Свободата означава да имате силата да направлявате собствения си живот, "
            "а не той да бъде предрешаван от каквото и да било."
        )
        self.assertEqual(
            build_quote_post_caption(quote),
            f"{quote} #Садгуру",
        )

    def test_quote_image_instagram_uses_full_caption(self) -> None:
        job = PublishJob(
            title="Quote title",
            description=(
                "Животът е относно съзнателност, а не относно грижи, неосъзнати подтици или "
                "конфликти. Нека следващите месеци донесат онази дълбочина на човешкото "
                "съществувание, която води до блажен живот."
            ),
            video_format="short_form",
            content_kind="image",
        )
        prepared = prepare_publish_job(job, "instagram")
        self.assertIn("Нека следващите месеци", prepared.description)
        self.assertIn("#Садгуру", prepared.description)
        self.assertNotIn(SMARTLINK_CTA, prepared.description)
        self.assertNotIn("[#Садгуру]", prepared.description)

    def test_short_form_youtube_template(self) -> None:
        job = PublishJob(
            title="Накарайте всичко да работи за вас",
            description="Садгуру обяснява защо балансът е най-важното качество.",
            video_format="short_form",
        )
        prepared = prepare_publish_job(job, "youtube")
        self.assertEqual(
            prepared.title,
            "Накарайте всичко да работи за вас | Садгуру #shorts",
        )
        self.assertEqual(
            prepared.description,
            "#shorts #садгуру\n"
            "Садгуру обяснява защо балансът е най-важното качество.\n\n"
            f"{SMARTLINK_CTA}",
        )
        self.assertIn("садгуру българия", prepared.tags)
        self.assertNotIn("духовно развитие", prepared.tags)

    def test_short_form_facebook_caption(self) -> None:
        job = PublishJob(
            title="Накарайте всичко да работи за вас",
            description="Садгуру обяснява защо балансът е най-важното качество.",
            video_format="short_form",
        )
        prepared = prepare_publish_job(job, "facebook")
        self.assertEqual(
            prepared.description,
            "Накарайте всичко да работи за вас. "
            "Садгуру обяснява защо балансът е най-важното качество. #Садгуру\n\n"
            f"{SMARTLINK_CTA}",
        )

    def test_long_form_facebook_uses_hashtag_caption(self) -> None:
        job = PublishJob(
            title="Когато Садгуру откри Нагамани – бижуто на кобрата",
            description="Основен текст.",
            video_url="https://youtu.be/wCnnfKRycwI",
            video_format="post",
        )
        prepared = prepare_publish_job(job, "facebook")
        self.assertEqual(
            prepared.description,
            "Когато Садгуру откри Нагамани – бижуто на кобрата. Основен текст. #Садгуру\n\n"
            f"{SMARTLINK_CTA}",
        )
        self.assertNotIn("Original video:", prepared.description)

    def test_short_form_youtube_uses_title_when_description_missing(self) -> None:
        job = PublishJob(
            title="Накарайте всичко да работи за вас",
            description="",
            video_format="short_form",
        )
        prepared = prepare_publish_job(job, "youtube")
        self.assertEqual(
            prepared.description,
            "#shorts #садгуру\nНакарайте всичко да работи за вас\n\n"
            f"{SMARTLINK_CTA}",
        )

    def test_long_form_youtube_uses_title_when_description_missing(self) -> None:
        job = PublishJob(
            title="Заглавие на видеото",
            description="",
            video_url="https://youtu.be/original",
            video_format="post",
        )
        prepared = prepare_publish_job(job, "youtube")
        self.assertIn("Заглавие на видеото", prepared.description)
        self.assertIn("Original video:", prepared.description)

    def test_build_short_form_youtube_title_truncates_to_limit(self) -> None:
        long_title = "Животът е относно съзнателност, а не относно грижи, неосъзнати подтици или конфликти."
        prepared = build_short_form_youtube_title(long_title)
        self.assertLessEqual(len(prepared), 100)
        self.assertTrue(prepared.endswith("#shorts"))

    def test_build_short_form_youtube_title(self) -> None:
        self.assertEqual(
            build_short_form_youtube_title("Накарайте всичко да работи за вас"),
            "Накарайте всичко да работи за вас | Садгуру #shorts",
        )

    def test_build_short_form_youtube_description(self) -> None:
        self.assertEqual(
            build_short_form_youtube_description(
                "Садгуру обяснява защо балансът е най-важното качество."
            ),
            "#shorts #садгуру\n"
            "Садгуру обяснява защо балансът е най-важното качество.\n\n"
            f"{SMARTLINK_CTA}",
        )

    def test_build_short_form_social_caption(self) -> None:
        self.assertEqual(
            build_short_form_social_caption(
                PublishJob(
                    title="Заглавие",
                    description="Описание на видеото.",
                    video_format="short_form",
                )
            ),
            "Заглавие. Описание на видеото. #Садгуру\n\n"
            f"{SMARTLINK_CTA}",
        )

    def test_build_long_form_social_caption(self) -> None:
        self.assertEqual(
            build_long_form_social_caption(
                PublishJob(
                    title="Когато Садгуру откри Нагамани – бижуто на кобрата",
                    description=(
                        "Садгуру разказва за един увлекателен случай, при който е открил нагамани."
                    ),
                    video_format="post",
                )
            ),
            "Когато Садгуру откри Нагамани – бижуто на кобрата. "
            "#Садгуру разказва за един увлекателен случай, при който е открил нагамани.\n\n"
            f"{SMARTLINK_CTA}",
        )
        self.assertEqual(
            build_long_form_social_caption(
                PublishJob(
                    title="Когато Садгуру откри Нагамани – бижуто на кобрата",
                    description="Основен текст.",
                    video_format="post",
                )
            ),
            "Когато Садгуру откри Нагамани – бижуто на кобрата. #Садгуру Основен текст.\n\n"
            f"{SMARTLINK_CTA}",
        )

    def test_inject_published_video_url(self) -> None:
        description = (
            "Основен текст.\n\n"
            "Original video: https://youtu.be/wCnnfKRycwI\n\n"
            "Footer"
        )
        self.assertEqual(
            inject_published_video_url(description, "ewFr6ZU1Jnk"),
            "Основен текст.\n\n"
            "https://youtu.be/ewFr6ZU1Jnk\n\n"
            "Original video: https://youtu.be/wCnnfKRycwI\n\n"
            "Footer",
        )

    def test_build_long_form_description_without_body(self) -> None:
        text = build_long_form_description(
            PublishJob(
                title="Заглавие",
                video_url="https://youtu.be/original",
                video_format="post",
            )
        )
        self.assertIn("Заглавие", text)
        self.assertIn("Original video:", text)
        self.assertIn("Доброволец от Иша:", text)
        self.assertIn(SMARTLINK_CTA, text)
        self.assertNotIn("facebook.com/SadhguruBulgarian", text)


if __name__ == "__main__":
    unittest.main()
