"""
Orchestrates a sync run: scrape → dedupe → match on Spotify → add to the
half-yearly playlists → update state.

Two playlists are maintained per half-year:

  "ABC Rage H2 2026"                 — everything aired in that half
  "ABC Rage H2 2026 New songs only"  — only tracks NOT seen in rage play
                                       history within the previous 12 months

State lives in data/state.json and is committed back to the repo by the
GitHub Actions workflow, so the repo itself is the database.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from .scrape import Track, scrape_all
from .spotify import Spotify

STATE_PATH = Path("data/state.json")
UNMATCHED_PATH = Path("data/unmatched.json")

NEW_SONG_WINDOW = timedelta(days=365)

PLAYLIST_DESCRIPTION = (
    "Tracks aired on ABC's rage, synced automatically. "
    "Unofficial fan project — not affiliated with the ABC. "
    "https://github.com/coffeykevin/Rage-On"
)
NEW_ONLY_DESCRIPTION = (
    "Tracks aired on ABC's rage that hadn't appeared on rage in the "
    "previous 12 months. Unofficial fan project — not affiliated with "
    "the ABC. https://github.com/coffeykevin/Rage-On"
)


def playlist_name(air_date_iso: str) -> str:
    d = date.fromisoformat(air_date_iso)
    half = "H1" if d.month <= 6 else "H2"
    return f"ABC Rage {half} {d.year}"


def new_only_playlist_name(air_date_iso: str) -> str:
    return f"{playlist_name(air_date_iso)} New songs only"


def song_key(artist: str, title: str) -> str:
    """History key: artist+title with no date, for last-seen lookups."""
    norm = lambda s: re.sub(r"\s+", " ", s).strip().lower()
    return f"{norm(artist)}|{norm(title)}"


def is_new_to_rage(track: Track, history: dict[str, str]) -> bool:
    """True if this song hasn't aired on rage within NEW_SONG_WINDOW
    before this track's air date (or has never aired)."""
    last_seen = history.get(song_key(track.artist, track.title))
    if last_seen is None:
        return True
    gap = date.fromisoformat(track.air_date) - date.fromisoformat(last_seen)
    return gap > NEW_SONG_WINDOW


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def migrate_history(state: dict) -> None:
    """Seed the play-history map from processed keys written before the
    history feature existed (key format: artist|title|YYYY-MM-DD)."""
    history = state.setdefault("history", {})
    for key in state.get("processed", {}):
        parts = key.rsplit("|", 1)
        if len(parts) != 2:
            continue
        song, air = parts
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", air):
            continue
        if song not in history or air > history[song]:
            history[song] = air


def main() -> int:
    state: dict = load_json(STATE_PATH, {"processed": {}})
    unmatched: list = load_json(UNMATCHED_PATH, [])
    processed: dict = state["processed"]  # key -> spotify uri | null
    playlists: dict = state.setdefault("playlists", {})  # name -> playlist id
    migrate_history(state)
    history: dict = state["history"]      # artist|title -> last air date

    tracks = scrape_all()
    new_tracks = [t for t in tracks if t.key not in processed]
    print(f"\n{len(tracks)} scraped, {len(new_tracks)} new")

    if not new_tracks:
        print("Nothing to do.")
        return 0

    # Process in air-date order so the 12-month window is evaluated
    # against genuinely earlier plays.
    new_tracks.sort(key=lambda t: t.air_date)

    sp = Spotify()
    playlist_cache: dict[str, str] = {}       # name -> id
    existing_cache: dict[str, set[str]] = {}  # id -> uris already present
    added = added_new_only = matched_dupes = missed = 0

    def get_playlist(name: str, description: str) -> str:
        if name not in playlist_cache:
            pid = playlists.get(name)
            if not pid:
                pid = sp.find_or_create_playlist(name, description)
                playlists[name] = pid
            playlist_cache[name] = pid
            existing_cache[pid] = sp.playlist_track_uris(pid)
        return playlist_cache[name]

    def add_if_absent(pid: str, uri: str) -> bool:
        if uri in existing_cache[pid]:
            return False
        sp.add_tracks(pid, [uri])
        existing_cache[pid].add(uri)
        return True

    def record_history(t: Track) -> None:
        skey = song_key(t.artist, t.title)
        if history.get(skey, "") < t.air_date:
            history[skey] = t.air_date

    # Save state even if the run dies partway — playlist creation and
    # processed tracks must never be forgotten, or re-runs would duplicate.
    try:
        for t in new_tracks:
            fresh = is_new_to_rage(t, history)

            uri = sp.match_track(t.artist, t.title)
            if uri is None:
                missed += 1
                processed[t.key] = None
                unmatched.append(t.to_dict())
                # Still record the airing in history: the song DID air on
                # rage even though Spotify couldn't match it.
                record_history(t)
                print(f"  ✗ no match: {t.artist} — {t.title}")
                continue

            main_pid = get_playlist(playlist_name(t.air_date), PLAYLIST_DESCRIPTION)
            if add_if_absent(main_pid, uri):
                added += 1
                print(f"  ✓ {playlist_name(t.air_date)}: {t.artist} — {t.title}")
            else:
                matched_dupes += 1

            if fresh:
                new_pid = get_playlist(
                    new_only_playlist_name(t.air_date), NEW_ONLY_DESCRIPTION
                )
                if add_if_absent(new_pid, uri):
                    added_new_only += 1
                    print(f"    + new to rage → {new_only_playlist_name(t.air_date)}")

            processed[t.key] = uri
            record_history(t)
    finally:
        save_json(STATE_PATH, state)
        save_json(UNMATCHED_PATH, unmatched)
    print(
        f"\nDone. Added {added} (main), {added_new_only} (new-only), "
        f"already present {matched_dupes}, unmatched {missed} "
        f"(see data/unmatched.json)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
