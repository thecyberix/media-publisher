from __future__ import annotations

import unittest

from media_publisher.models import PublishJob
from media_publisher.post_templates import (
    build_long_form_description,
    build_quote_post_caption,
    build_quote_youtube_description,
    build_quote_youtube_title,
    build_short_form_social_caption,
    build_short_form_youtube_description,
    build_short_form_youtube_title,
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
        self.assertIn("facebook.com/SadhguruBulgarian", prepared.description)
        self.assertIn("instagram.com/sadhguru.bulgarian", prepared.description)
        self.assertIn("youtube.com/channel/UCg8jXnEr8ZKmuwm3S9J4e-Q", prepared.description)
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
            "Животът е относно съзнателност, а не относно грижи, неосъзнати подтици или конфликти.",
        )
        self.assertTrue(prepared.description.startswith("#садгуру\n"))
        self.assertIn("Нека следващите месеци", prepared.description)
        self.assertNotIn("#Садгуру", prepared.description)
        self.assertNotIn("#Садгуру", prepared.title)

    def test_build_quote_youtube_title_truncates_long_first_sentence(self) -> None:
        long_sentence = "А" * 120 + "."
        title = build_quote_youtube_title(long_sentence)
        self.assertLessEqual(len(title), 100)
        self.assertTrue(title.endswith("..."))
        self.assertNotIn("#Садгуру", title)

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
            "#shorts\n#садгуру\nНакарайте всичко да работи за вас",
        )
        self.assertIn("духовно развитие", prepared.tags)
        self.assertNotIn("садгуру българия", prepared.tags)

    def test_short_form_facebook_caption(self) -> None:
        job = PublishJob(
            title="Накарайте всичко да работи за вас",
            description="Садгуру обяснява защо балансът е най-важното качество.",
            video_format="short_form",
        )
        prepared = prepare_publish_job(job, "facebook")
        self.assertEqual(
            prepared.description,
            "Накарайте всичко да работи за вас. [#Садгуру] "
            "Садгуру обяснява защо балансът е най-важното качество.",
        )

    def test_long_form_facebook_uses_social_footer(self) -> None:
        job = PublishJob(
            title="Когато Садгуру откри Нагамани – бижуто на кобрата",
            description="Основен текст.",
            video_url="https://youtu.be/wCnnfKRycwI",
            video_format="post",
        )
        prepared = prepare_publish_job(job, "facebook")
        self.assertIn("Основен текст.", prepared.description)
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
            build_short_form_youtube_description("Накарайте всичко да работи за вас"),
            "#shorts\n#садгуру\nНакарайте всичко да работи за вас",
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
            "Заглавие. [#Садгуру] Описание на видеото.",
        )

    def test_build_long_form_description_without_body(self) -> None:
        text = build_long_form_description(
            PublishJob(
                title="Заглавие",
                video_url="https://youtu.be/original",
                video_format="post",
            )
        )
        self.assertIn("Original video:", text)
        self.assertIn("facebook.com/SadhguruBulgarian", text)


if __name__ == "__main__":
    unittest.main()
