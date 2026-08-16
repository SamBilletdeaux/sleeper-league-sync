#!/usr/bin/env python3
"""Build the published site from the Sleeper API (or from fixtures, offline).

    python3 scripts/build.py --out site
    python3 scripts/build.py --fixtures tests/fixtures --out /tmp/site
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import model
import prune_players
import render
from sleeper import SleeperError, env, make_client, resolve_current_league

DEFAULT_LEAGUE_ID = "1359238964875132928"
DEFAULT_USER_ID = "1260412048202813440"
DEFAULT_SITE_URL = "https://sambilletdeaux.github.io/sleeper-league-sync"

# league.md has to fit in a chat model's context in a single fetch. These bounds
# keep it from quietly growing back into something nothing will read.
WARN_BYTES = 120 * 1024
FAIL_BYTES = 250 * 1024


def build_snapshot(
    client: Any,
    league_id: str,
    user_id: str,
    players: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble everything the brief renders.

    ``players`` lets the request path pass the baked dictionary that
    ``prune_players.py`` produces; left None, the full map is fetched, which is
    what the daily static build does.
    """
    league_raw = resolve_current_league(client, league_id, user_id)
    resolved_id = str(league_raw["league_id"])
    state = client.get("/state/nfl") or {}
    week = state.get("week") or 1

    paths = {
        "users": f"/league/{resolved_id}/users",
        "rosters": f"/league/{resolved_id}/rosters",
        "traded_picks": f"/league/{resolved_id}/traded_picks",
        "matchups": f"/league/{resolved_id}/matchups/{week}",
        "transactions": f"/league/{resolved_id}/transactions/{week}",
        "trending_add": "/players/nfl/trending/add?lookback_hours=24&limit=25",
        "trending_drop": "/players/nfl/trending/drop?lookback_hours=24&limit=25",
    }
    previous_id = league_raw.get("previous_league_id")
    if previous_id:
        paths["prior_league"] = f"/league/{previous_id}"
        paths["prior_rosters"] = f"/league/{previous_id}/rosters"
        paths["prior_users"] = f"/league/{previous_id}/users"
        paths["prior_bracket"] = f"/league/{previous_id}/winners_bracket"

    got = client.get_many(paths)
    if players is None:
        players = client.players()

    rosters = got.get("rosters") or []
    if not rosters:
        raise SleeperError(f"league {resolved_id} returned no rosters")

    teams = model.build_teams(got.get("users") or [], rosters, players, league_raw)
    summary = model.league_summary(league_raw, state)
    season = int(summary["season"]) if summary["season"].isdigit() else datetime.now(timezone.utc).year

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": "Sleeper API (public, read-only)",
        "root_league_id": league_id,
        "followed_season_rollover": resolved_id != league_id,
        "league": summary,
        "season_state": model.season_state(league_raw, state),
        "teams": teams,
        "standings": model.standings(teams),
        "picks": model.build_pick_inventory(league_raw, teams, got.get("traded_picks") or [], season),
        "matchups": model.build_matchups(got.get("matchups"), teams),
        "transactions": model.build_transactions(got.get("transactions"), players, teams),
        "prior_season": model.prior_season(
            got.get("prior_league"),
            got.get("prior_rosters"),
            got.get("prior_users"),
            got.get("prior_bracket"),
        ),
        "free_agents": model.build_free_agents(players, teams),
        "trending": {
            "adds": model.build_trending(got.get("trending_add"), players, teams),
            "drops": model.build_trending(got.get("trending_drop"), players, teams),
        },
    }


def viewer_for(snapshot: dict[str, Any], username: str) -> dict[str, Any] | None:
    """Resolve a Sleeper username (or team name) to the team it manages.

    Personalised routes exist so the assistant never has to open with "which
    team are you?" — a round trip that costs a turn and that the reader often
    answers ambiguously.
    """
    if not username:
        return None
    wanted = username.strip().lower()
    picks = snapshot.get("picks") or {}
    for team in snapshot["teams"]:
        candidates = {
            str(team["owner"]).lower(),
            str(team["team_name"]).lower(),
            str(team["roster_id"]),
        }
        if wanted in candidates:
            viewer = dict(team)
            viewer["pick_summary"] = model.pick_summary(picks.get(team["roster_id"]) or [])
            viewer["gaps"] = render.roster_gaps(team, snapshot["league"].get("starting_lineup"))
            return viewer
    return None


def write_site(snapshot: dict[str, Any], out_dir: Path, site_url: str) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = render.render_markdown(snapshot)
    files = {
        "league.md": markdown,
        # Served alongside the markdown because some assistants will only read
        # text/html; the static mirror has to work for them too.
        "league.html": render.render_html(snapshot, markdown),
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


def load_players(client: Any, args: argparse.Namespace) -> dict[str, Any] | None:
    """The pruned dictionary the renderer expects, however we can get it.

    Free agent availability keys off the ``rosterable`` flag that pruning adds,
    so handing the renderer a raw Sleeper player map would silently produce an
    empty free agent list rather than an error. Fixture runs are exempt: their
    player file is already in the pruned shape.
    """
    if args.fixtures:
        return None
    path = Path(args.players)
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            players = json.load(handle)
        print(f"players: {len(players)} from {path}")
        return players
    print(f"{path} missing; pruning the full player map in-process")
    held = prune_players.rostered_ids(client, args.league_id, args.user_id)
    return prune_players.prune(client.players(), held)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="site", help="output directory (default: site)")
    parser.add_argument("--fixtures", help="build from recorded fixtures instead of the live API")
    parser.add_argument("--league-id", default=env("SLEEPER_LEAGUE_ID") or DEFAULT_LEAGUE_ID)
    parser.add_argument("--user-id", default=env("SLEEPER_USER_ID") or DEFAULT_USER_ID)
    parser.add_argument("--site-url", default=env("SITE_URL") or DEFAULT_SITE_URL)
    parser.add_argument("--cache-dir", default=".cache")
    parser.add_argument(
        "--players",
        default="data/players.json",
        help="pruned player dictionary (built by scripts/prune_players.py)",
    )
    args = parser.parse_args()

    client = make_client(args.fixtures, Path(args.cache_dir))
    try:
        snapshot = build_snapshot(
            client, args.league_id, args.user_id, players=load_players(client, args)
        )
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
