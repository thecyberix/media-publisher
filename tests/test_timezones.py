import unittest

from media_publisher.timezones import get_timezone


class TimezoneHelperTests(unittest.TestCase):
    def test_get_timezone_europe_sofia(self) -> None:
        tz = get_timezone("Europe/Sofia")
        self.assertEqual(str(tz), "Europe/Sofia")


if __name__ == "__main__":
    unittest.main()
