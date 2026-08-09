from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from catalog_parser.smartcat_web import SmartcatWebClient


class ResolveFilledSkipTests(unittest.TestCase):
    def test_resolve_skips_when_any_segment_has_translation(self) -> None:
        client = SmartcatWebClient(
            storage_state_path=MagicMock(),
        )
        document = {
            "id": "doc-filled",
            "name": "Filled Doc",
            "targets": [
                {
                    "languageId": 1026,
                    "workflowStages": [
                        {
                            "type": 1,
                            "progress": 0.0,
                            "wordsTranslated": 0,
                            "translatedCharsWithoutSpaces": 0,
                        }
                    ],
                }
            ],
        }
        segments = [
            {"id": 1, "targets": [{"languageId": 1026, "text": ""}]},
            {"id": 2, "targets": [{"languageId": 1026, "text": "Едно"}]},
        ]
        with (
            patch.object(client, "_find_document", return_value=document),
            patch(
                "catalog_parser.smartcat_write.list_document_segments",
                return_value=segments,
            ) as list_segments,
        ):
            link = client._resolve_with_session(
                MagicMock(url="https://ea.smartcat.com/projects/x"),
                "https://ea.smartcat.com/projects/proj/files?folderMode=true&search=Filled",
                "proj",
                search="Filled",
                title="Filled Doc",
                language="bg",
            )
        self.assertIsNone(link)
        list_segments.assert_called_once()

    def test_resolve_returns_editor_link_when_segments_empty(self) -> None:
        client = SmartcatWebClient(
            storage_state_path=MagicMock(),
        )
        document = {
            "id": "doc-empty",
            "name": "Empty Doc",
            "targets": [
                {
                    "languageId": 1026,
                    "workflowStages": [
                        {
                            "type": 1,
                            "progress": 0.0,
                            "wordsTranslated": 0,
                            "translatedCharsWithoutSpaces": 0,
                        }
                    ],
                }
            ],
        }
        segments = [
            {"id": 1, "targets": [{"languageId": 1026, "text": ""}]},
            {"id": 2, "targets": [{"languageId": 1026, "text": ""}]},
        ]
        with (
            patch.object(client, "_find_document", return_value=document),
            patch(
                "catalog_parser.smartcat_write.list_document_segments",
                return_value=segments,
            ),
        ):
            link = client._resolve_with_session(
                MagicMock(url="https://ea.smartcat.com/projects/x"),
                "https://ea.smartcat.com/projects/proj/files?folderMode=true&search=Empty",
                "proj",
                search="Empty",
                title="Empty Doc",
                language="bg",
            )
        self.assertIsInstance(link, str)
        self.assertIn("open-editor/doc-empty", link)


if __name__ == "__main__":
    unittest.main()
