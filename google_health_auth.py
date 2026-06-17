"""One-time Google Health authorisation helper.

The agent keeps itself authorised by reusing the OAuth refresh token on every run
(see google_health_client.py), but you have to grant access once to obtain the
first refresh token. This script walks through that one-time flow:

  1. Create a project and OAuth client at
     https://developers.google.com/health/setup
       - "Where are you calling from?": Web Server
       - Add an Authorized redirect URI of  http://localhost:8080/
         (Google allows several; this one lets this script capture the code
         locally. The console also asks for https://www.google.com, which is
         fine to keep alongside it.)
       - Add the health_metrics_and_measurements.readonly scope, and add your
         Google account under "Test users".
  2. Put GOOGLE_HEALTH_CLIENT_ID and GOOGLE_HEALTH_CLIENT_SECRET in your .env
     (or export them).
  3. Run:  python google_health_auth.py
       - It prints an authorise URL. Open it, approve the weight scope.
       - Google redirects back to this script, which captures the code, swaps it
         for tokens, prints the refresh token, and stores it in the database.
  4. Paste the printed GOOGLE_HEALTH_REFRESH_TOKEN into your .env.

Uses the Authorization Code Grant. Only stdlib + requests are needed.
"""

from __future__ import annotations

import os
import secrets
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from google_health_client import SCOPE, TOKEN_URL

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/auth"
DEFAULT_REDIRECT_URI = "http://localhost:8080/"
REQUEST_TIMEOUT = 15


def build_authorize_url(
    client_id: str,
    state: str,
    *,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    scope: str = SCOPE,
) -> str:
    """Build the Google consent URL the user opens in their browser."""
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            # offline + consent guarantee a refresh token is returned.
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    *,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
) -> dict | None:
    """Swap an authorisation code for access and refresh tokens."""
    try:
        response = requests.post(
            TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        print(f"Token exchange failed: {exc}", file=sys.stderr)
        if exc.response is not None:
            print(exc.response.text, file=sys.stderr)
        return None
    except ValueError:
        print("Google returned invalid token JSON.", file=sys.stderr)
        return None


class _CallbackHandler(BaseHTTPRequestHandler):
    code: str | None = None
    state: str | None = None

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.code = (params.get("code") or [None])[0]
        _CallbackHandler.state = (params.get("state") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = (
            "<h2>Google Health authorised.</h2><p>You can close this tab and "
            "return to the terminal.</p>"
            if _CallbackHandler.code
            else "<h2>No authorisation code received.</h2>"
        )
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *_args) -> None:  # silence the default logging
        pass


def _wait_for_code(redirect_uri: str) -> tuple[str | None, str | None]:
    parsed = urllib.parse.urlparse(redirect_uri)
    server = HTTPServer(
        (parsed.hostname or "localhost", parsed.port or 80), _CallbackHandler
    )
    print("Waiting for Google to redirect back...")
    server.handle_request()  # blocks until the single callback arrives
    server.server_close()
    return _CallbackHandler.code, _CallbackHandler.state


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    client_id = os.environ.get("GOOGLE_HEALTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_HEALTH_CLIENT_SECRET", "").strip()
    redirect_uri = (
        os.environ.get("GOOGLE_HEALTH_REDIRECT_URI", "").strip()
        or DEFAULT_REDIRECT_URI
    )

    if not client_id or not client_secret:
        print(
            "Set GOOGLE_HEALTH_CLIENT_ID and GOOGLE_HEALTH_CLIENT_SECRET first (in "
            ".env or the environment). Create an OAuth client at "
            "https://developers.google.com/health/setup with the redirect URI "
            f"{redirect_uri}",
            file=sys.stderr,
        )
        return 1

    state = secrets.token_urlsafe(16)
    url = build_authorize_url(client_id, state, redirect_uri=redirect_uri)

    print("\nOpen this URL and approve access (the weight scope):\n")
    print(url, "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    code, returned_state = _wait_for_code(redirect_uri)
    if not code:
        print("No authorisation code was captured. Try again.", file=sys.stderr)
        return 1
    if returned_state != state:
        print("State mismatch; aborting for safety.", file=sys.stderr)
        return 1

    tokens = exchange_code(client_id, client_secret, code, redirect_uri=redirect_uri)
    if not tokens or not tokens.get("refresh_token"):
        print(
            "Could not obtain a refresh token. Make sure the consent screen "
            "granted offline access (access_type=offline, prompt=consent).",
            file=sys.stderr,
        )
        return 1

    refresh_token = tokens["refresh_token"]

    # Store it so the very next agent run is already authorised.
    try:
        from database import set_meta

        db_path = os.environ.get("DATABASE_PATH", "workout_agent.db").strip()
        set_meta("google_health_refresh_token", refresh_token, db_path)
        stored = f" (also saved to {db_path})"
    except Exception:
        stored = ""

    print("\nSuccess! Add this line to your .env:\n")
    print(f"GOOGLE_HEALTH_REFRESH_TOKEN={refresh_token}")
    print(f"\nThe token is now valid{stored}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
