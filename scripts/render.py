"""Render the snapshot into the files published to GitHub Pages.

``league.md`` is the product: one fetch, self-contained, sized to fit comfortably
in a chat model's context. Every player line begins with its Sleeper id so this
one document also acts as the decoder ring for any live API response the model
fetches afterwards.
"""

from __future__ import annotations

import json
from typing import Any

API_BASE = "https://api.sleeper.app/v1"
POSITION_ORDER = ["QB", "RB", "WR", "TE", "K", "DEF"]


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _player_line(p: dict[str, Any], *, tag: str = "") -> str:
    bits = [p["pos"] or "?", p["team"] or "FA"]
    if p.get("age") is not None:
        bits.append(f"{p['age']}yo")
    if p.get("exp") == 0:
        bits.append("rookie")
    line = f"{p['id']} {p['name']} ({', '.join(bits)})"
    if p.get("injury"):
        line += f" — {p['injury']}"
    if tag:
        line += f" [{tag}]"
    return line


def _roster_block(team: dict[str, Any]) -> list[str]:
    tagged: list[tuple[dict[str, Any], str]] = []
    tagged += [(p, "") for p in team["starters"]]
    tagged += [(p, "") for p in team["bench"]]
    tagged += [(p, "taxi") for p in team["taxi"]]
    tagged += [(p, "IR") for p in team["reserve"]]

    by_pos: dict[str, list[str]] = {}
    for player, tag in tagged:
        by_pos.setdefault(player["pos"] or "OTHER", []).append(_player_line(player, tag=tag))

    lines: list[str] = []
    ordered = [p for p in POSITION_ORDER if p in by_pos]
    ordered += sorted(p for p in by_pos if p not in POSITION_ORDER)
    for pos in ordered:
        lines.append(f"- **{pos}** ({len(by_pos[pos])})")
        lines.extend(f"  - {entry}" for entry in by_pos[pos])
    return lines


