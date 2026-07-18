from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

# Environment variables whose values are written to credential files before startup.
# Used by GitHub Actions (secrets) and optional local overrides; ignored when unset.
CREDENTIAL_ENV_FILES: dict[str, str] = {
    "YOUTUBE_CLIENT_SECRETS_JSON": "credentials/youtube-client.json",
    "YOUTUBE_TOKEN_JSON": "credentials/youtube-token.json",
    "CANVA_TOKEN_JSON": "credentials/canva-token.json",
    "HAPPYSCRIBE_BROWSER_STATE_JSON": "credentials/happyscribe-browser.json",
    # Same secret name as catalog-parser (GOOGLE_SERVICE_ACCOUNT_JSON).
    "GOOGLE_SERVICE_ACCOUNT_JSON": "credentials/google-sheets-service-account.json",
    "GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON": "credentials/google-sheets-service-account.json",
}

CANVA_TOKEN_RELATIVE_PATH = CREDENTIAL_ENV_FILES["CANVA_TOKEN_JSON"]
YOUTUBE_TOKEN_RELATIVE_PATH = CREDENTIAL_ENV_FILES["YOUTUBE_TOKEN_JSON"]
DEFAULT_GITHUB_REPOSITORY = "thecyberix/media-publisher"
GITHUB_API_VERSION = "2022-11-28"
INITIAL_CREDENTIAL_JSON: dict[str, str] = {}
CANVA_TOKEN_BASELINE: str | None = None
YOUTUBE_TOKEN_BASELINE: str | None = None


def _canva_token_expires_at(payload: str) -> float | None:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("expires_at")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _should_keep_existing_canva_token(existing: str, incoming: str) -> bool:
    """Prefer an already-refreshed on-disk token over a stale job-scoped secret.

    Canva refresh tokens are single-use. Auth-check / earlier steps may rotate the
    token on disk, while later steps still inject the pre-refresh secret value.
    """
    existing_expires = _canva_token_expires_at(existing)
    incoming_expires = _canva_token_expires_at(incoming)
    if existing_expires is None or incoming_expires is None:
        return False
    return existing_expires > incoming_expires


def materialize_credentials(project_root: Path) -> list[Path]:
    """Write credential JSON files from *_JSON environment variables.

    When an env var is set, its contents replace the target file. When unset,
    existing local files are left unchanged.

    For Canva, a newer on-disk token (higher expires_at) is kept so a refresh in
    an earlier workflow step is not clobbered by the stale secret still present in
    that job's environment.
    """
    INITIAL_CREDENTIAL_JSON.clear()
    written: list[Path] = []
    stale_canva_secret: str | None = None
    for env_name, relative_path in CREDENTIAL_ENV_FILES.items():
        payload = os.getenv(env_name, "").strip()
        if not payload:
            continue
        destination = project_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if (
            env_name == "CANVA_TOKEN_JSON"
            and destination.is_file()
            and _should_keep_existing_canva_token(
                destination.read_text(encoding="utf-8").strip(),
                payload,
            )
        ):
            # Keep rotated disk token; remember the env secret as sync baseline.
            stale_canva_secret = payload
            INITIAL_CREDENTIAL_JSON[relative_path] = payload
            continue
        destination.write_text(payload, encoding="utf-8")
        INITIAL_CREDENTIAL_JSON[relative_path] = payload
        written.append(destination)
    note_canva_token_baseline(project_root)
    if stale_canva_secret is not None:
        # Force maybe_persist_canva_token to sync the fresher on-disk token back.
        global CANVA_TOKEN_BASELINE
        CANVA_TOKEN_BASELINE = stale_canva_secret
        INITIAL_CREDENTIAL_JSON[CANVA_TOKEN_RELATIVE_PATH] = stale_canva_secret
    return written


def note_canva_token_baseline(project_root: Path) -> None:
    """Remember the Canva token on disk at session start (before any refresh)."""
    global CANVA_TOKEN_BASELINE
    token_path = project_root / CANVA_TOKEN_RELATIVE_PATH
    if token_path.is_file():
        CANVA_TOKEN_BASELINE = token_path.read_text(encoding="utf-8").strip()
        return
    initial = INITIAL_CREDENTIAL_JSON.get(CANVA_TOKEN_RELATIVE_PATH)
    CANVA_TOKEN_BASELINE = initial.strip() if isinstance(initial, str) else None


def _canva_token_baseline() -> str | None:
    if CANVA_TOKEN_BASELINE is not None:
        return CANVA_TOKEN_BASELINE
    initial = INITIAL_CREDENTIAL_JSON.get(CANVA_TOKEN_RELATIVE_PATH)
    if isinstance(initial, str):
        return initial.strip()
    return None


def _github_repository() -> str | None:
    for env_name in ("GITHUB_REPOSITORY", "GITHUB_REPO"):
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    return DEFAULT_GITHUB_REPOSITORY


