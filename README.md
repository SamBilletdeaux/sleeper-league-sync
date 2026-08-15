# sleeper-league-sync

Publishes a compact, always-reasonably-fresh brief of a Sleeper dynasty fantasy football league
that anyone can point an AI assistant at — no GitHub account, no API key, no setup.

**→ https://sambilletdeaux.github.io/sleeper-league-sync**

## For leaguemates

Paste this into ChatGPT, Claude, Gemini, or any assistant that can browse the web:

```
Read https://sambilletdeaux.github.io/sleeper-league-sync/league.md and use it
to answer my questions about my dynasty fantasy football league.
```

Then ask whatever you want — who has cap-adjacent depth at RB, which teams are rebuilding, who owns
the most 2027 firsts, who's worth a waiver claim.

## How it works

A GitHub Action runs every 6 hours, pulls the league from the public Sleeper API, and publishes
three files to GitHub Pages:

| File | What it is |
|---|---|
| `league.md` | **The brief.** Self-contained, ~40 KB, written to be read by a language model in one fetch |
| `league.json` | The same data, structured, for scripts and tools |
| `index.html` | A human landing page with the copy-paste prompt |

`league.md` contains league settings and scoring, standings, every roster with real player names,
future rookie pick ownership after trades, and the top available free agents by position.

**Freshness is hybrid.** The snapshot covers the slow-moving things. The brief also ends with a
*Live data* section listing the exact Sleeper API endpoints for anything time-sensitive — current
rosters, transactions, live scores — so an assistant can fetch genuinely current data mid-conversation.
Because every player in the brief is listed as `<sleeper_id> <name>`, the document doubles as the
decoder ring for those raw API responses, which otherwise come back as bare numeric ids.

Two deliberate constraints keep it usable:

- **The brief is size-capped.** The build warns above 120 KB and fails above 250 KB. Publishing every
  free agent would produce a 1.6 MB file that no chat model will ingest, so free agent lists are
  truncated by Sleeper's `search_rank`.
- **Nothing is committed to git.** The site deploys as a Pages artifact, so refreshing four times a
  day forever adds nothing to the repository history.

The build also follows dynasty season rollover automatically: Sleeper mints a new `league_id` every
season, so the script walks `previous_league_id` from the configured root to find the current one.

## Running it locally

Python 3.10+, no dependencies.

```bash
# Build from recorded fixtures — no network needed
python3 scripts/build.py --fixtures tests/fixtures --out /tmp/site

# Build from the live Sleeper API
python3 scripts/build.py --out site

# Tests (offline)
python3 -m unittest discover -s tests
```

## Layout

```
scripts/sleeper.py   API client: retries, disk-cached player map, season rollover
scripts/model.py     pure transforms — rosters, records, pick inventory, free agents
scripts/render.py    pure rendering — league.md, league.json, index.html
scripts/build.py     orchestration and the size guardrail
tests/               offline tests and fixtures
```

## Pointing it at a different league

Set `SLEEPER_LEAGUE_ID` and `SLEEPER_USER_ID` in `.github/workflows/publish.yml`, or pass
`--league-id` / `--user-id` on the command line. The user id just needs to belong to someone in the
league; it's used to follow the league across seasons.

## Notes

The Sleeper API is public and read-only, so there are no secrets in this repo. The one endpoint that
needs care is `/players/nfl` (~5 MB, and Sleeper asks for at most one call per day) — it's cached on
disk with a date-keyed Actions cache, so the first run each day fetches it and the rest reuse it.
