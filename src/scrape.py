"""
Scraper for the ABC Rage playlist pages.

Discovery (verified against the live site, Aug 2026):
  1. The hub page (https://www.abc.net.au/rage/playlist) is a Next.js app.
     Its __NEXT_DATA__ JSON contains a MetaCollection with one collection
     per tab ("All", "ABC TV", "ABC Entertains"); each has a numeric
     collection id.
  2. https://www.abc.net.au/rage/all_playlists/<collection-id> embeds links
     to every episode page: /rage/playlist/<slug>/<article-id>, where the
     slug carries the air date, e.g. saturday-night-8-august-2026-on-abc-tv.
  3. Each episode page's __NEXT_DATA__ has the tracklist as rich text under
     documentProps.text.descriptor. Lines look like:
         PLAYLIST 11:30pm EDDY CURRENT SUPPRESSION RING   Which Way To Go  (Shock)
         THE B-52S   Private Idaho  (Warner)
     i.e. optional "PLAYLIST"/time prefixes, then ARTIST and Title separated
     by a run of 2+ spaces, with an optional trailing (Label).

HTML-heuristic fallbacks are kept for when the ABC changes structure again.
All selectors and heuristics live in this one file so that when the ABC
markup changes, this is the only file to fix. Run `python -m src.scrape`
locally for a dry run that prints what it finds without touching Spotify.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.abc.net.au"
HUB_URL = f"{BASE}/rage/playlist"
ALL_PLAYLISTS_URL = f"{BASE}/rage/all_playlists/{{collection_id}}"
USER_AGENT = (
    "rage-spotify-sync (+https://github.com/coffeykevin/Rage-On; "
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


def _next_data(html: str) -> dict | None:
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.S,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Episode discovery
# ---------------------------------------------------------------------------

EPISODE_HREF = re.compile(r"/rage/playlist/[a-z0-9-]+/\d{6,}")


def _collection_ids(hub_data: dict) -> list[str]:
    """Collection ids from the hub's MetaCollection tabs ("All" first)."""
    ids: list[str] = []
    try:
        components = hub_data["props"]["pageProps"]["data"]["componentsContent"]
    except (KeyError, TypeError):
        return ids
    for comp in components:
        for item in comp.get("componentProps", {}).get("items", []):
            cid = str(item.get("id", ""))
            if cid.isdigit() and cid not in ids:
                ids.append(cid)
    return ids


