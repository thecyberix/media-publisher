from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from catalog_parser.airtable import (
    FIELD_COMBINED_MEDIA_FILE,
    FIELD_PLAYLISTS,
    FIELD_TITLE,
    FIELD_TRANSLATED_SUBTITLES,
    FIELD_VIDEO_FOLDER,
)
from catalog_parser.drive_combine import DriveMediaFile
from catalog_parser.drive_mix import MixToDriveResult, _playlists_after_end_cards
from catalog_parser.playlists import (
    END_CARD_INNER_ENGINEERING,
    END_CARD_MIRACLE_OF_MIND,
    END_CARD_SAVE_SOIL,
    PlaylistConfig,
    PlaylistRule,
    combined_media_airtable_fields,
    instagram_comments_for_playlists,
    load_playlist_config,
    youtube_playlist_ids_for_end_cards,
    youtube_playlists,
)
from catalog_parser.save_soil import EndCardOverlay
from catalog_parser.workflow.actions import _combine_media
from catalog_parser.workflow.rules import WorkflowAction, WorkflowActionType
from catalog_parser.workflow.table_cache import TableCache


def _sample_config() -> PlaylistConfig:
    return PlaylistConfig(
        rules=(
            PlaylistRule("Съзнателна Планета"),
            PlaylistRule("Вътрешно Изграждане", END_CARD_INNER_ENGINEERING),
            PlaylistRule("Спасете Почвата", END_CARD_SAVE_SOIL),
        )
    )


