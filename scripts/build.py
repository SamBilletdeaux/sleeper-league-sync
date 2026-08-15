#!/usr/bin/env python3
"""Build the published site from the Sleeper API (or from fixtures, offline).

    python3 scripts/build.py --out site
    python3 scripts/build.py --fixtures tests/fixtures --out /tmp/site
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import model
import render
from sleeper import SleeperError, env, make_client, resolve_current_league

DEFAULT_LEAGUE_ID = "1359238964875132928"
DEFAULT_USER_ID = "1260412048202813440"
DEFAULT_SITE_URL = "https://sambilletdeaux.github.io/sleeper-league-sync"

# league.md has to fit in a chat model's context in a single fetch. These bounds
# keep it from quietly growing back into something nothing will read.
WARN_BYTES = 120 * 1024
FAIL_BYTES = 250 * 1024


def build_snapshot(client: Any, league_id: str, user_id: str) -> dict[str, Any]:
    league_raw = resolve_current_league(client, league_id, user_id)
    resolved_id = str(league_raw["league_id"])
    state = client.get("/state/nfl") or {}

    users = client.get(f"/league/{resolved_id}/users") or []
    rosters = client.get(f"/league/{resolved_id}/rosters") or []
    traded_picks = client.get(f"/league/{resolved_id}/traded_picks") or []
    trending_add = client.get("/players/nfl/trending/add", lookback_hours=24, limit=25) or []
    trending_drop = client.get("/players/nfl/trending/drop", lookback_hours=24, limit=25) or []
    players = client.players()

    if not rosters:
        raise SleeperError(f"league {resolved_id} returned no rosters")

    teams = model.build_teams(users, rosters, players, league_raw)
    summary = model.league_summary(league_raw, state)
    season = int(summary["season"]) if summary["season"].isdigit() else datetime.now(timezone.utc).year

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": "Sleeper API (public, read-only)",
        "root_league_id": league_id,
        "followed_season_rollover": resolved_id != league_id,
        "league": summary,
        "teams": teams,
        "standings": model.standings(teams),
        "picks": model.build_pick_inventory(league_raw, teams, traded_picks, season),
        "free_agents": model.build_free_agents(players, teams),
        "trending": {
            "adds": model.build_trending(trending_add, players, teams),
            "drops": model.build_trending(trending_drop, players, teams),
        },
    }


def write_site(snapshot: dict[str, Any], out_dir: Path, site_url: str) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = render.render_markdown(snapshot)
    files = {
        "league.md": markdown,
        "league.json": render.render_json(snapshot),
        "index.html": render.render_index(snapshot, site_url.rstrip("/")),
        # Stops Pages from running the output through Jekyll.
        ".nojekyll": "",
    }
    for name, content in files.items():
        (out_dir / name).write_text(content, encoding="utf-8")

    size = len(markdown.encode("utf-8"))
    print(f"\nWrote {out_dir}/")
    for name in files:
        print(f"  {name:14s} {(out_dir / name).stat().st_size / 1024:8.1f} KB")

    if size > FAIL_BYTES:
        print(
            f"\nERROR: league.md is {size / 1024:.0f} KB, over the {FAIL_BYTES / 1024:.0f} KB "
            "limit. Tighten model.FREE_AGENT_LIMITS.",
            file=sys.stderr,
        )
        return 1
    if size > WARN_BYTES:
        print(f"\nWARNING: league.md is {size / 1024:.0f} KB, above the {WARN_BYTES / 1024:.0f} KB target.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="site", help="output directory (default: site)")
    parser.add_argument("--fixtures", help="build from recorded fixtures instead of the live API")
    parser.add_argument("--league-id", default=env("SLEEPER_LEAGUE_ID") or DEFAULT_LEAGUE_ID)
    parser.add_argument("--user-id", default=env("SLEEPER_USER_ID") or DEFAULT_USER_ID)
    parser.add_argument("--site-url", default=env("SITE_URL") or DEFAULT_SITE_URL)
    parser.add_argument("--cache-dir", default=".cache")
    args = parser.parse_args()

    client = make_client(args.fixtures, Path(args.cache_dir))
    try:
        snapshot = build_snapshot(client, args.league_id, args.user_id)
    except SleeperError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    league = snapshot["league"]
    print(
        f"{league['name']} · season {league['season']} · league {league['league_id']} · "
        f"{len(snapshot['teams'])} teams"
    )
    if snapshot["followed_season_rollover"]:
        print(f"(followed season rollover from root league {snapshot['root_league_id']})")
    return write_site(snapshot, Path(args.out), args.site_url)


if __name__ == "__main__":
    raise SystemExit(main())
