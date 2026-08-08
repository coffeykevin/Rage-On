"""
Verifies the three Spotify secrets work together, without touching playlists.

Usage:
  export SPOTIFY_CLIENT_ID=... SPOTIFY_CLIENT_SECRET=... SPOTIFY_REFRESH_TOKEN=...
  python3 -m src.check_auth

Prints exactly what Spotify says, so a bad value is identifiable:
  - "invalid_client"  -> client ID or secret is wrong
  - "invalid_grant"   -> refresh token is wrong, revoked, or was minted by
                         a different app than this client ID/secret pair
"""

from __future__ import annotations

import base64
import os
import sys

import requests


def main() -> int:
    problems = False
    values = {}
    for name in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REFRESH_TOKEN"):
        raw = os.environ.get(name)
        if not raw:
            print(f"✗ {name} is not set")
            problems = True
            continue
        value = raw.strip().strip("'\"")
        if value != raw:
            print(f"! {name} had surrounding whitespace/quotes — using trimmed value")
        if "=" in value and name in value:
            print(f"✗ {name} contains '{name}=' — save only the value itself")
            problems = True
        values[name] = value
        print(f"  {name}: {len(value)} chars, starts {value[:4]}…")

    if problems or len(values) < 3:
        return 1

    if len(values["SPOTIFY_CLIENT_ID"]) != 32:
        print("! client ID is usually exactly 32 characters — double-check it")

    basic = base64.b64encode(
        f"{values['SPOTIFY_CLIENT_ID']}:{values['SPOTIFY_CLIENT_SECRET']}".encode()
    ).decode()
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={"Authorization": f"Basic {basic}"},
        data={
            "grant_type": "refresh_token",
            "refresh_token": values["SPOTIFY_REFRESH_TOKEN"],
        },
        timeout=30,
    )
    if resp.ok:
        scope = resp.json().get("scope", "")
        print(f"\n✓ Auth works. Granted scope: {scope or '(none reported)'}")
        missing = [
            s
            for s in ("playlist-modify-public", "playlist-modify-private")
            if s not in scope
        ]
        if missing:
            print(f"✗ …but scope lacks {' and '.join(missing)} — re-run src.get_token")
            return 1
        return 0
    print(f"\n✗ Spotify rejected the credentials ({resp.status_code}): {resp.text}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
