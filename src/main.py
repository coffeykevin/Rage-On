"""
Orchestrates a sync run: scrape → dedupe → match on Spotify → add to the
half-yearly playlist → update state.

State lives in data/state.json and is committed back to the repo by the
GitHub Actions workflow, so the repo itself is the database.

Runs are resumable: work is bounded by a time budget (MAX_RUNTIME_MINUTES,
default 45) and by Spotify's rate-limit cool-downs; whatever isn't processed
this run is picked up by the next scheduled one.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

from .scrape import Track, scrape_all
from .spotify import RateLimitStall, Spotify

STATE_PATH = Path("data/state.json")
UNMATCHED_PATH = Path("data/unmatched.json")

PLAYLIST_DESCRIPTION = (
    "Tracks aired on ABC's rage, synced automatically. "
    "Unofficial fan project — not affiliated with the ABC. "
    "https://github.com/coffeykevin/Rage-On"
)

ADD_BATCH_SIZE = 100


def playlist_name(air_date_iso: str) -> str:
    d = date.fromisoformat(air_date_iso)
    half = "H1" if d.month <= 6 else "H2"
    return f"ABC Rage {half} {d.year}"


def song_key(artist: str, title: str) -> str:
    norm = lambda s: re.sub(r"\s+", " ", s).strip().lower()
    return f"{norm(artist)}|{norm(title)}"


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
    processed: dict = state["processed"]  # track key -> spotify uri | null
    playlists: dict = state.setdefault("playlists", {})  # name -> playlist id
    # song key -> uri | null; searches are expensive, rage repeats songs a lot
    song_cache: dict = state.setdefault("songs", {})

    deadline = time.monotonic() + 60 * float(os.environ.get("MAX_RUNTIME_MINUTES", "45"))

    tracks = scrape_all()
    new_tracks = [t for t in tracks if t.key not in processed]
    print(f"\n{len(tracks)} scraped, {len(new_tracks)} new")

    if not new_tracks:
        print("Nothing to do.")
        return 0

    sp = Spotify()
    playlist_cache: dict[str, str] = {}       # name -> id
    existing_cache: dict[str, set[str]] = {}  # id -> uris already present
    pending: dict[str, list[tuple[str, str]]] = {}  # id -> [(track key, uri)]
    added = matched_dupes = missed = out_of_time = 0

    def flush(pid: str) -> None:
        """Add queued uris to the playlist, then mark them processed.
        Order matters: only tracks that actually reached Spotify may be
        recorded in state, or a crash would strand them forever."""
        nonlocal added
        batch = pending.get(pid) or []
        if not batch:
            return
        uris = [uri for _, uri in batch]
        sp.add_tracks(pid, uris)
        for key, uri in batch:
            processed[key] = uri
            existing_cache[pid].add(uri)
        added += len(batch)
        pending[pid] = []
        print(f"  + added a batch of {len(batch)}")

    try:
        for t in new_tracks:
            if time.monotonic() > deadline:
                out_of_time = len([x for x in new_tracks if x.key not in processed])
                print(f"Time budget reached; {out_of_time} track(s) left for the next run.")
                break

            name = playlist_name(t.air_date)
            if name not in playlist_cache:
                pid = playlists.get(name)
                if not pid:
                    pid = sp.find_or_create_playlist(name, PLAYLIST_DESCRIPTION)
                    playlists[name] = pid
                playlist_cache[name] = pid
                existing_cache[pid] = sp.playlist_track_uris(pid)
                pending[pid] = []
            pid = playlist_cache[name]

            skey = song_key(t.artist, t.title)
            if skey in song_cache:
                uri = song_cache[skey]
            else:
                uri = sp.match_track(t.artist, t.title)
                song_cache[skey] = uri

            if uri is None:
                missed += 1
                processed[t.key] = None
                unmatched.append(t.to_dict())
                print(f"  ✗ no match: {t.artist} — {t.title}")
                continue

            if uri in existing_cache[pid] or any(uri == u for _, u in pending[pid]):
                matched_dupes += 1
                processed[t.key] = uri
                continue

            pending[pid].append((t.key, uri))
            print(f"  ✓ {name}: {t.artist} — {t.title}")
            if len(pending[pid]) >= ADD_BATCH_SIZE:
                flush(pid)
    except RateLimitStall as e:
        print(f"Stopping early: {e}. The next scheduled run will resume.")
    finally:
        # Push whatever is queued, then persist state no matter what.
        for pid in list(pending):
            try:
                flush(pid)
            except Exception as e:  # noqa: BLE001 — never lose state over a flush
                print(f"  ! final flush failed for {pid}: {e}", file=sys.stderr)
        save_json(STATE_PATH, state)
        save_json(UNMATCHED_PATH, unmatched)

    remaining = sum(1 for t in new_tracks if t.key not in processed)
    print(
        f"\nDone. Added {added}, already present {matched_dupes}, "
        f"unmatched {missed}, remaining for next run {remaining} "
        f"(see data/unmatched.json)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
