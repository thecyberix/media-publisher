from __future__ import annotations

import os
from pathlib import Path

# Environment variables whose values are written to credential files before startup.
# Used by GitHub Actions (secrets) and optional local overrides; ignored when unset.
CREDENTIAL_ENV_FILES: dict[str, str] = {
    "YOUTUBE_CLIENT_SECRETS_JSON": "credentials/youtube-client.json",
    "YOUTUBE_TOKEN_JSON": "credentials/youtube-token.json",
    "CANVA_TOKEN_JSON": "credentials/canva-token.json",
    "HAPPYSCRIBE_BROWSER_STATE_JSON": "credentials/happyscribe-browser.json",
}


def materialize_credentials(project_root: Path) -> list[Path]:
    """Write credential JSON files from *_JSON environment variables.

    When an env var is set, its contents replace the target file. When unset,
    existing local files are left unchanged.
    """
    written: list[Path] = []
    for env_name, relative_path in CREDENTIAL_ENV_FILES.items():
        payload = os.getenv(env_name, "").strip()
        if not payload:
            continue
        destination = project_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload, encoding="utf-8")
        written.append(destination)
    return written