def render_markdown(snapshot: dict[str, Any]) -> str:
    league = snapshot["league"]
    teams = snapshot["teams"]
    league_id = league["league_id"]
    out: list[str] = []
    add = out.append

    add(f"# {league.get('name') or 'Sleeper league'} — league brief")
    add("")
    add(f"Snapshot generated **{snapshot['generated_at']}** (UTC).")
    add(
        f"Season **{league['season']}**"
        + (f", NFL week **{league['sport_week']}**" if league.get("sport_week") else "")
        + f". League id `{league_id}`."
    )
    add("")
    add(
        "> **Read this first.** This file is a point-in-time snapshot of a Sleeper dynasty "
        "fantasy football league, written for an AI assistant to answer questions about. "
        "Rosters, standings, draft picks and free agents below were current as of the "
        "timestamp above. If the question depends on something that may have changed since "
        "then, see **Live data** at the end of this file for the exact URLs to fetch. "
        "Every player is listed as `<sleeper_id> <name>`, so you can decode player ids in any "
        "live API response using the tables in this document."
    )
    add("")

    # --- Settings -----------------------------------------------------------
    add("## League settings")
    add("")
    add(f"- Teams: {league.get('total_teams') or len(teams)}")
    if league.get("starting_lineup"):
        lineup = ", ".join(league["starting_lineup"])
        add(f"- Starting lineup ({len(league['starting_lineup'])} slots): {lineup}")
        if "SUPER_FLEX" in league["starting_lineup"]:
            add(
                "- **Superflex league.** The SUPER_FLEX slot accepts a quarterback, so most "
                "teams start two. This raises quarterback value sharply relative to a "
                "standard league — weigh that in any trade or roster analysis."
            )
    add(f"- Bench slots: {league.get('bench_slots')}")
    if league.get("taxi_slots"):
        add(f"- Taxi squad slots: {league['taxi_slots']}")
    if league.get("ir_slots"):
        add(f"- IR slots: {league['ir_slots']}")
    if league.get("waiver_budget") is not None:
        add(f"- FAAB budget: {league['waiver_budget']} per team per season")
    if league.get("draft_rounds"):
        add(f"- Rookie draft rounds: {league['draft_rounds']}")
    if league.get("playoff_teams"):
        add(
            f"- Playoffs: {league['playoff_teams']} teams, starting week "
            f"{league.get('playoff_week_start')}"
        )
    add("")
    if league.get("scoring"):
        add("Scoring settings (only non-zero values shown):")
        add("")
        add("```")
        add(", ".join(f"{k}={_num(v)}" for k, v in league["scoring"].items()))
        add("```")
        add("")

    # --- Standings ----------------------------------------------------------
    add("## Standings")
    add("")
    add("| # | Team | Owner | roster_id | W-L-T | PF | PA | FAAB left |")
    add("|---|---|---|---|---|---|---|---|")
    for i, team in enumerate(snapshot["standings"], start=1):
        rec = team["record"]
        faab = "—" if team["faab_left"] is None else str(team["faab_left"])
        add(
            f"| {i} | {team['team_name']} | {team['owner']} | {team['roster_id']} | "
            f"{rec['wins']}-{rec['losses']}-{rec['ties']} | {rec['points_for']} | "
            f"{rec['points_against']} | {faab} |"
        )
    add("")
    add(
        "`roster_id` is the join key used by the Sleeper API — live roster and transaction "
        "responses identify teams by that number, not by name."
    )
    add("")

    # --- Rosters ------------------------------------------------------------
    add("## Rosters")
    add("")
    for team in teams:
        rec = team["record"]
        add(f"### {team['team_name']} (roster_id {team['roster_id']}, owner {team['owner']})")
        add("")
        add(
            f"Record {rec['wins']}-{rec['losses']}-{rec['ties']} · "
            f"PF {rec['points_for']} · PA {rec['points_against']} · "
            f"FAAB left {team['faab_left'] if team['faab_left'] is not None else '—'}"
        )
        add("")
        out.extend(_roster_block(team))
        add("")

    # --- Picks --------------------------------------------------------------
    picks = snapshot.get("picks") or {}
    if picks:
        add("## Future rookie draft picks")
        add("")
        add(
            "Who currently owns each future pick, after trades. "
            "`(own)` means the team's original pick; otherwise the original owner is named."
        )
        add("")
        names = {t["roster_id"]: t["team_name"] for t in teams}
        for team in teams:
            held = picks.get(team["roster_id"]) or []
            if not held:
                continue
            add(f"**{team['team_name']}** (roster_id {team['roster_id']})")
            by_season: dict[int, list[str]] = {}
            for pick in sorted(held, key=lambda p: (p["season"], p["round"], p["original_roster_id"])):
                origin = "own" if pick["is_own"] else f"from {names.get(pick['original_roster_id'], '?')}"
                by_season.setdefault(pick["season"], []).append(f"{_ordinal(pick['round'])} ({origin})")
            for season in sorted(by_season):
                add(f"- {season}: {', '.join(by_season[season])}")
            add("")

    # --- Free agents --------------------------------------------------------
    free_agents = snapshot.get("free_agents") or {}
    if any(free_agents.values()):
        add("## Top available free agents")
        add("")
        add(
            "Unrostered players, ranked by Sleeper's search rank (a popularity/relevance "
            "proxy — lower is better). This is a truncated list of the most relevant names, "
            "not every available player."
        )
        add("")
        for pos in POSITION_ORDER:
            rows = free_agents.get(pos)
            if not rows:
                continue
            add(f"### {pos} ({len(rows)})")
            add("")
            for player in rows:
                add(f"- {_player_line(player)}")
            add("")

    # --- Trending -----------------------------------------------------------
    for label, key in (("Most added", "adds"), ("Most dropped", "drops")):
        rows = (snapshot.get("trending") or {}).get(key)
        if not rows:
            continue
        add(f"## {label} across Sleeper (last 24h)")
        add("")
        for player in rows:
            owned = " — already rostered in this league" if player.get("rostered_in_league") else ""
            add(f"- {_player_line(player)} · {player.get('count')} transactions{owned}")
        add("")

    # --- Live data ----------------------------------------------------------
    add("## Live data")
    add("")
    add(
        f"Everything above was captured at {snapshot['generated_at']} UTC. If the question "
        "depends on something more recent — a waiver claim that just processed, a trade from "
        "this morning, current scoring — fetch these directly:"
    )
    add("")
    add(f"- **Current rosters:** `{API_BASE}/league/{league_id}/rosters`")
    add(f"- **League info and settings:** `{API_BASE}/league/{league_id}`")
    add(f"- **Managers:** `{API_BASE}/league/{league_id}/users`")
    add(f"- **Traded picks:** `{API_BASE}/league/{league_id}/traded_picks`")
    add(
        f"- **Transactions for NFL week N** (trades, waivers, free agent moves): "
        f"`{API_BASE}/league/{league_id}/transactions/N`"
    )
    add(f"- **Matchups and live scores for week N:** `{API_BASE}/league/{league_id}/matchups/N`")
    add(f"- **Current NFL week:** `{API_BASE}/state/nfl`")
    add("")
    add("Notes for reading those responses:")
    add("")
    add("- They identify teams by `roster_id` and players by numeric player id. Both decode against the tables above.")
    add("- Fantasy points arrive split in two fields: `fpts` plus `fpts_decimal`/100.")
    add("- In `transactions`, `adds` and `drops` map player id to the `roster_id` involved.")
    add(
        f"- **Do not fetch `{API_BASE}/players/nfl`.** It is roughly 5 MB and will overflow "
        "or time out. Player names are already resolved in this document."
    )
    add("- The API is public and read-only. No key, no auth, no rate limit worth worrying about.")
    add("")
    add("---")
    add("")
    add(
        "Generated by [sleeper-league-sync](https://github.com/SamBilletdeaux/sleeper-league-sync). "
        "Source: the public Sleeper API."
    )
    add("")
    return "\n".join(out)


