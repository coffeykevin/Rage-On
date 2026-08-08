"""
Scraper for the ABC Rage playlist pages.

The hub page (https://www.abc.net.au/rage/playlist) links to per-episode
playlist pages, each tied to an air date. This module fetches the hub,
discovers episode pages, and parses each into (artist, title, air_date)
entries.

The ABC site has changed structure over the years (see RAGEagain's notes),
so parsing is deliberately layered:

  1. Try embedded structured data (__NEXT_DATA__ / JSON-LD) if present.
  2. Fall back to HTML heuristics.

All selectors and heuristics live in this one file so that when the ABC
markup changes, this is the only file to fix. Run `python -m src.scrape`
locally for a dry run that prints what it finds without touching Spotify.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HUB_URL = "https://www.abc.net.au/rage/playlist"
USER_AGENT = (
    "rage-spotify-sync (+https://github.com/YOUR_USERNAME/rage-spotify-sync; "
    "personal, non-commercial playlist tool)"
)
TIMEOUT = 30


@dataclass(frozen=True)
class Track:
    artist: str
    title: str
    air_date: str  # ISO date (YYYY-MM-DD) of the episode

    @property
    def key(self) -> str:
        """Stable dedupe key. Same song on a later episode is a new key,
        but adds are also deduped against the playlist itself."""
        norm = lambda s: re.sub(r"\s+", " ", s).strip().lower()
        return f"{norm(self.artist)}|{norm(self.title)}|{self.air_date}"

    def to_dict(self) -> dict:
        return asdict(self)


def _get(url: str) -> requests.Response:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# Episode discovery
# ---------------------------------------------------------------------------

def discover_episode_urls(hub_html: str, base_url: str = HUB_URL) -> list[str]:
    """Find links to per-episode playlist pages on the hub page."""
    soup = BeautifulSoup(hub_html, "html.parser")
    urls: list[str] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Historic pattern: /rage/archive/s1234567.htm
        # Modern ABC article pattern: /rage/...playlist.../<numeric-id>
        if re.search(r"/rage/.*(archive/s\d+\.htm|playlist)", href, re.I) or (
            "/rage/" in href and re.search(r"/\d{6,}$", href)
        ):
            full = urljoin(base_url, href)
            if full != base_url and full not in seen:
                seen.add(full)
                urls.append(full)
    return urls


# ---------------------------------------------------------------------------
# Episode parsing
# ---------------------------------------------------------------------------

DATE_PATTERNS = [
    "%Y-%m-%d",
    "%d %B %Y",
    "%A %d %B %Y",
    "%d/%m/%Y",
]


def _parse_date(text: str) -> str | None:
    text = text.strip()
    # ISO timestamps in metadata
    m = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if m:
        return m.group(0)
    for pat in DATE_PATTERNS:
        try:
            return datetime.strptime(text, pat).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})", text, re.I)
    if m:
        try:
            return datetime.strptime(" ".join(m.groups()), "%d %B %Y").date().isoformat()
        except ValueError:
            return None
    return None


def _episode_date(soup: BeautifulSoup, url: str) -> str | None:
    """Best-effort extraction of the episode's air date."""
    # 1. Metadata tags
    for sel, attr in [
        ('meta[property="article:published_time"]', "content"),
        ('meta[name="DC.date"]', "content"),
        ("time[datetime]", "datetime"),
    ]:
        el = soup.select_one(sel)
        if el and el.get(attr):
            d = _parse_date(el[attr])
            if d:
                return d
    # 2. JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in data if isinstance(data, list) else [data]:
            if isinstance(obj, dict) and obj.get("datePublished"):
                d = _parse_date(str(obj["datePublished"]))
                if d:
                    return d
    # 3. Date in the page heading or title
    for el in soup.find_all(["h1", "h2", "title"]):
        d = _parse_date(el.get_text(" ", strip=True))
        if d:
            return d
    return None


# Track lines on Rage playlists are conventionally "ARTIST - Title" or
# "Artist – Title (Label)". Uppercase artist names are common.
TRACK_LINE = re.compile(r"^(?P<artist>[^–—\-]{1,80}?)\s+[–—\-]\s+(?P<title>.{1,120})$")

NOISE = re.compile(
    r"^(rage|playlist|guest programmer|top ?50|new release|"
    r"\d{1,2}[:.]\d{2}\s*(am|pm)?|(mon|tue|wed|thu|fri|sat|sun))",
    re.I,
)


def parse_episode(html: str, url: str) -> list[Track]:
    soup = BeautifulSoup(html, "html.parser")
    air_date = _episode_date(soup, url)
    if not air_date:
        print(f"  ! no air date found for {url}, skipping", file=sys.stderr)
        return []

    tracks: list[Track] = []
    seen: set[str] = set()

    # Strategy A: list items / table rows
    candidates = [el.get_text(" ", strip=True) for el in soup.select("li, td, p")]
    # Strategy B: raw text lines (older pages are near-plain text)
    if len(candidates) < 5:
        candidates = [ln.strip() for ln in soup.get_text("\n").splitlines()]

    for line in candidates:
        line = re.sub(r"\s*\((19|20)\d{2}\)\s*$", "", line)  # trailing (year)
        line = re.sub(r"\s*\[[^\]]+\]\s*$", "", line)        # trailing [label]
        if not line or len(line) > 200 or NOISE.match(line):
            continue
        m = TRACK_LINE.match(line)
        if not m:
            continue
        artist = m.group("artist").strip()
        title = re.sub(r"\s*\([^)]*\)\s*$", "", m.group("title")).strip()
        if not artist or not title:
            continue
        t = Track(artist=artist, title=title, air_date=air_date)
        if t.key not in seen:
            seen.add(t.key)
            tracks.append(t)
    return tracks


def scrape_all() -> list[Track]:
    print(f"Fetching hub: {HUB_URL}")
    hub = _get(HUB_URL)
    episode_urls = discover_episode_urls(hub.text)
    print(f"Found {len(episode_urls)} episode link(s)")

    all_tracks: list[Track] = []
    for url in episode_urls:
        print(f"  Fetching {url}")
        try:
            tracks = parse_episode(_get(url).text, url)
        except requests.RequestException as e:
            print(f"  ! fetch failed: {e}", file=sys.stderr)
            continue
        print(f"    {len(tracks)} track(s)")
        all_tracks.extend(tracks)
    return all_tracks


if __name__ == "__main__":
    # Dry run: scrape and print, no Spotify calls.
    found = scrape_all()
    for t in found:
        print(f"{t.air_date}  {t.artist} — {t.title}")
    print(f"\nTotal: {len(found)} tracks")
