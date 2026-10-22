from __future__ import annotations

import io
import json
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

from media_publisher.transient_retry import (
    call_with_transient_retry,
    delay_before_retry,
    execute_urllib_request,
    http_status_from_exception,
    is_transient_exception,
    is_transient_http_status,
    retry_after_seconds,
)


class _FakeResp:
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = headers or {}


class _FakeHttpError(Exception):
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        super().__init__(f"HttpError {status}")
        self.resp = _FakeResp(status, headers)


class TransientRetryTests(unittest.TestCase):
    def test_is_transient_http_status(self) -> None:
        self.assertTrue(is_transient_http_status(500))
        self.assertTrue(is_transient_http_status(429))
        self.assertFalse(is_transient_http_status(404))
        self.assertFalse(is_transient_http_status(None))

    def test_http_status_from_google_style_error(self) -> None:
        self.assertEqual(http_status_from_exception(_FakeHttpError(500)), 500)

    def test_retry_after_seconds(self) -> None:
        exc = _FakeHttpError(429, {"retry-after": "2.5"})
        self.assertEqual(retry_after_seconds(exc), 2.5)

    def test_is_transient_for_internal_error_text(self) -> None:
        self.assertTrue(is_transient_exception(RuntimeError("Internal Error")))
        self.assertFalse(is_transient_exception(RuntimeError("permission denied")))

    def test_call_retries_transient_then_succeeds(self) -> None:
        sleeps: list[float] = []
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise _FakeHttpError(500)
            return "ok"

        result = call_with_transient_retry(
            flaky,
            attempts=3,
            base_delay_seconds=1.0,
            max_delay_seconds=10.0,
            sleep=sleeps.append,
            rng=__import__("random").Random(0),
        )
        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len(sleeps), 2)

    def test_call_does_not_retry_permanent_errors(self) -> None:
        fn = Mock(side_effect=_FakeHttpError(404))
        with self.assertRaises(_FakeHttpError):
            call_with_transient_retry(fn, attempts=3, sleep=lambda _: None)
        self.assertEqual(fn.call_count, 1)

    def test_call_raises_after_attempts_exhausted(self) -> None:
        fn = Mock(side_effect=_FakeHttpError(503))
        with self.assertRaises(_FakeHttpError):
            call_with_transient_retry(fn, attempts=2, sleep=lambda _: None)
        self.assertEqual(fn.call_count, 2)

    def test_delay_honors_retry_after_cap(self) -> None:
        delay = delay_before_retry(
            1,
            retry_after=100.0,
            max_delay_seconds=20.0,
        )
        self.assertEqual(delay, 20.0)


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.example/token",
        code=code,
        msg="Error",
        hdrs=None,
        fp=io.BytesIO(body),
    )


def _ok_body(payload: dict[str, object]) -> Mock:
    response = Mock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    return response


class ExecuteUrllibRequestTests(unittest.TestCase):
    def test_retries_503_then_returns_body(self) -> None:
        calls = {"n": 0}
        logs: list[str] = []

        def urlopen(request: urllib.request.Request, timeout: float = 60) -> Mock:
            calls["n"] += 1
            if calls["n"] < 3:
                raise _http_error(503, b'{"error":"service unavailable"}')
            return _ok_body({"access_token": "new"})

        with patch(
            "media_publisher.transient_retry.urllib.request.urlopen",
            side_effect=urlopen,
        ):
            raw = execute_urllib_request(
                lambda: urllib.request.Request("https://api.example/token"),
                timeout=60,
                attempts=5,
                sleep=lambda _delay: None,
                log=logs.append,
                operation="Canva token exchange",
            )
        self.assertEqual(json.loads(raw), {"access_token": "new"})
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len(logs), 2)
        self.assertIn("HTTP 503", logs[0])

    def test_does_not_retry_http_400(self) -> None:
        calls = {"n": 0}

        def urlopen(request: urllib.request.Request, timeout: float = 60) -> Mock:
            calls["n"] += 1
            raise _http_error(400, b'{"error":"invalid_grant"}')

        with patch(
            "media_publisher.transient_retry.urllib.request.urlopen",
            side_effect=urlopen,
        ):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                execute_urllib_request(
                    lambda: urllib.request.Request("https://api.example/token"),
                    timeout=60,
                    attempts=5,
                    sleep=lambda _delay: None,
                )
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(getattr(ctx.exception, "detail_text"), '{"error":"invalid_grant"}')
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
