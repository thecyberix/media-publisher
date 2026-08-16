"""Test-only defaults so unit tests can import language-aware modules without .env.

`python -m unittest discover -s tests` does not load this file; CI uses
`discover -s tests -t .` plus TARGET_LANGUAGE / PUBLISH_JSON in the job env.
"""

from __future__ import annotations

import os

os.environ.setdefault("TARGET_LANGUAGE", "Bulgarian")
os.environ.setdefault(
    "PUBLISH_JSON",
    '{"timezone":"Europe/Sofia","quotes_hour":8,"videos_hour":18}',
)
