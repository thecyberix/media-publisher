"""Shared credential paths with media-publisher (Canva token, etc.)."""

from __future__ import annotations

from media_publisher.runtime_env import (
    CANVA_TOKEN_RELATIVE_PATH,
    INITIAL_CREDENTIAL_JSON,
    materialize_credentials,
    maybe_persist_canva_token,
    note_canva_token_baseline,
)

DEFAULT_CANVA_TOKEN_RELATIVE_PATH = CANVA_TOKEN_RELATIVE_PATH

__all__ = [
    "CANVA_TOKEN_RELATIVE_PATH",
    "DEFAULT_CANVA_TOKEN_RELATIVE_PATH",
    "INITIAL_CREDENTIAL_JSON",
    "materialize_credentials",
    "maybe_persist_canva_token",
    "note_canva_token_baseline",
]