def discover_episode_urls(hub_html: str, base_url: str = HUB_URL) -> list[str]:
    """Find links to per-episode playlist pages."""
    seen: set[str] = set()
    urls: list[str] = []

    def add(href: str) -> None:
        full = urljoin(base_url, href)
        if full not in seen:
            seen.add(full)
            urls.append(full)

    # Strategy A: hub __NEXT_DATA__ -> collection ids -> all_playlists pages,
    # whose embedded JSON links every episode.
    data = _next_data(hub_html)
    if data:
        for cid in _collection_ids(data):
            listing_url = ALL_PLAYLISTS_URL.format(collection_id=cid)
            try:
                listing = _get(listing_url).text
            except requests.RequestException as e:
                print(f"  ! fetch failed: {listing_url}: {e}", file=sys.stderr)
                continue
            for href in EPISODE_HREF.findall(listing):
                add(href)

    # Strategy B: any episode-shaped links directly in the hub page.
    for href in EPISODE_HREF.findall(hub_html):
        add(href)

    # Strategy C (legacy): anchor tags matching older patterns.
    if not urls:
        soup = BeautifulSoup(hub_html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if re.search(r"/rage/.*(archive/s\d+\.htm|playlist/)", href, re.I):
                add(href)

    return urls


# ---------------------------------------------------------------------------
# Air date
# ---------------------------------------------------------------------------

SLUG_DATE = re.compile(
    r"(\d{1,2})-(january|february|march|april|may|june|july|august|"
    r"september|october|november|december)-(\d{4})",
    re.I,
)


def _date_from_url(url: str) -> str | None:
    """Episode slugs embed the air date: .../saturday-night-8-august-2026-on-abc-tv/..."""
    m = SLUG_DATE.search(url)
    if not m:
        return None
    try:
        return (
            datetime.strptime(" ".join(m.groups()), "%d %B %Y").date().isoformat()
        )
    except ValueError:
        return None


def _parse_date(text: str) -> str | None:
    text = text.strip()
    m = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if m:
        return m.group(0)
    m = re.search(
        r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+(\d{4})",
        text,
        re.I,
    )
    if m:
        try:
            return datetime.strptime(" ".join(m.groups()), "%d %B %Y").date().isoformat()
        except ValueError:
            return None
    return None


def _episode_date(html: str, url: str) -> str | None:
    """Best-effort extraction of the episode's air date."""
    d = _date_from_url(url)
    if d:
        return d
    soup = BeautifulSoup(html, "html.parser")
    for el in soup.find_all(["h1", "title"]):
        d = _parse_date(el.get_text(" ", strip=True))
        if d:
            return d
    for sel, attr in [
        ('meta[property="article:published_time"]', "content"),
        ("time[datetime]", "datetime"),
    ]:
        el = soup.select_one(sel)
        if el and el.get(attr):
            d = _parse_date(el[attr])
            if d:
                return d
    return None


# ---------------------------------------------------------------------------
# Episode parsing
# ---------------------------------------------------------------------------

# Prefix noise on a track line: "PLAYLIST", time markers like "11:30pm".
LINE_PREFIX = re.compile(r"^(?:playlist\b[\s:]*|\d{1,2}[:.]\d{2}\s*(?:am|pm)\b[\s:]*)+", re.I)
# Trailing record label: "  (Shock Records)".
TRAILING_LABEL = re.compile(r"\s*\([^()]*\)\s*$")
# ARTIST and Title separated by a run of 2+ spaces.
TWO_SPACE_SPLIT = re.compile(r"\s{2,}")
# Artist annotations like "BLACK DIAMONDS, THE - Live on Be Our Guest, 1966".
ARTIST_ANNOTATION = re.compile(r"\s+[-–—]\s+(live|recorded|from)\b.*$", re.I)
TRAILING_THE = re.compile(r"^(?P<name>.+),\s*(?P<article>the)$", re.I)


def _richtext_lines(descriptor: dict) -> list[str]:
    """Flatten the rich-text tree into text lines (block/br boundaries)."""
    lines: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        # Keep runs of spaces intact — 2+ spaces separate ARTIST from Title.
        text = re.sub(r"[\n\r\t]+", " ", " ".join(buf)).strip()
        buf.clear()
        if text:
            lines.append(text)

    def walk(node: dict) -> None:
        if node.get("type") == "text":
            content = node.get("content")
            if content:
                buf.append(content.replace("\xa0", " "))
            return
        key = str(node.get("key", "")).lower()
        if key == "br":
            flush()
        for child in node.get("children") or []:
            walk(child)
        if key in ("p", "h2", "h3", "li", "div"):
            flush()

    walk(descriptor)
    flush()
    return lines


def _clean_artist(artist: str) -> str:
    artist = ARTIST_ANNOTATION.sub("", artist).strip()
    m = TRAILING_THE.match(artist)  # "BLACK DIAMONDS, THE" -> "THE BLACK DIAMONDS"
    if m:
        artist = f"{m.group('article')} {m.group('name')}".strip()
    return artist


def _track_from_line(line: str, air_date: str) -> Track | None:
    line = LINE_PREFIX.sub("", line.replace("\xa0", " ")).strip()
    if not line or len(line) > 200:
        return None
    line = TRAILING_LABEL.sub("", line)
    parts = TWO_SPACE_SPLIT.split(line, maxsplit=1)
    if len(parts) == 2:
        artist, title = parts
    else:
        # Fallback: "ARTIST - Title" with a dash separator.
        m = re.match(r"^(?P<artist>[^–—\-]{1,80}?)\s+[–—\-]\s+(?P<title>.{1,120})$", line)
        if not m:
            return None
        artist, title = m.group("artist"), m.group("title")
    artist = _clean_artist(artist)
    title = TRAILING_LABEL.sub("", title).strip()
    if not artist or not title:
        return None
    return Track(artist=artist, title=title, air_date=air_date)


def parse_episode(html: str, url: str) -> list[Track]:
    air_date = _episode_date(html, url)
    if not air_date:
        print(f"  ! no air date found for {url}, skipping", file=sys.stderr)
        return []

    lines: list[str] = []

    # Strategy A: rich-text tracklist inside __NEXT_DATA__.
    data = _next_data(html)
    if data:
        try:
            descriptor = data["props"]["pageProps"]["data"]["documentProps"]["text"]["descriptor"]
        except (KeyError, TypeError):
            descriptor = None
        if descriptor:
            lines = _richtext_lines(descriptor)

    # Strategy B: visible text of the rendered page.
    if not lines:
        soup = BeautifulSoup(html, "html.parser")
        main = soup.select_one("main") or soup
        lines = [ln.strip() for ln in main.get_text("\n").splitlines() if ln.strip()]

    tracks: list[Track] = []
    seen: set[str] = set()
    for line in lines:
        t = _track_from_line(line, air_date)
        if t and t.key not in seen:
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
