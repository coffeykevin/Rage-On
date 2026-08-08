# rage → Spotify sync

Automatically mirrors the [ABC rage playlist](https://www.abc.net.au/rage/playlist) into public Spotify playlists, bucketed by half-year:

- **ABC Rage H1 2026** — everything aired January–June 2026
- **ABC Rage H2 2026** — everything aired July–December 2026

Everything runs on GitHub. A scheduled GitHub Actions workflow scrapes the rage playlist pages, matches each track on Spotify, adds it to the right playlist, and commits its state back to this repo. No servers, no database, nothing to host.

> Unofficial fan project. Not affiliated with or endorsed by the Australian Broadcasting Corporation or Spotify. Only track titles and artist names are used — no ABC media is copied.

## How it works

```
GitHub Actions (daily cron)
  └─ src/scrape.py    fetch hub page → discover episode pages → parse
                      each into (artist, title, air_date)
  └─ src/main.py      dedupe against data/state.json
  └─ src/spotify.py   search Spotify → fuzzy-match → add to
                      "ABC Rage H1/H2 YYYY" (created if missing, public)
  └─ workflow         commits data/state.json + data/unmatched.json
                      back to the repo — the repo is the database
```

Tracks with no confident Spotify match are logged to `data/unmatched.json` for manual review rather than guessing.

## Setup

1. **Fork or clone this repo** to your GitHub account.
2. **Create a Spotify app** at [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard). Add `http://127.0.0.1:8765/callback` as a redirect URI.
3. **Get a refresh token** (one-time, locally):
   ```bash
   pip install -r requirements.txt
   export SPOTIFY_CLIENT_ID=... SPOTIFY_CLIENT_SECRET=...
   python -m src.get_token
   ```
4. **Add three repository secrets** (Settings → Secrets and variables → Actions): `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REFRESH_TOKEN`.
5. **Enable the workflow** (Actions tab) or trigger it manually with *Run workflow*. It then runs daily.

### Test the scraper locally first

The ABC site's markup has changed over the years, so before the first real run:

```bash
python -m src.scrape
```

This dry-runs the scraper and prints what it finds without touching Spotify. If it comes back empty, the selectors in `src/scrape.py` need adjusting — all parsing logic is deliberately kept in that one file.

## Design notes

- **State in the repo.** `data/state.json` records every processed track and its matched Spotify URI (or `null` for misses), so re-runs never duplicate work. Adds are additionally checked against the playlist's live contents.
- **Fuzzy matching.** Spotify search results are scored against the scraped artist/title (threshold 0.75). Below the threshold, the track is logged as unmatched rather than added wrongly.
- **Polite scraping.** One fetch per episode page, a descriptive User-Agent, and a daily schedule. Be a good citizen of the ABC's servers.
- **Rate limits.** Spotify 429s are honoured with `Retry-After`.

## Attribution & prior art

This project stands on the shoulders of people who explored this space first:

- **[RAGEagain](https://www.pjgalbraith.com/rageagain/)** by Patrick Galbraith — a searchable archive of rage playlists back to 1999, scraped via scheduled GitHub workers into flat JSON. The all-on-GitHub architecture here is directly inspired by it.
- **[Spotifier](https://github.com/brtrx/Spotifier)** by brtrx — an earlier take on building Spotify playlists from the rage archive.
- **rage** itself — broadcast on ABC TV since 1987. Watch it. Support it.

## License

Released under [Creative Commons Attribution 4.0](https://creativecommons.org/licenses/by/4.0/) (CC BY 4.0) — use it, adapt it, share it, just credit this repo.

*Note: Creative Commons licenses aren't purpose-built for software (they lack patent grants and source-code provisions). If you fork this and want a code-native license, MIT is the closest equivalent in spirit.*
