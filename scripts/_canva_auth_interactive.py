"""Interactive Canva OAuth with localhost callback (media_publisher token path)."""
from __future__ import annotations

import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from media_publisher.__main__ import canva_client_from_settings
from media_publisher.config import load_settings
from media_publisher.sources.canva import CanvaError, format_access_token_scopes, missing_canva_scopes


def main() -> int:
    settings = load_settings(PROJECT_ROOT)
    try:
        client = canva_client_from_settings(settings)
    except CanvaError as exc:
        print(f"Canva setup failed: {exc}")
        return 1

    redirect_uri = client.redirect_uri
    parsed_redirect = urlparse(redirect_uri)
    callback_path = parsed_redirect.path or "/callback"
    port = parsed_redirect.port or 8765

    result: dict[str, str | None] = {"code": None, "state": None, "error": None}

    class OAuthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != callback_path:
                self.send_response(404)
                self.end_headers()
                return
            params = parse_qs(parsed.query)
            result["code"] = params.get("code", [None])[0]
            result["state"] = params.get("state", [None])[0]
            result["error"] = params.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if result["error"]:
                body = f"<h1>Canva authorization failed</h1><p>{result['error']}</p>"
            else:
                body = (
                    "<h1>Canva authorization complete</h1>"
                    "<p>You can close this tab and return to the terminal.</p>"
                )
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, format: str, *args) -> None:
            return

    server = HTTPServer(("127.0.0.1", port), OAuthHandler)
    thread = Thread(target=server.handle_request, daemon=True)
    thread.start()

    try:
        auth_url = client.start_authorization()
    except CanvaError as exc:
        print(f"Canva authorization setup failed: {exc}")
        return 1

    print("Open this URL in a browser and authorize the integration:")
    print(auth_url)
    print(f"Waiting for OAuth callback on {redirect_uri} ...")
    webbrowser.open(auth_url, new=1, autoraise=True)

    thread.join(timeout=300)
    server.server_close()

    if result["error"]:
        print(f"Canva authorization failed: {result['error']}")
        return 1
    if not result["code"]:
        print("Timed out waiting for authorization callback.")
        return 1

    try:
        token = client.complete_authorization(result["code"], state=result["state"])
    except CanvaError as exc:
        print(f"Canva authorization failed: {exc}")
        return 1

    print(f"Canva token saved to {settings.canva_token!r}.")
    print(f"Granted scopes: {format_access_token_scopes(token.access_token)}")
    missing = missing_canva_scopes(token.access_token)
    if missing:
        print(
            "Warning: missing scopes "
            f"{', '.join(missing)}. Enable them in the Canva Developer Portal and re-auth."
        )
    print()
    print("Update GitHub secret CANVA_TOKEN_JSON with credentials/canva-token.json contents.")
    from media_publisher.runtime_env import maybe_persist_canva_token

    try:
        message = maybe_persist_canva_token(PROJECT_ROOT)
        if message:
            print(message)
    except RuntimeError as exc:
        print(f"Warning: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
