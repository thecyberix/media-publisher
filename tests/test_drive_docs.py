from __future__ import annotations

import unittest

from catalog_parser.drive_docs import (
    DEFAULT_YT_DESCRIPTION_FIELD,
    DEFAULT_YT_THUMBNAIL_FIELD,
    DEFAULT_YT_TITLE_FIELD,
    DESCRIPTION_TABLE_LABEL,
    THUMBNAIL_TABLE_LABEL,
    TITLE_YT_TABLE_LABEL,
    extract_drive_fields_from_document,
    extract_drive_file_id,
    extract_drive_folder_id,
    extract_labeled_fields_from_blocks,
    extract_table_value_from_grid,
    extract_yt_title_from_document,
    extract_yt_title_from_grid,
    table_matches_label,
    table_matches_title_yt,
    table_to_grid,
)


class DriveDocsParsingTests(unittest.TestCase):
    def test_extract_drive_folder_id(self) -> None:
        self.assertEqual(
            extract_drive_folder_id(
                "https://drive.google.com/drive/folders/1-F_9awPFn6ZUam22lkpyrR_a_TnnIYVy"
            ),
            "1-F_9awPFn6ZUam22lkpyrR_a_TnnIYVy",
        )

    def test_extract_drive_file_id(self) -> None:
        self.assertEqual(
            extract_drive_file_id(
                "https://drive.google.com/file/d/11gdzzdLnDbI5qdBs0SBIeAzcYDAeryh0/view"
            ),
            "11gdzzdLnDbI5qdBs0SBIeAzcYDAeryh0",
        )

    def test_extract_yt_title_from_grid(self) -> None:
        grid = [
            [TITLE_YT_TABLE_LABEL, "Value"],
            ["My YouTube Title", "Other"],
        ]
        self.assertEqual(extract_yt_title_from_grid(grid), "My YouTube Title")

    def test_table_matches_title_yt_from_heading(self) -> None:
        grid = [["", ""], ["My YouTube Title", ""]]
        self.assertTrue(table_matches_title_yt(grid, TITLE_YT_TABLE_LABEL))

    def test_extract_yt_title_from_document(self) -> None:
        document = {
            "body": {
                "content": [
                    {
                        "paragraph": {
                            "elements": [{"textRun": {"content": "TITLE - YT\n"}}],
                        }
                    },
                    {
                        "table": {
                            "tableRows": [
                                {
                                    "tableCells": [
                                        {"content": [{"paragraph": {"elements": []}}]},
                                        {"content": [{"paragraph": {"elements": []}}]},
                                    ]
                                },
                                {
                                    "tableCells": [
                                        {
                                            "content": [
                                                {
                                                    "paragraph": {
                                                        "elements": [
                                                            {
                                                                "textRun": {
                                                                    "content": "Translated title"
                                                                }
                                                            }
                                                        ]
                                                    }
                                                }
                                            ]
                                        },
                                        {"content": [{"paragraph": {"elements": []}}]},
                                    ]
                                },
                            ]
                        }
                    },
                ]
            }
        }
        self.assertEqual(extract_yt_title_from_document(document), "Translated title")

    def test_extract_drive_fields_from_document(self) -> None:
        document = {
            "body": {
                "content": [
                    {
                        "paragraph": {
                            "elements": [{"textRun": {"content": "TITLE - YT\n"}}],
                        }
                    },
                    {
                        "table": {
                            "tableRows": [
                                {
                                    "tableCells": [
                                        {"content": [{"paragraph": {"elements": []}}]},
                                        {"content": [{"paragraph": {"elements": []}}]},
                                    ]
                                },
                                {
                                    "tableCells": [
                                        {
                                            "content": [
                                                {
                                                    "paragraph": {
                                                        "elements": [
                                                            {
                                                                "textRun": {
                                                                    "content": "Translated title"
                                                                }
                                                            }
                                                        ]
                                                    }
                                                }
                                            ]
                                        },
                                        {"content": [{"paragraph": {"elements": []}}]},
                                    ]
                                },
                            ]
                        }
                    },
                    {
                        "paragraph": {
                            "elements": [{"textRun": {"content": "Description\n"}}],
                        }
                    },
                    {
                        "table": {
                            "tableRows": [
                                {
                                    "tableCells": [
                                        {"content": [{"paragraph": {"elements": []}}]},
                                        {"content": [{"paragraph": {"elements": []}}]},
                                    ]
                                },
                                {
                                    "tableCells": [
                                        {
                                            "content": [
                                                {
                                                    "paragraph": {
                                                        "elements": [
                                                            {
                                                                "textRun": {
                                                                    "content": "A longer description"
                                                                }
                                                            }
                                                        ]
                                                    }
                                                }
                                            ]
                                        },
                                        {"content": [{"paragraph": {"elements": []}}]},
                                    ]
                                },
                            ]
                        }
                    },
                ]
            }
        }
        fields = extract_drive_fields_from_document(document)
        self.assertEqual(fields[DEFAULT_YT_TITLE_FIELD], "Translated title")
        self.assertEqual(fields[DEFAULT_YT_DESCRIPTION_FIELD], "A longer description")

    def test_table_matches_description_label(self) -> None:
        grid = [["", ""], ["Description text", ""]]
        self.assertTrue(
            table_matches_label(grid, DESCRIPTION_TABLE_LABEL, DESCRIPTION_TABLE_LABEL)
        )

    def test_table_matches_thumbnail_label(self) -> None:
        grid = [["", ""], ["https://www.canva.com/design/DAF123abc/edit", ""]]
        self.assertTrue(
            table_matches_label(grid, THUMBNAIL_TABLE_LABEL, THUMBNAIL_TABLE_LABEL)
        )
        fields = extract_labeled_fields_from_blocks(
            iter(
                [
                    ("paragraph", THUMBNAIL_TABLE_LABEL),
                    ("table", grid),
                ]
            )
        )
        self.assertEqual(
            fields[DEFAULT_YT_THUMBNAIL_FIELD],
            "https://www.canva.com/design/DAF123abc/edit",
        )

    def test_table_to_grid(self) -> None:
        table = {
            "tableRows": [
                {
                    "tableCells": [
                        {
                            "content": [
                                {
                                    "paragraph": {
                                        "elements": [{"textRun": {"content": "A"}}],
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        self.assertEqual(table_to_grid(table), [["A"]])


if __name__ == "__main__":
    unittest.main()
