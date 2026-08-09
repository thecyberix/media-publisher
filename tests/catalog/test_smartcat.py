from __future__ import annotations

import unittest

from catalog_parser.smartcat import (
    build_smartcat_editor_link,
    bulgarian_target_needs_translation,
    document_has_language_target,
    find_bulgarian_srt_document,
    find_document_by_id,
    find_matching_document,
    language_matches,
    parse_pkg_sm_link,
    parse_smartcat_editor_link,
    parse_smartcat_resource_link,
    resolve_language_id,
)
from catalog_parser.smartcat_web import AnchorCandidate, pick_bulgarian_srt_href


class SmartcatLinkParsingTests(unittest.TestCase):
    def test_parse_pkg_sm_link_extracts_project_and_search(self) -> None:
        parsed = parse_pkg_sm_link(
            "https://ea.smartcat.com/projects/d1b6348b-541f-473a-9583-2a03d5315fef/"
            "files?folderMode=true&search=What%20Old%20Bread%20Does%20To%20Your%20Body"
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.project_id, "d1b6348b-541f-473a-9583-2a03d5315fef")
        self.assertEqual(parsed.search, "What Old Bread Does To Your Body")

    def test_parse_pkg_sm_link_extracts_search_when_first_query_param(self) -> None:
        parsed = parse_pkg_sm_link(
            "https://ea.smartcat.com/projects/d1b6348b-541f-473a-9583-2a03d5315fef/"
            "files?search=Most%20Indians%20Know%20Very%20Little%20of%20India%20%F0%9F%87%AE%F0%9F%87%B3"
            "&referenceFiles=false"
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.search, "Most Indians Know Very Little of India 🇮🇳")

    def test_find_matching_document_prefers_prefix_match(self) -> None:
        documents = [
            {"id": "1", "name": "What Old Bread Does To Your Body.srt"},
            {"id": "2", "name": "What Old Bread Does To Your Body.bg.srt"},
        ]
        match = find_matching_document(
            documents,
            search="What Old Bread Does To Your Body",
            title=None,
        )
        self.assertEqual(match, documents[0])

    def test_find_bulgarian_srt_document(self) -> None:
        documents = [
            {"id": "1", "name": "Stop Fighting Your Sweet Cravings.mp4"},
            {"id": "2", "name": "Stop Fighting Your Sweet Cravings.bg.srt"},
            {"id": "3", "name": "Stop Fighting Your Sweet Cravings.de.srt"},
        ]
        match = find_bulgarian_srt_document(
            documents,
            search="Stop Fighting Your Sweet Cravings",
            title=None,
        )
        self.assertEqual(match, documents[1])

    def test_language_matches_bulgarian_aliases(self) -> None:
        self.assertTrue(language_matches("Bulgarian", "bg"))
        self.assertTrue(language_matches("title.bg.srt", "bg"))
        self.assertFalse(language_matches("German", "bg"))

    def test_pick_bulgarian_srt_href(self) -> None:
        href = pick_bulgarian_srt_href(
            [
                AnchorCandidate(
                    href="https://ea.smartcat.com/files/stop.de.srt",
                    text="German",
                ),
                AnchorCandidate(
                    href="https://ea.smartcat.com/files/stop.bg.srt",
                    text="Bulgarian",
                ),
            ],
            search="Stop Fighting",
            title=None,
            language="bg",
        )
        self.assertEqual(href, "https://ea.smartcat.com/files/stop.bg.srt")

    def test_resolve_language_id_for_bulgarian(self) -> None:
        self.assertEqual(resolve_language_id("bg"), 1026)

    def test_document_has_language_target(self) -> None:
        document = {
            "id": "doc-1",
            "targets": [{"documentId": "doc-1", "languageId": 1026}],
        }
        self.assertTrue(document_has_language_target(document, 1026))
        self.assertFalse(document_has_language_target(document, 9))

    def test_bulgarian_segments_have_translation(self) -> None:
        from catalog_parser.smartcat import bulgarian_segments_have_translation

        empty = [
            {"targets": [{"languageId": 1026, "text": ""}]},
            {"targets": [{"languageId": 1026, "text": "  "}]},
        ]
        any_filled = [
            {"targets": [{"languageId": 1026, "text": ""}]},
            {"targets": [{"languageId": 1026, "text": "Едно"}]},
        ]
        self.assertFalse(bulgarian_segments_have_translation(empty, 1026))
        self.assertTrue(bulgarian_segments_have_translation(any_filled, 1026))

    def test_bulgarian_target_needs_translation(self) -> None:
        empty_target = {
            "languageId": 1026,
            "status": 0,
            "workflowStages": [
                {
                    "type": 1,
                    "progress": 0.0,
                    "translatedCharsWithoutSpaces": 0,
                    "wordsTranslated": 0,
                }
            ],
        }
        filled_target = {
            "languageId": 1026,
            "status": 1,
            "workflowStages": [
                {
                    "type": 1,
                    "progress": 100.0,
                    "translatedCharsWithoutSpaces": 763,
                    "wordsTranslated": 120,
                }
            ],
        }
        self.assertTrue(bulgarian_target_needs_translation(empty_target))
        self.assertFalse(bulgarian_target_needs_translation(filled_target))

    def test_build_smartcat_editor_link(self) -> None:
        pkg_sm_link = (
            "https://ea.smartcat.com/projects/d1b6348b-541f-473a-9583-2a03d5315fef/"
            "files?folderMode=true&search=In%20Any%20Arena%20Of%20Life%20No%20One%20Has%20Done%20"
            "Anything%20Truly%20Worthwhile%20Without%20Being%20Devoted%20To%20What%20They%20A"
        )
        expected = (
            "https://ea.smartcat.com/open-editor/823dc24f77812b1e698594b0?"
            "targetLanguageId=1026&backUrl=%2Fprojects%2Fd1b6348b-541f-473a-9583-2a03d5315fef%2Ffiles"
            "%3FfolderMode%3Dtrue%26search%3DIn%2520Any%2520Arena%2520Of%2520Life%2520No%2520One%2520Has%2520"
            "Done%2520Anything%2520Truly%2520Worthwhile%2520Without%2520Being%2520Devoted%2520To%2520What%2520They%2520A"
        )
        link = build_smartcat_editor_link(
            "https://ea.smartcat.com",
            "823dc24f77812b1e698594b0",
            language_id=1026,
            pkg_sm_link=pkg_sm_link,
        )
        self.assertEqual(link, expected)

    def test_parse_smartcat_editor_link(self) -> None:
        pkg_sm_link = (
            "https://ea.smartcat.com/projects/d1b6348b-541f-473a-9583-2a03d5315fef/"
            "files?folderMode=true&search=What%20Old%20Bread%20Does%20To%20Your%20Body"
        )
        editor_link = build_smartcat_editor_link(
            "https://ea.smartcat.com",
            "823dc24f77812b1e698594b0",
            language_id=1026,
            pkg_sm_link=pkg_sm_link,
        )
        parsed = parse_smartcat_editor_link(editor_link)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.document_id, "823dc24f77812b1e698594b0")
        self.assertEqual(parsed.target_language_id, 1026)
        self.assertEqual(parsed.project_id, "d1b6348b-541f-473a-9583-2a03d5315fef")
        self.assertEqual(parsed.search, "What Old Bread Does To Your Body")

    def test_find_document_by_id(self) -> None:
        documents = [
            {"id": "abc", "name": "One.srt"},
            {"id": "def", "name": "Two.srt"},
        ]
        self.assertEqual(find_document_by_id(documents, "def"), documents[1])


    def test_parse_legacy_smartcat_editor_document_id(self) -> None:
        parsed = parse_smartcat_resource_link(
            "https://ea.smartcat.com/editor?documentId=fac4d1f436d40b094c3ee72e"
            "&languageId=1026&backUrl=%2Fprojects%2Fd1b6348b-541f-473a-9583-2a03d5315fef%2Ffiles"
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.document_id, "fac4d1f436d40b094c3ee72e")
        self.assertEqual(parsed.target_language_id, 1026)
        self.assertEqual(parsed.project_id, "d1b6348b-541f-473a-9583-2a03d5315fef")

    def test_parse_legacy_smartcat_editor_back_url_only(self) -> None:
        parsed = parse_smartcat_resource_link(
            "https://ea.smartcat.com/editor?v=2&selectedStage=1&backUrl=%2Fprojects%2F"
            "d1b6348b-541f-473a-9583-2a03d5315fef%2Ffiles%3Fsearch%3DTest%2520Title"
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertIsNone(parsed.document_id)
        self.assertEqual(parsed.search, "Test Title")


if __name__ == "__main__":
    unittest.main()
