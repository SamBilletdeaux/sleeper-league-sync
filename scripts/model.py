"""Pure transforms from raw Sleeper payloads into the snapshot the site renders.

Nothing here does I/O, so it is all directly unit-testable against fixtures.
"""

from __future__ import annotations

from typing import Any, Iterable

# Positions worth listing as free agents, and how deep to go in each. The full
# player map has ~11k entries; publishing all of them produced a 1.6 MB file that
# no chat model will read. Sleeper's `search_rank` is a decent relevance proxy
# (lower is better), so take the top slice per position.
FREE_AGENT_LIMITS: dict[str, int] = {"QB": 60, "RB": 100, "WR": 100, "TE": 60, "K": 25, "DEF": 32}
UNRANKED = 10**9

# Sleeper gives team defenses no search_rank, so the relevance filter used for
# skill players would discard every one of them. There are only 32 and streaming
# them is routine waiver work, so list them all and sort by name instead.
RANKLESS_POSITIONS = frozenset({"DEF"})


def compact_player(player_id: Any, players: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Reduce a Sleeper player record to the fields that matter for league questions."""
    pid = str(player_id)
    raw = players.get(pid) or {}
    name = raw.get("full_name") or " ".join(
        part for part in (raw.get("first_name"), raw.get("last_name")) if part
    )
    return {
        "id": pid,
        "name": name or f"Unknown player {pid}",
        "pos": raw.get("position"),
        "team": raw.get("team"),
        "age": raw.get("age"),
        "exp": raw.get("years_exp"),
        "injury": raw.get("injury_status"),
        "rank": _rank_of(raw),
    }


def _rank_of(raw: dict[str, Any]) -> int:
    rank = raw.get("search_rank")
    return rank if isinstance(rank, int) and not isinstance(rank, bool) else UNRANKED


def compact_many(ids: Iterable[Any] | None, players: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = [compact_player(pid, players) for pid in (ids or []) if str(pid) != "0"]
    out.sort(key=lambda p: (p["rank"], p["name"]))
    return out


def score_record(settings: dict[str, Any]) -> dict[str, Any]:
    """Sleeper splits fantasy points into whole and decimal parts."""

    def combined(base: str) -> float:
        whole = settings.get(base) or 0
        decimal = settings.get(f"{base}_decimal") or 0
        return round(float(whole) + float(decimal) / 100.0, 2)

    return {
        "wins": settings.get("wins", 0) or 0,
        "losses": settings.get("losses", 0) or 0,
        "ties": settings.get("ties", 0) or 0,
        "points_for": combined("fpts"),
        "points_against": combined("fpts_against"),
    }


def build_teams(
    users: list[dict[str, Any]],
    rosters: list[dict[str, Any]],
    players: dict[str, dict[str, Any]],
    league: dict[str, Any],
) -> list[dict[str, Any]]:
    user_by_id = {str(u.get("user_id")): u for u in (users or [])}
    total_faab = (league.get("settings") or {}).get("waiver_budget")
    teams: list[dict[str, Any]] = []

    for roster in sorted(rosters or [], key=lambda r: int(r.get("roster_id") or 0)):
        roster_id = int(roster.get("roster_id") or 0)
        user = user_by_id.get(str(roster.get("owner_id"))) or {}
        metadata = user.get("metadata") or {}
        settings = roster.get("settings") or {}

        all_ids = _ids(roster.get("players"))
        starters = _ids(roster.get("starters"))
        taxi = _ids(roster.get("taxi"))
        reserve = _ids(roster.get("reserve"))
        assigned = set(starters) | set(taxi) | set(reserve)
        bench = [pid for pid in all_ids if pid not in assigned]

        faab_used = settings.get("waiver_budget_used") or 0
        teams.append(
            {
                "roster_id": roster_id,
                "owner": user.get("display_name") or "unknown",
                "team_name": metadata.get("team_name")
                or user.get("display_name")
                or f"Roster {roster_id}",
                "division": settings.get("division"),
                "record": score_record(settings),
                "waiver_position": settings.get("waiver_position"),
                "faab_used": faab_used,
                "faab_left": (total_faab - faab_used) if isinstance(total_faab, int) else None,
                "starters": compact_many(starters, players),
                "bench": compact_many(bench, players),
                "taxi": compact_many(taxi, players),
                "reserve": compact_many(reserve, players),
                "player_ids": all_ids,
            }
        )
    for team in teams:
        team["profile"] = roster_profile(team)
    return teams


def _ids(values: Any) -> list[str]:
    return [str(v) for v in (values or []) if str(v) not in ("0", "None", "")]


def standings(teams: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        teams,
        key=lambda t: (-t["record"]["wins"], t["record"]["losses"], -t["record"]["points_for"]),
    )


def build_pick_inventory(
    league: dict[str, Any],
    teams: list[dict[str, Any]],
    traded_picks: list[dict[str, Any]],
    current_season: int,
) -> dict[int, list[dict[str, Any]]]:
    """Work out who owns every future rookie pick, keyed by owning roster_id.

    Every team starts owning its own pick in each round of each season; traded
    picks then reassign ownership. Grouping by current owner is what makes this
    useful in a trade conversation.

    Once the current season's rookie draft has happened those picks are spent, so
    the window starts at the next season unless the league is still pre-draft.
    """
    rounds = int((league.get("settings") or {}).get("draft_rounds") or 4)
    roster_ids = sorted(t["roster_id"] for t in teams)
    if not roster_ids:
        return {}

    drafted_yet = league.get("status") not in ("pre_draft", "drafting")
    first_season = current_season + 1 if drafted_yet else current_season

    traded_seasons = {
        int(p["season"])
        for p in (traded_picks or [])
        if str(p.get("season", "")).isdigit() and int(p["season"]) >= first_season
    }
    seasons = sorted(set(range(first_season, first_season + 3)) | traded_seasons)

    owner_of: dict[tuple[int, int, int], int] = {}
    for pick in traded_picks or []:
        if not str(pick.get("season", "")).isdigit():
            continue
        try:
            key = (int(pick["season"]), int(pick["round"]), int(pick["roster_id"]))
            owner_of[key] = int(pick["owner_id"])
        except (KeyError, TypeError, ValueError):
            continue

    by_owner: dict[int, list[dict[str, Any]]] = {rid: [] for rid in roster_ids}
    for season in seasons:
        for rnd in range(1, rounds + 1):
            for original in roster_ids:
                owner = owner_of.get((season, rnd, original), original)
                if owner not in by_owner:
                    by_owner[owner] = []
                by_owner[owner].append(
                    {
                        "season": season,
                        "round": rnd,
                        "original_roster_id": original,
                        "is_own": owner == original,
                    }
                )
    return by_owner


def build_free_agents(
    players: dict[str, dict[str, Any]],
    teams: list[dict[str, Any]],
    limits: dict[str, int] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Top unrostered players per position, ranked by Sleeper's search_rank.

    Availability comes from the ``rosterable`` flag that ``prune_players.py``
    precomputes, never from Sleeper's ``active``: Sleeper marks Tom Brady
    ``active: true, status: "Active"``, so filtering on it published a list that
    was 68% retired players, with the retired stars sorted to the top because
    ``search_rank`` is lifetime popularity.
    """
    limits = limits or FREE_AGENT_LIMITS
    rostered = {pid for team in teams for pid in team["player_ids"]}
    buckets: dict[str, list[dict[str, Any]]] = {pos: [] for pos in limits}

    for pid, raw in players.items():
        if str(pid) in rostered or not raw.get("rosterable"):
            continue
        positions = set(raw.get("fantasy_positions") or [])
        if raw.get("position"):
            positions.add(raw["position"])
        matched = positions & set(limits)
        if not matched:
            continue
        if _rank_of(raw) == UNRANKED and not (matched & RANKLESS_POSITIONS):
            continue  # Unranked players are camp bodies; they only add noise.
        compact = compact_player(pid, players)
        for pos in matched:
            buckets[pos].append(compact)

    for pos, rows in buckets.items():
        rows.sort(key=lambda p: (p["rank"], p["name"]))
        buckets[pos] = rows[: limits[pos]]
    return buckets


def build_trending(
    rows: list[dict[str, Any]] | None,
    players: dict[str, dict[str, Any]],
    teams: list[dict[str, Any]],
    limit: int = 25,
) -> list[dict[str, Any]]:
    rostered = {pid for team in teams for pid in team["player_ids"]}
    out = []
    for row in (rows or [])[:limit]:
        player = compact_player(row.get("player_id"), players)
        player["count"] = row.get("count")
        player["rostered_in_league"] = player["id"] in rostered
        out.append(player)
    return out


def roster_profile(team: dict[str, Any]) -> dict[str, Any]:
    """Aggregates computed once here rather than by the reader.

    A model asked "how many running backs does roster 4 have?" has to tally
    twenty-odd lines and often gets it wrong; reading a number off a table it
    gets right every time. Positional depth and roster age are what nearly every
    real question ("who needs a quarterback?", "who is rebuilding?") reduces to.
    """
    everyone = team["starters"] + team["bench"] + team["taxi"] + team["reserve"]
    counts: dict[str, int] = {}
    ages: list[float] = []
    young = 0
    for player in everyone:
        counts[player["pos"] or "OTHER"] = counts.get(player["pos"] or "OTHER", 0) + 1
        age = player.get("age")
        if isinstance(age, (int, float)) and not isinstance(age, bool):
            ages.append(float(age))
            if age <= 25:
                young += 1
    return {
        "size": len(everyone),
        "by_position": dict(sorted(counts.items(), key=lambda kv: (POSITION_SORT.get(kv[0], 99), kv[0]))),
        "average_age": round(sum(ages) / len(ages), 1) if ages else None,
        "age_25_and_under": young,
    }


POSITION_SORT = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "K": 4, "DEF": 5}


