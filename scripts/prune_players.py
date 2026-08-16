#!/usr/bin/env python3
"""Build the baked player dictionary the live renderer joins against.

    python3 scripts/prune_players.py --out data/players.json

Sleeper's ``/players/nfl`` is ~16 MB and covers every player it has ever known,
which is far too large to fetch inside a request. It also changes slowly, so it
is pruned here once a day and committed; the request path only fetches the
volatile league endpoints (~18 KB) and joins against this file.

Two rules decide who survives, and the ``or`` between them is load-bearing:

1. Currently rosterable — a skill position, on an NFL team, with news inside
   ``FRESH_WINDOW_DAYS``. Sleeper never retires anyone (Tom Brady is still
   ``active: true, status: "Active"``), so ``active`` cannot be used for this
   and ``search_rank`` actively works against it: it is lifetime popularity, so
   retired stars sort above everyone.
2. Rostered in this league — a dynasty manager may hold a player who is a real
   life free agent. Tyreek Hill, Keenan Allen and Matt Prater are all ``team:
   None`` yet rostered here; rule 1 alone drops them and their names stop
   resolving anywhere in the brief.

Team defenses skip the freshness test entirely: their records carry no
``news_updated`` at all, so applying it deletes all 32.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sleeper import SleeperError, env, make_client, resolve_current_league

DEFAULT_LEAGUE_ID = "1359238964875132928"
DEFAULT_USER_ID = "1260412048202813440"

# Positions worth carrying. Anything else (offensive line, most IDP) never
# appears in a lineup slot this league uses.
SKILL_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K"})
RANKLESS_POSITIONS = frozenset({"DEF"})

# How recently Sleeper must have published news about a player for them to count
# as active. Chosen to span a full offseason plus a season: a starter always has
# news inside it, and someone who last made news two seasons ago does not.
FRESH_WINDOW_DAYS = 540

# Fields kept per player. These deliberately keep Sleeper's own names so
# model.compact_player and model._rank_of read this file unchanged.
KEEP_FIELDS = (
    "full_name",
    "first_name",
    "last_name",
    "position",
    "team",
    "age",
    "years_exp",
    "injury_status",
    "search_rank",
    "fantasy_positions",
)


def is_rosterable(raw: dict[str, Any], now: float) -> bool:
    """Whether this player could plausibly be picked up today."""
    position = raw.get("position")
    if position in RANKLESS_POSITIONS:
        return True
    if position not in SKILL_POSITIONS:
        return False
    if not raw.get("team"):
        return False
    news = raw.get("news_updated") or 0
    return (news / 1000.0) >= (now - FRESH_WINDOW_DAYS * 86400)


def rostered_ids(client: Any, league_id: str, user_id: str) -> set[str]:
    """Every player id held by a team in this league, across all roster slots."""
    league = resolve_current_league(client, league_id, user_id)
    rosters = client.get(f"/league/{league['league_id']}/rosters") or []
    held: set[str] = set()
    for roster in rosters:
        for slot in ("players", "starters", "taxi", "reserve"):
            for pid in roster.get(slot) or []:
                if str(pid) not in ("0", "None", ""):
                    held.add(str(pid))
    return held


def prune(
    players: dict[str, dict[str, Any]], held: set[str], now: float | None = None
) -> dict[str, dict[str, Any]]:
    now = time.time() if now is None else now
    out: dict[str, dict[str, Any]] = {}
    for pid, raw in players.items():
        pid = str(pid)
        rosterable = is_rosterable(raw, now)
        if not rosterable and pid not in held:
            continue
        record = {field: raw.get(field) for field in KEEP_FIELDS if raw.get(field) is not None}
        # Precomputed so the request path never re-derives the heuristic, and so
        # a player kept only because someone rosters them is not offered as a
        # free agent.
        record["rosterable"] = rosterable
        out[pid] = record
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data/players.json")
    parser.add_argument("--league-id", default=env("SLEEPER_LEAGUE_ID") or DEFAULT_LEAGUE_ID)
    parser.add_argument("--user-id", default=env("SLEEPER_USER_ID") or DEFAULT_USER_ID)
    parser.add_argument("--fixtures", help="build from recorded fixtures instead of the live API")
    parser.add_argument("--cache-dir", default=".cache")
    args = parser.parse_args()

    client = make_client(args.fixtures, Path(args.cache_dir))
    try:
        players = client.players()
        held = rostered_ids(client, args.league_id, args.user_id)
    except SleeperError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    pruned = prune(players, held)
    kept_for_roster = sum(1 for pid in held if pid in pruned and not pruned[pid]["rosterable"])

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(pruned, sort_keys=True, separators=(",", ":")) + "\n"
    out_path.write_text(payload, encoding="utf-8")

    missing = held - set(pruned)
    print(
        f"players: {len(players)} -> {len(pruned)} "
        f"({len(payload) / 1024:.0f} KB, {kept_for_roster} kept only because they are rostered)"
    )
    if missing:
        # Would mean a rostered id Sleeper no longer publishes at all; the brief
        # would show "Unknown player <id>" for them.
        print(f"WARNING: {len(missing)} rostered ids absent from the player map: {sorted(missing)[:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
