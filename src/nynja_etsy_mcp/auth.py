"""One-time Etsy OAuth 2.0 (Authorization Code + PKCE) login.

Usage:  uv run nynja-etsy-auth            # opens/prints the consent URL, catches the redirect
        uv run nynja-etsy-auth --manual   # paste the redirected URL instead of running a listener
        uv run nynja-etsy-auth --status   # show what's stored
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser

import httpx

from .config import AUTH_URL, TOKEN_URL, load_settings
from .tokens import TokenStore


def make_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url(keystring: str, redirect_uri: str, scopes: str, state: str, challenge: str) -> str:
    params = {
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "client_id": keystring,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"


def exchange_code(keystring: str, redirect_uri: str, code: str, verifier: str) -> dict:
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": keystring,
            "redirect_uri": redirect_uri,
            "code": code,
            "code_verifier": verifier,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise SystemExit(f"Token exchange failed ({resp.status_code}): {resp.text}")
    return resp.json()


def _wait_for_redirect(redirect_uri: str, expected_state: str, timeout_s: int = 300) -> str:
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.hostname not in ("localhost", "127.0.0.1"):
        raise SystemExit("Listener mode needs a localhost redirect URI. Use --manual for other callbacks.")
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in q or "error" in q:
                result.update({k: v[0] for k, v in q.items()})
                ok = "code" in q and q.get("state", [""])[0] == expected_state
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                msg = "Shop connected. You can close this tab." if ok else f"Authorization failed: {result}"
                self.wfile.write(f"<h2>{msg}</h2>".encode())
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):  # silence
            pass

    server = http.server.HTTPServer(("0.0.0.0", parsed.port or 80), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.time() + timeout_s
    while not result and time.time() < deadline:
        time.sleep(0.25)
    server.shutdown()
    if not result:
        raise SystemExit("Timed out waiting for Etsy to redirect back. Re-run, or use --manual.")
    if "error" in result:
        raise SystemExit(f"Etsy returned an error: {result}")
    if result.get("state") != expected_state:
        raise SystemExit("State mismatch: possible CSRF or a stale browser tab. Re-run the command.")
    return result["code"]


def _manual_code(expected_state: str) -> str:
    pasted = input("\nPaste the FULL URL your browser landed on after approving:\n> ").strip()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    if q.get("state", [""])[0] != expected_state:
        raise SystemExit("State mismatch. Re-run and paste the URL from this attempt.")
    if "code" not in q:
        raise SystemExit(f"No ?code= in that URL: {q}")
    return q["code"][0]


def fetch_shop_id(settings, access_token: str) -> str | None:
    try:
        r = httpx.get(
            "https://api.etsy.com/v3/application/users/me",
            headers={"x-api-key": settings.api_key_header, "Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        if r.status_code == 200:
            sid = r.json().get("shop_id")
            return str(sid) if sid else None
        print(f"Warning: /users/me returned {r.status_code}: {r.text}", file=sys.stderr)
    except httpx.HTTPError as e:  # pragma: no cover
        print(f"Warning: could not look up shop_id: {e}", file=sys.stderr)
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Connect your Etsy shop (OAuth PKCE).")
    ap.add_argument("--manual", action="store_true", help="paste the redirect URL instead of running a listener")
    ap.add_argument("--status", action="store_true", help="show stored token status and exit")
    ap.add_argument("--no-browser", action="store_true", help="print the URL only")
    args = ap.parse_args()

    settings = load_settings()
    store = TokenStore(settings.token_file)

    if args.status:
        if not store.exists():
            print(f"No tokens at {store.path}")
            return
        d = store.load()
        left = int(float(d.get("expires_at", 0)) - time.time())
        print(f"Token file: {store.path}\nuser_id: {d.get('user_id')}  shop_id: {d.get('shop_id')}")
        print(f"access token: {'valid for ' + str(left // 60) + ' min' if left > 0 else 'expired (auto-refreshes on next call)'}")
        print(f"scopes: {d.get('scopes')}")
        return

    settings.require_credentials()
    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(16)
    url = build_auth_url(settings.keystring, settings.redirect_uri, settings.scopes, state, challenge)

    print("\n1) Open this URL, sign in as the shop owner, and click 'Allow access':\n")
    print(url + "\n")
    if not args.no_browser:
        with __import__("contextlib").suppress(Exception):
            webbrowser.open(url)

    code = _manual_code(state) if args.manual else _wait_for_redirect(settings.redirect_uri, state)
    print("2) Exchanging code for tokens...")
    tokens = TokenStore.from_token_response(exchange_code(settings.keystring, settings.redirect_uri, code, verifier))
    tokens["scopes"] = settings.scopes
    tokens["shop_id"] = settings.shop_id or fetch_shop_id(settings, tokens["access_token"])
    store.save(tokens)
    print(f"3) Saved to {store.path} (chmod 600). user_id={tokens['user_id']} shop_id={tokens['shop_id']}")
    print("Done. Claude Code and the HTTP server will refresh the token automatically for ~90 days.")


if __name__ == "__main__":
    main()