def _github_api_request(
    method: str,
    url: str,
    *,
    token: str,
    body: dict[str, str] | None = None,
) -> dict[str, object]:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", GITHUB_API_VERSION)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            if not raw:
                return {}
            parsed = json.loads(raw.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        message = f"GitHub API {exc.code} for {url}: {detail}"
        if exc.code == 403 and "/actions/secrets/" in url:
            message += (
                " Ensure CANVA_TOKEN_SYNC_PAT is a fine-grained PAT with "
                "Actions secrets: Read and write on this repository."
            )
        raise RuntimeError(message) from exc


def _encrypt_github_secret(public_key_b64: str, secret_value: str) -> str:
    try:
        from nacl import encoding, public
    except ImportError as exc:
        raise RuntimeError(
            "PyNaCl is required to update GitHub secrets without the gh CLI. "
            "Install with: pip install pynacl"
        ) from exc

    public_key = public.PublicKey(
        public_key_b64.encode("utf-8"),
        encoding.Base64Encoder(),
    )
    sealed_box = public.SealedBox(public_key)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def _set_github_actions_secret_via_gh(
    repository: str,
    secret_name: str,
    file_path: Path,
    *,
    token: str,
) -> None:
    gh_path = shutil.which("gh")
    if gh_path is None:
        raise RuntimeError("gh CLI not found")

    result = subprocess.run(
        [
            gh_path,
            "secret",
            "set",
            secret_name,
            "--repo",
            repository,
            "-f",
            str(file_path),
        ],
        env={**os.environ, "GH_TOKEN": token},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"Failed to update {secret_name} secret: {detail}")


def _set_github_actions_secret_file(
    repository: str,
    secret_name: str,
    file_path: Path,
    *,
    token: str,
) -> None:
    if shutil.which("gh") is not None:
        try:
            _set_github_actions_secret_via_gh(
                repository,
                secret_name,
                file_path,
                token=token,
            )
            return
        except RuntimeError:
            pass
    _set_github_actions_secret_file_api(
        repository,
        secret_name,
        file_path,
        token=token,
    )


def _set_github_actions_secret_file_api(
    repository: str,
    secret_name: str,
    file_path: Path,
    *,
    token: str,
) -> None:
    key_payload = _github_api_request(
        "GET",
        f"https://api.github.com/repos/{repository}/actions/secrets/public-key",
        token=token,
    )
    key_id = key_payload.get("key_id")
    public_key = key_payload.get("key")
    if not isinstance(key_id, str) or not isinstance(public_key, str):
        raise RuntimeError("GitHub secret public-key response was invalid")

    secret_value = file_path.read_text(encoding="utf-8")
    encrypted_value = _encrypt_github_secret(public_key, secret_value)
    _github_api_request(
        "PUT",
        f"https://api.github.com/repos/{repository}/actions/secrets/{secret_name}",
        token=token,
        body={"encrypted_value": encrypted_value, "key_id": key_id},
    )


def maybe_persist_canva_token(project_root: Path) -> str | None:
    """Write a rotated Canva token back to GitHub Secrets after local/CI refresh.

    Set CANVA_TOKEN_SYNC_PAT to a PAT with repository secret write access.
    GITHUB_REPOSITORY defaults to thecyberix/media-publisher when unset locally.
    """
    sync_pat = os.getenv("CANVA_TOKEN_SYNC_PAT", "").strip()
    if not sync_pat:
        return None

    repository = _github_repository()
    if not repository:
        return None

    token_path = project_root / CANVA_TOKEN_RELATIVE_PATH
    if not token_path.is_file():
        return None

    current = token_path.read_text(encoding="utf-8").strip()
    if not current:
        return None

    baseline = _canva_token_baseline()
    if baseline is not None and baseline == current:
        return None

    _set_github_actions_secret_file(
        repository,
        "CANVA_TOKEN_JSON",
        token_path,
        token=sync_pat,
    )

    global CANVA_TOKEN_BASELINE
    CANVA_TOKEN_BASELINE = current
    INITIAL_CREDENTIAL_JSON[CANVA_TOKEN_RELATIVE_PATH] = current

    return "Updated CANVA_TOKEN_JSON GitHub secret after Canva token refresh."


def _youtube_token_baseline() -> str | None:
    global YOUTUBE_TOKEN_BASELINE
    if YOUTUBE_TOKEN_BASELINE is not None:
        return YOUTUBE_TOKEN_BASELINE
    return INITIAL_CREDENTIAL_JSON.get(YOUTUBE_TOKEN_RELATIVE_PATH)


def maybe_persist_youtube_token(
    project_root: Path,
    *,
    force: bool = False,
) -> str | None:
    """Write a YouTube token back to GitHub Secrets after local re-auth/refresh.

    Uses the same CANVA_TOKEN_SYNC_PAT (repo secrets write access).
    Pass force=True after interactive re-auth so a revoked→new token always syncs.
    """
    sync_pat = os.getenv("CANVA_TOKEN_SYNC_PAT", "").strip()
    if not sync_pat:
        return None

    repository = _github_repository()
    if not repository:
        return None

    token_path = project_root / YOUTUBE_TOKEN_RELATIVE_PATH
    if not token_path.is_file():
        return None

    current = token_path.read_text(encoding="utf-8").strip()
    if not current:
        return None

    if not force:
        baseline = _youtube_token_baseline()
        if baseline is not None and baseline == current:
            return None

    _set_github_actions_secret_file(
        repository,
        "YOUTUBE_TOKEN_JSON",
        token_path,
        token=sync_pat,
    )

    global YOUTUBE_TOKEN_BASELINE
    YOUTUBE_TOKEN_BASELINE = current
    INITIAL_CREDENTIAL_JSON[YOUTUBE_TOKEN_RELATIVE_PATH] = current

    return "Updated YOUTUBE_TOKEN_JSON GitHub secret after YouTube token refresh."
