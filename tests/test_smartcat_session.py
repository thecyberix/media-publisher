from __future__ import annotations

import unittest

from catalog_parser.smartcat_web import _looks_like_login_url


class SmartcatSessionUrlTests(unittest.TestCase):
    def test_detects_login_urls(self) -> None:
        self.assertTrue(_looks_like_login_url("https://ea.smartcat.com/login"))
        self.assertTrue(_looks_like_login_url("https://ea.smartcat.com/sign-in"))
        self.assertTrue(_looks_like_login_url("https://auth.smartcat.com/oauth"))

    def test_allows_app_urls(self) -> None:
        self.assertFalse(_looks_like_login_url("https://ea.smartcat.com/projects"))
        self.assertFalse(_looks_like_login_url("https://ea.smartcat.com/"))


if __name__ == "__main__":
    unittest.main()
