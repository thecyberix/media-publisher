from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Environment variables whose values are written to credential files before startup.
# Used by GitHub Actions (secrets) and optional local overrides; ignored when unset.
CREDENTIAL_ENV_FILES: dict[str, str] = {
    "YOUTUBE_CLIENT_SECRETS_JSON": "credentials/youtube-client.json",
    "YOUTUBE_TOKEN_JSON": "credentials/youtube-token.json",
    "CANVA_TOKEN_JSON": "credentials/canva-token.json",
    "HAPPYSCRIBE_BROWSER_STATE_JSON": "credentials/happyscribe-browser.json",
}

CANVA_TOKEN_RELATIVE_PATH = CREDENTIAL_ENV_FILES["CANVA_TOKEN_JSON"]
INITIAL_CREDENTIAL_JSON: dict[str, str] = {}


def materialize_credentials(project_root: Path) -> list[Path]:
    """Write credential JSON files from *_JSON environment variables.

    When an env var is set, its contents replace the target file. When unset,
    existing local files are left unchanged.
    """
    INITIAL_CREDENTIAL_JSON.clear()
    written: list[Path] = []
    for env_name, relative_path in CREDENTIAL_ENV_FILES.items():
        payload = os.getenv(env_name, "").strip()
        if not payload:
            continue
        destination = project_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload, encoding="utf-8")
        INITIAL_CREDENTIAL_JSON[relative_path] = payload
        written.append(destination)
    return written


def maybe_persist_canva_token(project_root: Path) -> str | None:
    """Write a rotated Canva token back to GitHub Secrets after CI refresh.

  Set CANVA_TOKEN_SYNC_PAT to a PAT with repository secret write access.
  In GitHub Actions, GITHUB_REPOSITORY is provided automatically.
    """
    sync_pat = os.getenv("CANVA_TOKEN_SYNC_PAT", "").strip()
    if not sync_pat:
        return None

    repository = os.getenv("GITHUB_REPOSITORY", "").strip()
    if not repository:
        return None

    token_path = project_root / CANVA_TOKEN_RELATIVE_PATH
    if not token_path.is_file():
        return None

    current = token_path.read_text(encoding="utf-8").strip()
    if not current:
        return None

    initial = INITIAL_CREDENTIAL_JSON.get(CANVA_TOKEN_RELATIVE_PATH)
    if initial is not None and initial.strip() == current:
        return None

    result = subprocess.run(
        [
            "gh",
            "secret",
            "set",
            "CANVA_TOKEN_JSON",
            "--repo",
            repository,
            "-f",
            str(token_path),
        ],
        env={**os.environ, "GH_TOKEN": sync_pat},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"Failed to update CANVA_TOKEN_JSON secret: {detail}")

    return "Updated CANVA_TOKEN_JSON GitHub secret after Canva token refresh."
