"""Retry helpers for transient remote API failures.

Use at client boundaries for idempotent/read-mostly calls (Drive list/upload,
downloads, etc.). Do not wrap ambiguous publish writes unless the call is
clearly safe to repeat.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

DEFAULT_ATTEMPTS = 3
DEFAULT_BASE_DELAY_SECONDS = 1.0
DEFAULT_MAX_DELAY_SECONDS = 20.0
TRANSIENT_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def is_transient_http_status(status: int | None) -> bool:
    return status is not None and status in TRANSIENT_HTTP_STATUSES


def http_status_from_exception(exc: BaseException) -> int | None:
    """Best-effort HTTP status from googleapiclient / urllib / requests-like errors."""
    resp = getattr(exc, "resp", None)
    if resp is not None:
        status = getattr(resp, "status", None)
        if isinstance(status, int):
            return status
        if isinstance(status, str) and status.isdigit():
            return int(status)

    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    if isinstance(status, str) and status.isdigit():
        return int(status)

    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    return None


def retry_after_seconds(exc: BaseException) -> float | None:
    """Parse Retry-After when present (seconds only; HTTP-date ignored)."""
    headers = None
    resp = getattr(exc, "resp", None)
    if resp is not None:
        headers = getattr(resp, "headers", None) or resp
    if headers is None:
        headers = getattr(exc, "headers", None)
    if headers is None:
        return None

    raw = None
    try:
        raw = headers.get("retry-after")  # type: ignore[union-attr]
    except Exception:
        raw = None
    if raw is None:
        try:
            raw = headers.get("Retry-After")  # type: ignore[union-attr]
        except Exception:
            raw = None
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None
    if value < 0:
        return None
    return value


def is_transient_exception(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, BrokenPipeError)):
        return True
    if isinstance(
        exc,
        (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError),
    ):
        return False
    if isinstance(exc, OSError):
        # Includes many network failures on API client paths.
        return True

    status = http_status_from_exception(exc)
    if is_transient_http_status(status):
        return True

    # googleapiclient sometimes surfaces reason strings without a clean status.
    reason = str(getattr(exc, "reason", "") or "").casefold()
    message = str(exc).casefold()
    markers = (
        "internal error",
        "internalerror",
        "backend error",
        "rate limit",
        "ratelimit",
        "user rate limit exceeded",
        "temporarily unavailable",
        "unavailable",
        "timed out",
        "timeout",
        "connection reset",
        "connection aborted",
    )
    return any(marker in reason or marker in message for marker in markers)


def delay_before_retry(
    attempt: int,
    *,
    base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS,
    max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS,
    retry_after: float | None = None,
    rng: random.Random | None = None,
) -> float:
    """Exponential backoff with jitter. ``attempt`` is 1-based failed attempt count."""
    if retry_after is not None and retry_after > 0:
        return min(retry_after, max_delay_seconds)
    expo = min(max_delay_seconds, base_delay_seconds * (2 ** max(0, attempt - 1)))
    picker = rng.random if rng is not None else random.random
    jitter = 0.5 + picker()  # 0.5x .. 1.5x
    return min(max_delay_seconds, expo * jitter)


def call_with_transient_retry(
    fn: Callable[[], T],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS,
    max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    is_transient: Callable[[BaseException], bool] = is_transient_exception,
    rng: random.Random | None = None,
) -> T:
    """Call ``fn`` and retry on transient failures.

    Raises the last exception when attempts are exhausted or the error is not
    classified as transient.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except BaseException as exc:
            last_exc = exc
            if attempt >= attempts or not is_transient(exc):
                raise
            sleep(
                delay_before_retry(
                    attempt,
                    base_delay_seconds=base_delay_seconds,
                    max_delay_seconds=max_delay_seconds,
                    retry_after=retry_after_seconds(exc),
                    rng=rng,
                )
            )
    assert last_exc is not None
    raise last_exc