def _num(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def render_json(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n"


def render_index(snapshot: dict[str, Any], site_url: str) -> str:
    league = snapshot["league"]
    brief_url = f"{site_url}/league.md"
    prompt = (
        f"Read {brief_url} and use it to answer my questions about my "
        "dynasty fantasy football league."
    )
    teams = len(snapshot["teams"])
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(league.get("name") or "Sleeper league")} — LLM brief</title>
<style>
  :root {{
    --bg: #fbfbfa; --fg: #1a1a18; --muted: #6b6b66;
    --card: #ffffff; --border: #e3e3df; --accent: #7c4dff;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #17171a; --fg: #ececea; --muted: #9b9b95;
      --card: #212125; --border: #33333a; --accent: #a98bff;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 3rem 1.25rem; background: var(--bg); color: var(--fg);
    font: 16px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }}
  main {{ max-width: 44rem; margin: 0 auto; }}
  h1 {{ font-size: 1.9rem; line-height: 1.2; margin: 0 0 .35rem; letter-spacing: -.02em; }}
  .sub {{ color: var(--muted); margin: 0 0 2.25rem; }}
  h2 {{ font-size: 1.05rem; margin: 2.25rem 0 .75rem; letter-spacing: -.01em; }}
  .card {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 1.1rem 1.25rem;
  }}
  code, pre {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .875rem; }}
  pre {{ margin: 0; white-space: pre-wrap; word-break: break-word; }}
  a {{ color: var(--accent); }}
  ol {{ padding-left: 1.25rem; }}
  li {{ margin: .4rem 0; }}
  .files {{ list-style: none; padding: 0; }}
  .files li {{ padding: .55rem 0; border-bottom: 1px solid var(--border); }}
  .files li:last-child {{ border-bottom: 0; }}
  .files span {{ color: var(--muted); }}
  footer {{ margin-top: 3rem; color: var(--muted); font-size: .875rem; }}
</style>
</head>
<body>
<main>
  <h1>{_esc(league.get("name") or "Sleeper league")}</h1>
  <p class="sub">
    A live brief for {teams} teams, built for AI assistants.<br>
    Updated {_esc(snapshot["generated_at"])} UTC · refreshes every 6 hours.
  </p>

  <h2>Ask an AI about your league</h2>
  <p>Paste this into ChatGPT, Claude, Gemini, or any assistant that can browse the web:</p>
  <div class="card"><pre>{_esc(prompt)}</pre></div>

  <h2>How it works</h2>
  <ol>
    <li>A GitHub Action pulls this league from the public Sleeper API every 6 hours.</li>
    <li>It writes a compact, fully-readable brief &mdash; rosters with real player names,
        standings, future rookie picks, and top free agents.</li>
    <li>The brief also tells the assistant how to query Sleeper directly, so it can pull
        genuinely live data when your question needs something newer than the snapshot.</li>
  </ol>

  <h2>Files</h2>
  <ul class="files">
    <li><a href="league.md">league.md</a> <span>&mdash; the brief. This is the one to share.</span></li>
    <li><a href="league.json">league.json</a> <span>&mdash; same data, structured, for tools and scripts.</span></li>
  </ul>

  <footer>
    No account needed &mdash; these files are public.
    Source on <a href="https://github.com/SamBilletdeaux/sleeper-league-sync">GitHub</a>.
  </footer>
</main>
</body>
</html>
"""


def _esc(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
