# sleeper-league-sync

Publishes a live brief of a Sleeper dynasty fantasy football league that anyone can point an AI
assistant at — no GitHub account, no API key, no setup.

**→ https://kingkings.vercel.app**

## For leaguemates

Paste this into ChatGPT, Claude, Gemini, Perplexity, or any assistant that can browse the web:

```
Read https://kingkings.vercel.app and use it to answer my questions about my
dynasty fantasy football league.
```

Then ask whatever you want — who has the deepest running back room, which teams are rebuilding,
who owns the most 2027 firsts, who's worth a waiver claim.

Want the assistant to know which team is yours? Use your own link instead, and it will answer
from your perspective without asking:

```
https://kingkings.vercel.app/me/<your-sleeper-username>
```

If your assistant says it can't read the link, open the page yourself and press **Copy entire
brief**, then paste that into the chat.

## How it works

The page is rendered from the Sleeper API on every request, so it is current when it is read.

```
Daily GitHub Action
  fetch /players/nfl (~16 MB) → prune to ~1,000 players → commit data/players.json
  push triggers a Vercel redeploy, and publishes a static mirror to GitHub Pages

Vercel Python function (per request, 60s edge cache)
  fetch the volatile league endpoints in parallel   ~18 KB, ~200ms
  join against the committed player dictionary
  render HTML / Markdown / JSON
```

Splitting on volatility is what makes this cheap. The player map is 16 MB and changes slowly;
everything that actually moves — rosters, transactions, matchups, standings — totals about 18 KB
and is fetched live.

| Route | What you get |
|---|---|
| `/` | The brief, as HTML |
| `/me/<username>` | The same brief, addressed to one manager |
| `/?format=md` | The markdown source |
| `/league.json` | The same data, structured |
| `/health` | Liveness probe |

### Why not a static snapshot?

The first version of this published a snapshot every six hours and ended it with a *Live data*
section listing Sleeper endpoints, expecting the assistant to fetch them for anything current.
That delegated freshness to the least reliable thing in the loop. It also served `league.md` as
`text/markdown`, which Gemini reports as unreadable, and the landing page it linked from contained
no league data at all.

Rendering per request means an assistant only has to do the one thing every assistant can do:
read a page.

## What the brief contains, and why

It is written to be read by a language model, which shapes the content:

- **Aggregates are precomputed.** Positional counts, roster age and pick capital are stated per
  team, because a model counting twenty roster lines gets it wrong and a model reading a total
  does not.
- **The NFL calendar is spelled out.** During the preseason every record is 0-0; unexplained, that
  reads as teams performing badly rather than as games not yet played.
- **Nothing stays a bare id.** Sleeper identifies teams by `roster_id` and players by numeric id.
  Both are resolved everywhere, including inside transactions.
- **The boundaries are stated.** A *what this does not cover* section names the gaps — no dynasty
  trade values, no projections — so the model says "not in the document" instead of inventing.
- **Free agent availability is real.** Sleeper never retires anyone (Tom Brady is still
  `active: true`), and `search_rank` is lifetime popularity, so filtering on those two published a
  list that was 68% retired players led by Brady, Brees and Roethlisberger. Availability now comes
  from having an NFL team plus recent news, computed in `scripts/prune_players.py`.

## Running it locally

Python 3.10+, no dependencies.

```bash
# Build from recorded fixtures — no network needed
python3 scripts/build.py --fixtures tests/fixtures --out /tmp/site

# Refresh the pruned player dictionary from the live API
python3 scripts/prune_players.py --out data/players.json

# Build the static mirror from the live API
python3 scripts/build.py --out .

# Serve the live renderer locally
vercel dev

# Tests (offline)
python3 -m unittest discover -s tests
```

## Layout

```
api/index.py             Vercel handler: routing, format negotiation, cache headers
scripts/sleeper.py       API client: retries, parallel fetch, season rollover
scripts/prune_players.py 16 MB player map → the ~1,000 players this league can reference
scripts/model.py         pure transforms — rosters, records, picks, transactions, free agents
scripts/render.py        pure rendering — markdown, and the HTML generated from it
scripts/build.py         snapshot assembly, viewer resolution, static mirror
data/players.json        the pruned dictionary, refreshed daily
tests/                   offline tests and fixtures
```

`model.py` and `render.py` do no I/O, so everything they do is testable against fixtures.

## Pointing it at a different league

Set `SLEEPER_LEAGUE_ID` and `SLEEPER_USER_ID` as Vercel environment variables and in
`.github/workflows/publish.yml`, or pass `--league-id` / `--user-id` on the command line. The user
id just needs to belong to someone in the league; it is used to follow the league across seasons,
since Sleeper mints a new `league_id` every year and links it to the prior one through
`previous_league_id`.

## The static mirror

GitHub Pages still serves a daily snapshot at
https://sambilletdeaux.github.io/sleeper-league-sync as a fallback for when Vercel or Sleeper is
unavailable: `league.html`, `league.md` and `league.json`.

Pages is set to **Deploy from a branch** (`main`, `/`). It was previously set to **GitHub
Actions**, which meant commits to `main` published nothing and every file 404'd while the workflow
still reported success — worth knowing if the mirror ever goes stale again. `.nojekyll` keeps Pages
from running the output through Jekyll, which would otherwise render `README.md` as the landing
page and 404 on `league.md`.

## Notes

The Sleeper API is public and read-only, so there are no secrets here. The one endpoint that needs
care is `/players/nfl` (~16 MB, and Sleeper asks for at most one call per day) — it is fetched only
by the daily job, never on the request path.

The brief exposes leaguemates' Sleeper display names and team names on a public URL. That is
already semi-public through Sleeper itself, but it is a real change in reach.
