"""
One-time local helper: obtains a Spotify refresh token for the GitHub secret.

Usage:
  1. Create an app at https://developer.spotify.com/dashboard
     with redirect URI  http://127.0.0.1:8765/callback
  2. export SPOTIFY_CLIENT_ID=... SPOTIFY_CLIENT_SECRET=...
  3. python -m src.get_token
  4. A browser opens; approve access. The refresh token prints to the
     terminal. Save it as the SPOTIFY_REFRESH_TOKEN repo secret.
"""

from __future__ import annotations

import base64
import http.server
import os
import secrets
import threading
import urllib.parse
import webbrowser

import requests

CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
REDIRECT = "http://127.0.0.1:8765/callback"
# playlist-modify-private is ALSO needed to create playlists (Spotify treats
# "public" as "shown on profile"; creation itself needs the private scope),
# and playlist-read-private lets us list existing playlists.
SCOPE = "playlist-modify-public playlist-modify-private playlist-read-private"

code_holder: dict = {}
state = secrets.token_urlsafe(16)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        if params.get("state", [""])[0] == state and "code" in params:
            code_holder["code"] = params["code"][0]
            body = b"Done - you can close this tab."
        else:
            body = b"Auth failed or state mismatch."
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main() -> None:
    server = http.server.HTTPServer(("127.0.0.1", 8765), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()

    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(
        {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT,
            "scope": SCOPE,
            "state": state,
        }
    )
    print(f"Opening browser… if it doesn't open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    while "code" not in code_holder:
        pass

    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={"Authorization": f"Basic {basic}"},
        data={
            "grant_type": "authorization_code",
            "code": code_holder["code"],
            "redirect_uri": REDIRECT,
        },
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json()["refresh_token"]
    print("\nYour refresh token (save as SPOTIFY_REFRESH_TOKEN secret):\n")
    print(token)


if __name__ == "__main__":
    main()
