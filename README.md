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

A GitHub Action runs every 6 hours, pulls the league from the public Sleeper API, and commits
three files to the repo root, which is what GitHub Pages serves:

| File | What it is |
|---|---|
| `league.md` | **The brief.** Self-contained, ~34 KB, written to be read by a language model in one fetch |
| `league.json` | The same data, structured, for scripts and tools |
| `index.html` | A human landing page with the copy-paste prompt |

(A fourth file, `.nojekyll`, tells Pages to serve these as static files instead of running them
through Jekyll — without it, Jekyll renders `README.md` as the landing page and `league.md` 404s.)

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
- **The committed output stays small.** Four refreshes a day at ~34 KB is roughly 136 KB/day of
  history. That is affordable; publishing every free agent every 15 minutes, as an earlier draft
  did, would have been ~422 MB/day.

### Why commit instead of deploying a Pages artifact?

Pages can be served either from a branch or from a workflow artifact. The artifact route keeps
generated files out of git entirely, and is objectively tidier — but it requires
*Settings → Pages → Source* to be set to **GitHub Actions**. This repo is on the default
**Deploy from a branch**, so committing to `main` is what actually reaches the web.

If you ever switch that setting, swap the `Commit and push` step back to
`actions/upload-pages-artifact@v3` + `actions/deploy-pages@v4`, change `permissions` from
`contents: write` to `pages: write` / `id-token: write`, and build to `--out site` again.

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
