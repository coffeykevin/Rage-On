"""
Orchestrates a sync run: scrape → dedupe → match on Spotify → add to the
half-yearly playlist → update state.

State lives in data/state.json and is committed back to the repo by the
GitHub Actions workflow, so the repo itself is the database.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

from .scrape import Track, scrape_all
from .spotify import Spotify

STATE_PATH = Path("data/state.json")
UNMATCHED_PATH = Path("data/unmatched.json")

PLAYLIST_DESCRIPTION = (
    "Tracks aired on ABC's rage, synced automatically. "
    "Unofficial fan project — not affiliated with the ABC. "
    "https://github.com/coffeykevin/Rage-On"
)


def playlist_name(air_date_iso: str) -> str:
    d = date.fromisoformat(air_date_iso)
    half = "H1" if d.month <= 6 else "H2"
    return f"ABC Rage {half} {d.year}"


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def main() -> int:
    state: dict = load_json(STATE_PATH, {"processed": {}})
    unmatched: list = load_json(UNMATCHED_PATH, [])
    processed: dict = state["processed"]  # key -> spotify uri | null
    playlists: dict = state.setdefault("playlists", {})  # name -> playlist id

    tracks = scrape_all()
    new_tracks = [t for t in tracks if t.key not in processed]
    print(f"\n{len(tracks)} scraped, {len(new_tracks)} new")

    if not new_tracks:
        print("Nothing to do.")
        return 0

    sp = Spotify()
    playlist_cache: dict[str, str] = {}       # name -> id
    existing_cache: dict[str, set[str]] = {}  # id -> uris already present
    added = matched_dupes = missed = 0

    # Save state even if the run dies partway — playlist creation and
    # processed tracks must never be forgotten, or re-runs would duplicate.
    try:
        for t in new_tracks:
            name = playlist_name(t.air_date)
            if name not in playlist_cache:
                pid = playlists.get(name)
                if not pid:
                    pid = sp.find_or_create_playlist(name, PLAYLIST_DESCRIPTION)
                    playlists[name] = pid
                playlist_cache[name] = pid
                existing_cache[pid] = sp.playlist_track_uris(pid)
            pid = playlist_cache[name]

            uri = sp.match_track(t.artist, t.title)
            if uri is None:
                missed += 1
                processed[t.key] = None
                unmatched.append(t.to_dict())
                print(f"  ✗ no match: {t.artist} — {t.title}")
                continue

            if uri in existing_cache[pid]:
                matched_dupes += 1
            else:
                sp.add_tracks(pid, [uri])
                existing_cache[pid].add(uri)
                added += 1
                print(f"  ✓ {name}: {t.artist} — {t.title}")
            processed[t.key] = uri
    finally:
        save_json(STATE_PATH, state)
        save_json(UNMATCHED_PATH, unmatched)
    print(
        f"\nDone. Added {added}, already present {matched_dupes}, "
        f"unmatched {missed} (see data/unmatched.json)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
