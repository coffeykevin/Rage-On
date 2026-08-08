"""
Minimal Spotify Web API client for this project.

Uses the Authorization Code flow with a long-lived refresh token stored in
GitHub Actions secrets — no browser needed on the runner. Run
`python -m src.get_token` once locally to obtain the refresh token.

Scopes required: playlist-modify-public
"""

from __future__ import annotations

import base64
import difflib
import os
import re
import time

import requests

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"


class Spotify:
    def __init__(self) -> None:
        self.client_id = os.environ["SPOTIFY_CLIENT_ID"]
        self.client_secret = os.environ["SPOTIFY_CLIENT_SECRET"]
        self.refresh_token = os.environ["SPOTIFY_REFRESH_TOKEN"]
        self._access_token: str | None = None
        self._user_id: str | None = None

    # -- auth ---------------------------------------------------------------

    def _auth_header(self) -> dict:
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        return {"Authorization": f"Basic {basic}"}

    def _ensure_token(self) -> None:
        if self._access_token:
            return
        resp = requests.post(
            TOKEN_URL,
            headers=self._auth_header(),
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            timeout=30,
        )
        resp.raise_for_status()
        self._access_token = resp.json()["access_token"]

    def _request(self, method: str, path: str, **kwargs) -> dict:
        self._ensure_token()
        url = path if path.startswith("http") else API + path
        for attempt in range(4):
            resp = requests.request(
                method,
                url,
                headers={"Authorization": f"Bearer {self._access_token}"},
                timeout=30,
                **kwargs,
            )
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "2")) + 1
                time.sleep(wait)
                continue
            if resp.status_code == 401:
                self._access_token = None
                self._ensure_token()
                continue
            resp.raise_for_status()
            return resp.json() if resp.text else {}
        resp.raise_for_status()
        return {}

    # -- user / playlists ---------------------------------------------------

    @property
    def user_id(self) -> str:
        if not self._user_id:
            self._user_id = self._request("GET", "/me")["id"]
        return self._user_id

    def find_or_create_playlist(self, name: str, description: str) -> str:
        """Return the playlist ID for `name`, creating it (public) if needed.

        Listing uses /me/playlists; some development-mode apps get 403 on
        listing endpoints, in which case we skip straight to creation (the
        caller persists the ID in state.json, so this happens at most once
        per playlist).
        """
        try:
            url = "/me/playlists?limit=50"
            while url:
                page = self._request("GET", url)
                for pl in page.get("items", []):
                    if pl and pl.get("name") == name:
                        return pl["id"]
                url = page.get("next")
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            if status not in (403, 404):
                raise
            print(f"  ! listing playlists denied ({status}); creating directly")
        created = self._request(
            "POST",
            f"/users/{self.user_id}/playlists",
            json={"name": name, "public": True, "description": description},
        )
        print(f"Created playlist: {name}")
        return created["id"]

    def playlist_track_uris(self, playlist_id: str) -> set[str]:
        uris: set[str] = set()
        url = f"/playlists/{playlist_id}/tracks?fields=items(track(uri)),next&limit=100"
        while url:
            page = self._request("GET", url)
            for item in page.get("items", []):
                track = item.get("track") or {}
                if track.get("uri"):
                    uris.add(track["uri"])
            url = page.get("next")
        return uris

    def add_tracks(self, playlist_id: str, uris: list[str]) -> None:
        for i in range(0, len(uris), 100):
            self._request(
                "POST",
                f"/playlists/{playlist_id}/tracks",
                json={"uris": uris[i : i + 100]},
            )

    # -- search / matching --------------------------------------------------

    @staticmethod
    def _norm(s: str) -> str:
        s = s.lower()
        s = re.sub(r"\(feat\.?[^)]*\)|feat\.?\s+\S+", "", s)
        s = re.sub(r"[^a-z0-9 ]", "", s)
        return re.sub(r"\s+", " ", s).strip()

    def match_track(self, artist: str, title: str, threshold: float = 0.75) -> str | None:
        """Search Spotify and return the best-matching track URI, or None."""
        query = f"track:{title} artist:{artist}"
        results = self._request(
            "GET", "/search", params={"q": query, "type": "track", "limit": 5}
        )
        items = results.get("tracks", {}).get("items", [])
        if not items:
            # Looser fallback search
            results = self._request(
                "GET",
                "/search",
                params={"q": f"{artist} {title}", "type": "track", "limit": 5},
            )
            items = results.get("tracks", {}).get("items", [])

        want_artist, want_title = self._norm(artist), self._norm(title)
        best_uri, best_score = None, 0.0
        for item in items:
            got_title = self._norm(item["name"])
            got_artists = [self._norm(a["name"]) for a in item["artists"]]
            title_score = difflib.SequenceMatcher(None, want_title, got_title).ratio()
            artist_score = max(
                (difflib.SequenceMatcher(None, want_artist, a).ratio() for a in got_artists),
                default=0.0,
            )
            score = 0.5 * title_score + 0.5 * artist_score
            if score > best_score:
                best_score, best_uri = score, item["uri"]
        return best_uri if best_score >= threshold else None