def pick_summary(held: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick capital reduced to the counts a trade conversation actually uses."""
    by_season: dict[int, int] = {}
    firsts = 0
    for pick in held or []:
        by_season[pick["season"]] = by_season.get(pick["season"], 0) + 1
        if pick["round"] == 1:
            firsts += 1
    return {
        "total": len(held or []),
        "firsts": firsts,
        "by_season": dict(sorted(by_season.items())),
    }


def season_state(league: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Where the NFL calendar actually is, spelled out.

    ``week: 1`` alone reads as "regular season week 1, games played" to a model,
    which then invents narratives about teams struggling at 0-0. During the
    preseason every record genuinely is 0-0 and every points total genuinely is
    0, so the document has to say that those zeros are expected rather than
    missing.
    """
    season_type = (state.get("season_type") or "").lower()
    week = state.get("week")
    season = str(league.get("season") or state.get("season") or "")
    started = season_type in ("regular", "post")

    if season_type == "pre":
        plain = (
            f"It is the {season} NFL preseason. The regular season has not started yet, so "
            "every team's record is 0-0 and every points total is 0. Those zeros are correct, "
            "not missing data — do not read them as teams performing badly."
        )
    elif season_type == "regular":
        plain = (
            f"It is week {week} of the {season} NFL regular season. Records and points below "
            "reflect games played so far."
        )
    elif season_type == "post":
        plain = f"The {season} NFL regular season is over and the playoffs are underway (week {week})."
    elif season_type == "off":
        plain = (
            f"It is the NFL offseason following the {season} season. Rosters can change through "
            "trades and free agency, but no games are being played."
        )
    else:
        plain = f"Season {season}, NFL week {week}."

    return {
        "season": season,
        "week": week,
        "season_type": season_type,
        "regular_season_started": started,
        "start_date": state.get("season_start_date"),
        "league_status": league.get("status"),
        "plain": plain,
    }


def build_matchups(
    rows: list[dict[str, Any]] | None, teams: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """This week's pairings, with live scores when there are any."""
    names = {t["roster_id"]: t["team_name"] for t in teams}
    grouped: dict[Any, list[dict[str, Any]]] = {}
    for row in rows or []:
        grouped.setdefault(row.get("matchup_id"), []).append(
            {
                "roster_id": row.get("roster_id"),
                "team_name": names.get(row.get("roster_id"), f"Roster {row.get('roster_id')}"),
                "points": row.get("points") or 0,
            }
        )
    out = []
    for matchup_id, side in sorted(grouped.items(), key=lambda kv: (kv[0] is None, kv[0])):
        out.append(
            {
                "matchup_id": matchup_id,
                "teams": sorted(side, key=lambda t: t["roster_id"]),
                "any_points": any(t["points"] for t in side),
            }
        )
    return out


def build_transactions(
    rows: list[dict[str, Any]] | None,
    players: dict[str, dict[str, Any]],
    teams: list[dict[str, Any]],
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Recent moves, resolved into names.

    Sleeper returns ``{"adds": {"11583": 4}}`` — a player id mapped to a roster
    id. Left raw, answering "any recent moves?" means the reader joining two id
    spaces by hand. Both sides are resolved here instead.
    """
    names = {t["roster_id"]: t["team_name"] for t in teams}
    completed = [t for t in (rows or []) if t.get("status") == "complete"]
    completed.sort(key=lambda t: t.get("status_updated") or 0, reverse=True)

    out: list[dict[str, Any]] = []
    for tx in completed[:limit]:
        moves: dict[int, dict[str, list[str]]] = {}

        def side(roster_id: Any) -> dict[str, list[str]]:
            rid = int(roster_id)
            return moves.setdefault(rid, {"added": [], "dropped": [], "picks": [], "faab": []})

        for pid, roster_id in (tx.get("adds") or {}).items():
            side(roster_id)["added"].append(compact_player(pid, players)["name"])
        for pid, roster_id in (tx.get("drops") or {}).items():
            side(roster_id)["dropped"].append(compact_player(pid, players)["name"])
        for pick in tx.get("draft_picks") or []:
            label = f"{pick.get('season')} round {pick.get('round')}"
            original = names.get(pick.get("roster_id"), f"roster {pick.get('roster_id')}")
            if pick.get("owner_id") is not None:
                side(pick["owner_id"])["picks"].append(f"+{label} ({original}'s)")
            if pick.get("previous_owner_id") is not None:
                side(pick["previous_owner_id"])["picks"].append(f"-{label} ({original}'s)")
        for budget in tx.get("waiver_budget") or []:
            if budget.get("sender") is not None:
                side(budget["sender"])["faab"].append(f"-${budget.get('amount')}")
            if budget.get("receiver") is not None:
                side(budget["receiver"])["faab"].append(f"+${budget.get('amount')}")

        parts = []
        for rid in sorted(moves):
            bits = []
            for label, key in (("added", "added"), ("dropped", "dropped")):
                if moves[rid][key]:
                    bits.append(f"{label} {', '.join(moves[rid][key])}")
            for key in ("picks", "faab"):
                if moves[rid][key]:
                    bits.append(", ".join(moves[rid][key]))
            if bits:
                parts.append(f"{names.get(rid, f'Roster {rid}')} {'; '.join(bits)}")

        out.append(
            {
                "type": tx.get("type"),
                "when": tx.get("status_updated"),
                "roster_ids": tx.get("roster_ids") or [],
                "summary": " · ".join(parts) or "no player movement",
            }
        )
    return out


def prior_season(
    league: dict[str, Any] | None,
    rosters: list[dict[str, Any]] | None,
    users: list[dict[str, Any]] | None,
    bracket: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Last season's finish. In a dynasty league that context never stops mattering."""
    if not league or not rosters:
        return None
    user_by_id = {str(u.get("user_id")): u for u in (users or [])}
    rows = []
    for roster in rosters:
        settings = roster.get("settings") or {}
        user = user_by_id.get(str(roster.get("owner_id"))) or {}
        metadata = user.get("metadata") or {}
        rows.append(
            {
                "roster_id": int(roster.get("roster_id") or 0),
                "team_name": metadata.get("team_name") or user.get("display_name") or "unknown",
                "owner": user.get("display_name") or "unknown",
                "record": score_record(settings),
            }
        )
    rows.sort(key=lambda r: (-r["record"]["wins"], -r["record"]["points_for"]))

    champion = None
    for game in bracket or []:
        # Sleeper flags the placement game for Nth place with "p"; p == 1 is the final.
        if game.get("p") == 1 and game.get("w"):
            champion = int(game["w"])
            break
    names = {r["roster_id"]: r["team_name"] for r in rows}
    return {
        "season": str(league.get("season") or ""),
        "standings": rows,
        "champion_roster_id": champion,
        "champion": names.get(champion) if champion else None,
    }


def league_summary(league: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    settings = league.get("settings") or {}
    positions = league.get("roster_positions") or []
    starting = [p for p in positions if p not in ("BN", "TAXI", "IR")]
    scoring = {
        k: v
        for k, v in sorted((league.get("scoring_settings") or {}).items())
        if isinstance(v, (int, float)) and v
    }
    return {
        "league_id": str(league.get("league_id") or ""),
        "name": league.get("name"),
        "season": str(league.get("season") or ""),
        "status": league.get("status"),
        "sport_week": state.get("week"),
        "sport_season_type": state.get("season_type"),
        "total_teams": settings.get("num_teams"),
        "starting_lineup": starting,
        "bench_slots": positions.count("BN"),
        "taxi_slots": settings.get("taxi_slots") or positions.count("TAXI"),
        "ir_slots": positions.count("IR"),
        "playoff_teams": settings.get("playoff_teams"),
        "playoff_week_start": settings.get("playoff_week_start"),
        "waiver_type": settings.get("waiver_type"),
        "waiver_budget": settings.get("waiver_budget"),
        "draft_rounds": settings.get("draft_rounds"),
        "scoring": scoring,
    }
