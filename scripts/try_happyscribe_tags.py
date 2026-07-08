"""Try adding tags to a HappyScribe transcription via API."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from media_publisher.config import load_settings
from media_publisher.sources.happyscribe import DEFAULT_USER_AGENT, HappyScribeClient

TEST_TRANSCRIPTION_ID = "2f6cecb118034d4fa5227ff329cd06d2"
TEST_TAG = "media-publisher-api-test"
ORG_ID = "3310225"


def raw_patch(api_key: str, transcription_id: str, body: dict) -> tuple[int, str]:
    url = f"https://www.happyscribe.com/api/v1/transcriptions/{transcription_id}"
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="PATCH")
    request.add_header("Authorization", f"Bearer {api_key}")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", DEFAULT_USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def raw_get(api_key: str, transcription_id: str) -> dict:
    client = HappyScribeClient(api_key, organization_id=ORG_ID)
    payload = client._request("GET", client._url(f"transcriptions/{transcription_id}"))
    return payload if isinstance(payload, dict) else {}


def main() -> int:
    settings = load_settings(PROJECT_ROOT)
    api_key = settings.happyscribe_api_key or ""
    transcription_id = sys.argv[1] if len(sys.argv) > 1 else TEST_TRANSCRIPTION_ID

    before = raw_get(api_key, transcription_id)
    before_tags = before.get("tags", [])
    print("BEFORE")
    print(f"  id: {before.get('id')}")
    print(f"  name: {before.get('name')}")
    print(f"  tags: {before_tags}")

    attempts = [
        (
            "PATCH transcription.tags (top-level)",
            {"tags": list(dict.fromkeys([*before_tags, TEST_TAG]))},
        ),
        (
            "PATCH transcription.transcription.tags",
            {
                "transcription": {
                    "organization_id": ORG_ID,
                    "tags": list(dict.fromkeys([*before_tags, TEST_TAG])),
                }
            },
        ),
        (
            "PATCH transcription wrapper tags",
            {
                "transcription": {
                    "organization_id": ORG_ID,
                },
                "tags": list(dict.fromkeys([*before_tags, TEST_TAG])),
            },
        ),
    ]

    print()
    for label, body in attempts:
        status, response = raw_patch(api_key, transcription_id, body)
        after = raw_get(api_key, transcription_id)
        after_tags = after.get("tags", [])
        print(f"ATTEMPT: {label}")
        print(f"  HTTP {status}")
        print(f"  response snippet: {response[:300]}")
        print(f"  tags after: {after_tags}")
        if TEST_TAG in after_tags:
            print("  SUCCESS: test tag present")
            # Restore original tags
            restore_body = {
                "transcription": {
                    "organization_id": ORG_ID,
                    "tags": before_tags,
                }
            }
            raw_patch(api_key, transcription_id, restore_body)
            raw_patch(api_key, transcription_id, {"tags": before_tags})
            final = raw_get(api_key, transcription_id)
            print(f"  restored tags: {final.get('tags', [])}")
            return 0
        print()

    print("No attempt added the test tag via PATCH.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