class YoutubePlaylistTests(unittest.TestCase):
    def test_rules_without_an_end_card_are_always_included(self) -> None:
        self.assertEqual(
            youtube_playlists(config=_sample_config()),
            ["Съзнателна Планета"],
        )

    def test_inner_engineering_card_adds_its_playlist(self) -> None:
        self.assertEqual(
            youtube_playlists([END_CARD_INNER_ENGINEERING], config=_sample_config()),
            ["Съзнателна Планета", "Вътрешно Изграждане"],
        )

    def test_save_soil_card_adds_its_playlist(self) -> None:
        self.assertEqual(
            youtube_playlists([END_CARD_SAVE_SOIL], config=_sample_config()),
            ["Съзнателна Планета", "Спасете Почвата"],
        )

    def test_both_cards_keep_config_order(self) -> None:
        self.assertEqual(
            youtube_playlists(
                [END_CARD_SAVE_SOIL, END_CARD_INNER_ENGINEERING],
                config=_sample_config(),
            ),
            [
                "Съзнателна Планета",
                "Вътрешно Изграждане",
                "Спасете Почвата",
            ],
        )

    def test_checked_in_config_lists_the_current_playlists(self) -> None:
        config = load_playlist_config()
        self.assertEqual(
            [(rule.name, rule.end_card, rule.comment) for rule in config.rules],
            [
                ("Съзнателна Планета", None, None),
                (
                    "Вътрешно Изграждане",
                    END_CARD_INNER_ENGINEERING,
                    '"Вътрешно изграждане" е онлайн програма на Садгуру от 7 стъпки, '
                    "която предлага практически инструменти за благосъстояние и "
                    "предаването на Шамбави Махамудра Крия – мощна 21-минутна "
                    'йогистка практика. Напишете "Вътрешно изграждане" в коментар, '
                    "за да научите повече.",
                ),
                ("Спасете Почвата", END_CARD_SAVE_SOIL, None),
                (
                    "Чудото на Ума",
                    END_CARD_MIRACLE_OF_MIND,
                    'Приложението "Чудото на ума" предлага мощна водена медитация, '
                    "проектирана от Садгуру, която ви помага да овладеете умственото "
                    "си благосъстояние. Отнема само 7 минути на ден. Напишете "
                    '"Чудото на ума" в коментар, за да започнете.',
                ),
            ],
        )
        self.assertEqual(config.quote_playlist, "Цитати")
        self.assertEqual(config.ekadashi_playlist, "Екадаши")
        names = youtube_playlists(config=config)
        self.assertNotIn("Цитати", names)
        self.assertNotIn("Екадаши", names)

    def test_end_card_rules_resolve_youtube_playlist_ids_by_title(self) -> None:
        ids = youtube_playlist_ids_for_end_cards(
            load_playlist_config().rules,
            [
                ("PL-default", "Съзнателна Планета"),
                ("PL-ie", "Вътрешно Изграждане"),
                ("PL-soil", "Спасете Почвата"),
                ("PL-mom", "Чудото на Ума"),
            ],
        )
        self.assertEqual(
            ids,
            {
                END_CARD_INNER_ENGINEERING: "PL-ie",
                END_CARD_SAVE_SOIL: "PL-soil",
                END_CARD_MIRACLE_OF_MIND: "PL-mom",
            },
        )

    def test_missing_youtube_playlist_title_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            youtube_playlist_ids_for_end_cards(
                (PlaylistRule("Спасете Почвата", END_CARD_SAVE_SOIL),),
                [("PL-other", "Other")],
            )

    def test_miracle_of_mind_animation_adds_its_playlist(self) -> None:
        self.assertEqual(
            youtube_playlists([END_CARD_MIRACLE_OF_MIND], config=load_playlist_config()),
            ["Съзнателна Планета", "Чудото на Ума"],
        )

    def test_comment_is_posted_only_for_the_matching_playlist(self) -> None:
        config = load_playlist_config()
        self.assertEqual(
            instagram_comments_for_playlists(["Вътрешно Изграждане"], config=config),
            [
                '"Вътрешно изграждане" е онлайн програма на Садгуру от 7 стъпки, '
                "която предлага практически инструменти за благосъстояние и "
                "предаването на Шамбави Махамудра Крия – мощна 21-минутна "
                'йогистка практика. Напишете "Вътрешно изграждане" в коментар, '
                "за да научите повече."
            ],
        )
        self.assertEqual(
            instagram_comments_for_playlists(["Чудото на Ума"], config=config),
            [
                'Приложението "Чудото на ума" предлага мощна водена медитация, '
                "проектирана от Садгуру, която ви помага да овладеете умственото "
                "си благосъстояние. Отнема само 7 минути на ден. Напишете "
                '"Чудото на ума" в коментар, за да започнете.'
            ],
        )
        self.assertEqual(
            instagram_comments_for_playlists(
                ["Съзнателна Планета", "Спасете Почвата"],
                config=config,
            ),
            [],
        )

    def test_load_rejects_an_unknown_end_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "playlists.json"
            path.write_text(
                json.dumps(
                    {"playlists": [{"name": "Always"}, {"name": "Other", "end_card": "nope"}]}
                ),
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                load_playlist_config(path)

    def test_airtable_payload_includes_playlists_with_the_file_link(self) -> None:
        drive_url = "https://drive.google.com/file/d/abc/view"
        names = ["Съзнателна Планета", "Вътрешно Изграждане"]
        self.assertEqual(
            combined_media_airtable_fields(drive_url, names),
            {
                FIELD_COMBINED_MEDIA_FILE: drive_url,
                FIELD_PLAYLISTS: names,
            },
        )


class EndCardPlaylistTests(unittest.TestCase):
    def test_detected_cards_become_playlist_names(self) -> None:
        video = Path("mixed.mp4")
        with patch(
            "catalog_parser.drive_mix.replace_save_soil_end_card_if_present",
            return_value=EndCardOverlay(video, True),
        ), patch(
            "catalog_parser.drive_mix.replace_inner_engineering_end_card_if_present",
            return_value=EndCardOverlay(video, False),
        ), patch(
            "catalog_parser.drive_mix.miracle_of_mind_end_card_if_present",
            return_value=EndCardOverlay(video, False),
        ):
            playlists = _playlists_after_end_cards(
                MagicMock(),
                video,
                work_dir=Path("tmp"),
                ffmpeg_path=None,
                video_type="Reel",
                reference_path=Path("REF 720p.mp4"),
            )
        config = load_playlist_config()
        expected = tuple(
            rule.name
            for rule in config.rules
            if rule.end_card is None or rule.end_card == END_CARD_SAVE_SOIL
        )
        self.assertEqual(playlists, expected)


class CombineMediaPlaylistWriteTests(unittest.TestCase):
    def test_playlists_share_the_combined_media_update(self) -> None:
        created = MixToDriveResult(
            uploaded=DriveMediaFile(
                id="file-id",
                name="clip.mp4",
                mime_type="video/mp4",
                parent_id="out",
            ),
            playlists=("Съзнателна Планета", "Спасете Почвата"),
        )
        table_cache = TableCache(
            [
                {
                    "id": "rec1",
                    "fields": {
                        FIELD_TITLE: "Clip",
                        FIELD_VIDEO_FOLDER: "https://drive.google.com/drive/folders/pkg",
                        FIELD_TRANSLATED_SUBTITLES: "https://drive.google.com/file/d/srt/view",
                    },
                }
            ]
        )
        airtable = MagicMock()
        action = WorkflowAction(
            action_type=WorkflowActionType.COMBINE_MEDIA,
            record_id="rec1",
        )
        with patch(
            "catalog_parser.workflow.actions.get_drive_service_noninteractive"
        ), patch(
            "catalog_parser.workflow.actions.combined_media_output_folder_id",
            return_value="out",
        ), patch(
            "catalog_parser.workflow.actions.mix_folder_media_to_drive",
            return_value=created,
        ):
            result = _combine_media(
                action,
                airtable=airtable,
                config=MagicMock(),
                dry_run=False,
                table_cache=table_cache,
            )

        self.assertTrue(result.success)
        airtable.update_record_fields.assert_called_once_with(
            "rec1",
            {
                FIELD_COMBINED_MEDIA_FILE: "https://drive.google.com/file/d/file-id/view",
                FIELD_PLAYLISTS: [
                    "Съзнателна Планета",
                    "Спасете Почвата",
                ],
            },
        )
        self.assertEqual(
            table_cache.get("rec1")["fields"][FIELD_PLAYLISTS],
            ["Съзнателна Планета", "Спасете Почвата"],
        )
