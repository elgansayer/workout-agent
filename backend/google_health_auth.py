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
  3. Run:  python google_health_auth.py [--user-id <account id>]
       - It prints an authorise URL. Open it, approve the weight scope.
       - Google redirects back to this script, which captures the code, swaps it
         for tokens, and seals the refresh token in the credential vault for
         --user-id. Without --user-id nothing is stored outside the explicit
         non-production local mode, because there is no shared account to attach
         it to. The token is never printed or written to .env.

Uses the Authorization Code Grant. Only stdlib + requests are needed.
"""

from __future__ import annotations

import os
import secrets
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

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
    nonce: str | None = None,
    code_challenge: str | None = None,
) -> str:
    """Build the Google consent URL the user opens in their browser."""
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        # offline + consent guarantee a refresh token is returned.
        "access_type": "offline",
        "prompt": "consent",
    }
    if nonce:
        params["nonce"] = nonce
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    *,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    code_verifier: str | None = None,
) -> dict[str, Any] | None:
    """Swap an authorisation code for access and refresh tokens."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if code_verifier:
        data["code_verifier"] = code_verifier
    try:
        response = requests.post(
            TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=data,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            print("Google returned non-object token JSON.", file=sys.stderr)
            return None
        return data
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

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.code = (params.get("code") or [None])[0]  # type: ignore[list-item]
        _CallbackHandler.state = (params.get("state") or [None])[0]  # type: ignore[list-item]
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

    def log_message(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None:  # silence the default logging
        pass


def _wait_for_code(redirect_uri: str) -> tuple[str | None, str | None]:
    parsed = urllib.parse.urlparse(redirect_uri)
    server = HTTPServer(
        (parsed.hostname or "localhost", parsed.port or 80),
        _CallbackHandler,
    )
    print("Waiting for Google to redirect back...")
    server.handle_request()  # blocks until the single callback arrives
    server.server_close()
    return _CallbackHandler.code, _CallbackHandler.state


def _store_refresh_token(refresh_token: str, user_id: str | None) -> str:
    """Persist the refresh token for one account and describe the outcome.

    The token is stored under *user_id* only. With no user (and outside the
    explicit local mode) it is not stored at all rather than being attached to a
    shared fallback account.
    """
    from tenancy import TenantRequiredError

    try:
        from database import set_meta

        db_path = os.environ.get("DATABASE_PATH", "workout_agent.db").strip()
        set_meta(
            "google_health_refresh_token", refresh_token, db_path, user_id=user_id
        )
    except TenantRequiredError:
        return " (not saved to the database: pass --user-id to store it for one account)"
    except Exception:  # noqa: BLE001
        return ""
    return f" (also saved to {db_path})"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="One-time Google Health authorisation helper."
    )
    parser.add_argument(
        "--user-id",
        help="Store the refresh token in the database for this account id.",
    )
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    client_id = os.environ.get("GOOGLE_HEALTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_HEALTH_CLIENT_SECRET", "").strip()
    redirect_uri = (
        os.environ.get("GOOGLE_HEALTH_REDIRECT_URI", "").strip() or DEFAULT_REDIRECT_URI
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

    from connectors.oauth import generate_code_verifier, generate_nonce, pkce_challenge

    state = secrets.token_urlsafe(16)
    nonce = generate_nonce()
    code_verifier = generate_code_verifier()
    url = build_authorize_url(
        client_id,
        state,
        redirect_uri=redirect_uri,
        nonce=nonce,
        code_challenge=pkce_challenge(code_verifier),
    )

    print("\nOpen this URL and approve access (the weight scope):\n")
    print(url, "\n")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001, S110
        pass

    code, returned_state = _wait_for_code(redirect_uri)
    if not code:
        print("No authorisation code was captured. Try again.", file=sys.stderr)
        return 1
    if not secrets.compare_digest(
        (returned_state or "").encode("utf-8"), state.encode("utf-8")
    ):
        print("State mismatch; aborting for safety.", file=sys.stderr)
        return 1

    tokens = exchange_code(
        client_id,
        client_secret,
        code,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
    )
    if not tokens or not tokens.get("refresh_token"):
        print(
            "Could not obtain a refresh token. Make sure the consent screen "
            "granted offline access (access_type=offline, prompt=consent).",
            file=sys.stderr,
        )
        return 1

    refresh_token = tokens["refresh_token"]

    # Seal it in the credential vault so the very next agent run is authorised.
    # The token is never printed or written to the environment.
    stored = _store_refresh_token(refresh_token, args.user_id)
    if "not saved" in stored:
        return 1

    print(f"\nSuccess! The Google Health token is sealed in the credential vault{stored}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
