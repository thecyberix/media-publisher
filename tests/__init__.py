"""Test-only defaults so unit tests can import language-aware modules without .env."""

from __future__ import annotations

import os

os.environ.setdefault("TARGET_LANGUAGE", "Bulgarian")
os.environ.setdefault("PUBLISH_TIMEZONE", "Europe/Sofia")
