"""Load TARGET_LANGUAGE and PUBLISH_JSON before language-aware modules import.

`python -m unittest discover -s tests -t .` loads this package. CI injects the
repository variables into the job env; locally they come from `.env` or fallbacks.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

_GITHUB_API_VERSION = "2022-11-28"
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _github_actions_variables() -> dict[str, str]:
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    token = (
        os.getenv("GITHUB_TOKEN", "").strip()
        or os.getenv("GH_TOKEN", "").strip()
    )
    if not repo or not token:
        return {}
    url = f"https://api.github.com/repos/{repo}/actions/variables?per_page=100"
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", _GITHUB_API_VERSION)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return {}
    items = payload.get("variables", []) if isinstance(payload, dict) else []
    result: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if isinstance(name, str) and isinstance(value, str) and name.strip():
            result[name.strip()] = value
    return result


def _first_language_name() -> str:
    path = _REPO_ROOT / "config" / "languages.json"
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    for name in payload:
        if isinstance(name, str) and name.strip():
            return name.strip()
    return ""


def _ensure_test_env() -> None:
    need_language = not os.getenv("TARGET_LANGUAGE", "").strip()
    need_publish = not os.getenv("PUBLISH_JSON", "").strip()
    if not need_language and not need_publish:
        return

    fetched = _github_actions_variables()
    if need_language:
        language = fetched.get("TARGET_LANGUAGE", "").strip()
        if language:
            os.environ["TARGET_LANGUAGE"] = language
            need_language = False
    if need_publish:
        publish_json = fetched.get("PUBLISH_JSON", "").strip()
        if publish_json:
            os.environ["PUBLISH_JSON"] = publish_json
            need_publish = False

    in_actions = os.getenv("GITHUB_ACTIONS", "").strip().lower() == "true"
    if in_actions and (need_language or need_publish):
        missing = []
        if need_language:
            missing.append("TARGET_LANGUAGE")
        if need_publish:
            missing.append("PUBLISH_JSON")
        raise RuntimeError(
            "Could not read "
            + " and ".join(missing)
            + " from GitHub Actions repository variables. "
            "Set them on the repo and inject them into the CI job env."
        )

    if need_language:
        language = _first_language_name()
        if language:
            os.environ["TARGET_LANGUAGE"] = language
    if need_publish:
        os.environ["PUBLISH_JSON"] = (
            '{"timezone":"Europe/Sofia","quotes_hour":8,"videos_hour":18}'
        )


_ensure_test_env()
