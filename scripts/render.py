"""Render the snapshot into the brief that gets served.

The brief is the product: one fetch, self-contained, sized to fit comfortably in
a chat model's context. Markdown is the single source of truth; the HTML page is
generated from it by ``markdown_to_html`` so the two can never drift.

Three things shape what goes in and how it reads, each answering a way models get
this wrong:

* **Aggregates are precomputed.** Counting twenty roster lines is something a
  model does unreliably and a table does perfectly.
* **The calendar is spelled out.** "Week 1" alone reads as regular-season week 1
  with games played, which invents narratives about 0-0 teams in August.
* **Nothing is left as a bare id.** Sleeper identifies teams by ``roster_id`` and
  players by numeric id; both are resolved wherever they appear.
"""

from __future__ import annotations

import json
import re
from typing import Any

API_BASE = "https://api.sleeper.app/v1"
POSITION_ORDER = ["QB", "RB", "WR", "TE", "K", "DEF"]

# Which positions a flex slot will accept, used to work out whether a roster can
# actually field a legal lineup.
FLEX_ELIGIBLE = {
    "FLEX": {"RB", "WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
    "REC_FLEX": {"WR", "TE"},
    "WRRB_FLEX": {"RB", "WR"},
}


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _num(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _player_line(p: dict[str, Any], *, tag: str = "") -> str:
    bits = [p["pos"] or "?", p["team"] or "no NFL team"]
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


def _profile_line(team: dict[str, Any]) -> str:
    profile = team.get("profile") or {}
    counts = profile.get("by_position") or {}
    depth = ", ".join(f"{pos} {n}" for pos, n in counts.items())
    bits = [f"{profile.get('size', 0)} players", depth]
    if profile.get("average_age") is not None:
        bits.append(f"average age {profile['average_age']}")
    bits.append(f"{profile.get('age_25_and_under', 0)} aged 25 or under")
    return " · ".join(b for b in bits if b)


def lineup_requirements(starting_lineup: list[str] | None) -> dict[str, int]:
    """How many of each position the starting lineup demands outright."""
    required: dict[str, int] = {}
    for slot in starting_lineup or []:
        if slot in POSITION_ORDER:
            required[slot] = required.get(slot, 0) + 1
    return required


def roster_gaps(team: dict[str, Any], starting_lineup: list[str] | None) -> list[str]:
    """Positions where this roster cannot start a legal lineup, or has no cover.

    Stated as fact rather than advice — "no kicker rostered" is checkable; "you
    should trade for a kicker" is an opinion the reader can form on its own.
    """
    counts = (team.get("profile") or {}).get("by_position") or {}
    gaps = []
    for pos, need in lineup_requirements(starting_lineup).items():
        have = counts.get(pos, 0)
        if have < need:
            gaps.append(
                f"cannot fill the {pos} slot{'s' if need > 1 else ''} — needs {need}, rosters {have}"
            )
        elif have == need:
            gaps.append(f"exactly {need} {pos} with no backup")
    return gaps


def render_markdown(snapshot: dict[str, Any], viewer: dict[str, Any] | None = None) -> str:
    league = snapshot["league"]
    teams = snapshot["teams"]
    league_id = league["league_id"]
    season = snapshot.get("season_state") or {}
    out: list[str] = []
    add = out.append

    add(f"# {league.get('name') or 'Sleeper league'} — live league brief")
    add("")
    add(f"Generated **{snapshot['generated_at']}** (UTC). League id `{league_id}`.")
    add("")
    add(
        "> **Read this first.** This is a live brief for a Sleeper dynasty fantasy football "
        "league. It is rebuilt from the Sleeper API every time this page is requested, so it is "
        "current as of the timestamp above and you do not need to fetch anything else to answer "
        "questions about this league. Answer only from what is written here. Every team and every "
        "rostered player in the league appears below, so if a player is not in this document, "
        "that player is not in this league — say so instead of guessing. Players are written as "
        "`<sleeper_id> <name>`. The **Reference** section at the end states plainly what this "
        "document does and does not cover."
    )
    add("")

    # --- Viewer ---------------------------------------------------------------
    if viewer:
        add(f"## You are talking to {viewer['team_name']}")
        add("")
        add(
            f"The person asking manages **{viewer['team_name']}** — roster_id "
            f"`{viewer['roster_id']}`, Sleeper username `{viewer['owner']}`. When they say "
            '"my team", "I", "me" or "my roster", they mean this team. Answer from their '
            "perspective unless they ask about someone else."
        )
        add("")
        add(f"- Their roster: {_profile_line(viewer)}")
        if viewer.get("faab_left") is not None:
            add(f"- FAAB remaining: {viewer['faab_left']}")
        picks = viewer.get("pick_summary") or {}
        if picks.get("total"):
            seasons = ", ".join(f"{yr}: {n}" for yr, n in (picks.get("by_season") or {}).items())
            add(f"- Future rookie picks: {picks['total']} total, {picks['firsts']} of them 1st-rounders ({seasons})")
        gaps = viewer.get("gaps") or []
        if gaps:
            add(f"- Roster gaps: {'; '.join(gaps)}")
        else:
            add("- Roster gaps: none — every starting slot has at least one backup")
        add("")

    # --- Season state ---------------------------------------------------------
    add("## Where the season stands")
    add("")
    add(season.get("plain") or "")
    add("")
    if season.get("start_date"):
        add(f"- Season start date: {season['start_date']}")
    add(f"- Sleeper reports this league as `{season.get('league_status')}`")
    if not season.get("regular_season_started"):
        add(
            "- Because no regular season games have counted yet, standings order below is not "
            "meaningful. Judge teams on roster strength, age and draft capital instead."
        )
    add("")

    # --- Settings -------------------------------------------------------------
    add("## How this league works")
    add("")
    add(f"- Teams: {league.get('total_teams') or len(teams)}")
    add("- Format: dynasty — managers keep their rosters between seasons, so player age and future draft picks carry real value.")
    if league.get("starting_lineup"):
        lineup = ", ".join(league["starting_lineup"])
        add(f"- Starting lineup ({len(league['starting_lineup'])} slots): {lineup}")
        if "SUPER_FLEX" in league["starting_lineup"]:
            add(
                "- **Superflex league.** The SUPER_FLEX slot accepts a quarterback, so nearly "
                "every team starts two. Quarterbacks are therefore far more valuable here than "
                "in a standard league — weigh that in any trade or roster question."
            )
        for slot, eligible in FLEX_ELIGIBLE.items():
            if slot in league["starting_lineup"] and slot != "SUPER_FLEX":
                add(f"- A {slot} slot accepts: {', '.join(sorted(eligible))}")
    add(f"- Bench slots: {league.get('bench_slots')}")
    if league.get("taxi_slots"):
        add(f"- Taxi squad slots: {league['taxi_slots']} (for rookies kept off the active roster)")
    if league.get("ir_slots"):
        add(f"- IR slots: {league['ir_slots']}")
    if league.get("waiver_budget") is not None:
        add(f"- FAAB budget: {league['waiver_budget']} per team per season, spent bidding on free agents")
    if league.get("draft_rounds"):
        add(f"- Rookie draft: {league['draft_rounds']} rounds each offseason")
    if league.get("playoff_teams"):
        add(f"- Playoffs: {league['playoff_teams']} teams, starting week {league.get('playoff_week_start')}")
    add("")
    if league.get("scoring"):
        add("Scoring settings (only non-zero values shown):")
        add("")
        add("```")
        add(", ".join(f"{k}={_num(v)}" for k, v in league["scoring"].items()))
        add("```")
        add("")

    # --- Standings ------------------------------------------------------------
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
        "`roster_id` is the join key Sleeper uses — its API identifies teams by that number "
        "rather than by name."
    )
    add("")

    prior = snapshot.get("prior_season")
    if prior and prior.get("standings"):
        add(f"### How last season ({prior['season']}) finished")
        add("")
        if prior.get("champion"):
            add(f"**{prior['champion']}** won the {prior['season']} championship.")
            add("")
        add("| # | Team | Owner | W-L-T | PF |")
        add("|---|---|---|---|---|")
        for i, row in enumerate(prior["standings"], start=1):
            rec = row["record"]
            add(
                f"| {i} | {row['team_name']} | {row['owner']} | "
                f"{rec['wins']}-{rec['losses']}-{rec['ties']} | {rec['points_for']} |"
            )
        add("")

    # --- Rosters --------------------------------------------------------------
    add("## Rosters")
    add("")
    add(
        "Each team's full roster. The summary line under each heading is precomputed — use it "
        "rather than counting the player lines yourself."
    )
    add("")
    for team in teams:
        rec = team["record"]
        add(f"### {team['team_name']} (roster_id {team['roster_id']}, owner {team['owner']})")
        add("")
        add(f"{_profile_line(team)}")
        add("")
        add(
            f"Record {rec['wins']}-{rec['losses']}-{rec['ties']} · "
            f"PF {rec['points_for']} · PA {rec['points_against']} · "
            f"FAAB left {team['faab_left'] if team['faab_left'] is not None else '—'}"
        )
        gaps = roster_gaps(team, league.get("starting_lineup"))
        if gaps:
            add("")
            add(f"Lineup gaps: {'; '.join(gaps)}.")
        add("")
        out.extend(_roster_block(team))
        add("")

    # --- Matchups -------------------------------------------------------------
    matchups = snapshot.get("matchups") or []
    if matchups:
        week = season.get("week")
        add(f"## This week's matchups (NFL week {week})")
        add("")
        scored = any(m["any_points"] for m in matchups)
        if not scored:
            add("No points have been scored yet this week — these are the scheduled pairings.")
            add("")
        for matchup in matchups:
            side = matchup["teams"]
            if len(side) == 2:
                a, b = side
                if scored:
                    add(f"- {a['team_name']} {a['points']} — {b['points']} {b['team_name']}")
                else:
                    add(f"- {a['team_name']} (roster {a['roster_id']}) vs {b['team_name']} (roster {b['roster_id']})")
            else:
                add(f"- {', '.join(t['team_name'] for t in side)}")
        add("")

    # --- Transactions ---------------------------------------------------------
    transactions = snapshot.get("transactions") or []
    add("## Recent moves")
    add("")
    if transactions:
        add(f"The {len(transactions)} most recent completed transactions, newest first.")
        add("")
        for tx in transactions:
            add(f"- **{tx['type'].replace('_', ' ')}** — {tx['summary']}")
    else:
        add("No completed transactions have been recorded for the current week.")
    add("")

    # --- Picks ----------------------------------------------------------------
    picks = snapshot.get("picks") or {}
    if picks:
        add("## Future rookie draft picks")
        add("")
        add(
            "Who currently owns each future pick, after trades. `(own)` means the team's own "
            "original pick; otherwise the team it came from is named."
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
            for season_year in sorted(by_season):
                add(f"- {season_year}: {', '.join(by_season[season_year])}")
            add("")

    # --- Free agents ----------------------------------------------------------
    free_agents = snapshot.get("free_agents") or {}
    if any(free_agents.values()):
        add("## Available free agents")
        add("")
        add(
            "Players on no roster in this league who are on an NFL team and have had recent "
            "news. Ordered by Sleeper's search rank, a popularity proxy where lower is better. "
            "This is the most relevant slice, not every available player."
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

    # --- Trending -------------------------------------------------------------
    for label, key in (("Most added", "adds"), ("Most dropped", "drops")):
        rows = (snapshot.get("trending") or {}).get(key)
        if not rows:
            continue
        add(f"## {label} across all of Sleeper (last 24h)")
        add("")
        add(
            "Activity across every Sleeper league, not just this one — a signal about which "
            "players are being picked up generally."
        )
        add("")
        for player in rows:
            owned = " — already rostered in this league" if player.get("rostered_in_league") else ""
            add(f"- {_player_line(player)} · {player.get('count')} transactions{owned}")
        add("")

    # --- Reference ------------------------------------------------------------
    add("## Reference")
    add("")
    add("### What this document covers")
    add("")
    add("Everything needed for questions about this league, current as of the timestamp at the top:")
    add("")
    add("- League rules, scoring and roster requirements")
    add("- Every team's full roster, record, FAAB and precomputed positional depth")
    add("- Standings now, and how last season finished")
    add("- Ownership of every future rookie draft pick, after trades")
    add("- Recent completed transactions")
    add("- Available free agents worth considering")
    add("")
    add("### What it does not cover")
    add("")
    add("Say so rather than inventing an answer if asked about:")
    add("")
    add("- **Dynasty trade values or player rankings.** Sleeper does not publish them, so no number here represents a player's trade value. `search_rank` is popularity, not value, and it flatters long-retired stars.")
    add("- **Injury detail beyond the status tag** shown next to a player.")
    add("- **Projections, or how a player is expected to perform.**")
    add("- **Full free agent lists.** The lists above are truncated to the most relevant names.")
    add("- **Anything from more than a moment ago** if this page was cached; the timestamp at the top is authoritative.")
    add("")
    add("### Checking this page was actually read")
    add("")
    add(
        f"If someone doubts the answer came from this document, the generated timestamp is "
        f"`{snapshot['generated_at']}`. Quoting it back proves the page was read."
    )
    add("")
    add("### Fetching newer data directly")
    add("")
    add(
        "Rarely needed, since this page is rebuilt on request. If something changed in the last "
        "minute, these public endpoints need no key or authentication:"
    )
    add("")
    add("| If asked about | Fetch |")
    add("|---|---|")
    add(f"| Current rosters | `{API_BASE}/league/{league_id}/rosters` |")
    add(f"| Trades and waiver moves in week N | `{API_BASE}/league/{league_id}/transactions/N` |")
    add(f"| Live scores for week N | `{API_BASE}/league/{league_id}/matchups/N` |")
    add(f"| Which NFL week it is | `{API_BASE}/state/nfl` |")
    add(f"| League settings | `{API_BASE}/league/{league_id}` |")
    add(f"| Managers | `{API_BASE}/league/{league_id}/users` |")
    add(f"| Draft pick ownership | `{API_BASE}/league/{league_id}/traded_picks` |")
    add("")
    add("Reading those responses:")
    add("")
    add("- Teams are identified by `roster_id` and players by numeric id; both decode against the tables above.")
    add("- Fantasy points arrive split across two fields: `fpts` plus `fpts_decimal`/100.")
    add("- In `transactions`, `adds` and `drops` map a player id to the `roster_id` involved.")
    add(
        f"- **Do not fetch `{API_BASE}/players/nfl`.** It is roughly 16 MB and will overflow or "
        "time out. Every player relevant to this league is already named above."
    )
    add("")
    add("---")
    add("")
    add(
        "Generated by [sleeper-league-sync](https://github.com/SamBilletdeaux/sleeper-league-sync) "
        "from the public Sleeper API."
    )
    add("")
    return "\n".join(out)


def render_json(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- #
# Markdown -> HTML
#
# Deliberately minimal: it only has to handle the constructs render_markdown
# emits, which is a closed set this module controls. Generating the page from
# the markdown keeps one source of truth, and emitting real tags (rather than
# one big <pre>) means an assistant's HTML-to-text conversion recovers clean
# structure instead of an undifferentiated code block.
# --------------------------------------------------------------------------- #

def _esc(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _inline(text: str) -> str:
    """Escape, then restore the inline markup render_markdown uses."""
    out = _esc(text)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', out)
    return out


def markdown_to_html(markdown: str) -> str:
    lines = markdown.split("\n")
    html: list[str] = []
    i = 0
    # One entry per open <ul>; True when that list was opened inside an <li>
    # that still needs closing, which is what keeps nesting valid.
    open_lists: list[bool] = []

    def close_lists(to: int = 0) -> None:
        while len(open_lists) > to:
            html.append("</ul>")
            if open_lists.pop():
                html.append("</li>")

    def open_list() -> None:
        inside_li = bool(html) and html[-1].endswith("</li>")
        if inside_li:
            html[-1] = html[-1][: -len("</li>")]
        html.append("<ul>")
        open_lists.append(inside_li)

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            close_lists()
            i += 1
            block = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(_esc(lines[i]))
                i += 1
            i += 1
            html.append("<pre><code>" + "\n".join(block) + "</code></pre>")
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and set(lines[i + 1].strip()) <= set("|-: "):
            close_lists()
            header = [c.strip() for c in stripped.strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            html.append("<table><thead><tr>")
            html.extend(f"<th>{_inline(c)}</th>" for c in header)
            html.append("</tr></thead><tbody>")
            for row in rows:
                html.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>")
            html.append("</tbody></table>")
            continue

        if not stripped:
            close_lists()
            i += 1
            continue

        if stripped == "---":
            close_lists()
            html.append("<hr>")
            i += 1
            continue

        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            close_lists()
            level = len(heading.group(1))
            html.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
            continue

        if stripped.startswith("> "):
            close_lists()
            html.append(f"<blockquote>{_inline(stripped[2:])}</blockquote>")
            i += 1
            continue

        bullet = re.match(r"^(\s*)-\s+(.*)$", line)
        if bullet:
            want = 2 if len(bullet.group(1)) >= 2 else 1
            close_lists(want)
            while len(open_lists) < want:
                open_list()
            html.append(f"<li>{_inline(bullet.group(2))}</li>")
            i += 1
            continue

        close_lists()
        html.append(f"<p>{_inline(stripped)}</p>")
        i += 1

    close_lists()
    return "\n".join(html)


PAGE_CSS = """
:root { color-scheme: light dark;
  --bg:#fbfbfa; --fg:#1a1a18; --muted:#63635c; --card:#fff; --border:#e3e3df;
  --accent:#6d3fd4; --code:#f3f3f0; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#17171a; --fg:#ececea; --muted:#9d9d96; --card:#212125; --border:#33333a;
  --accent:#b39bff; --code:#26262b; } }
* { box-sizing:border-box; }
body { margin:0; padding:2rem 1.25rem 5rem; background:var(--bg); color:var(--fg);
  font:16px/1.65 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:52rem; margin:0 auto; }
h1 { font-size:1.8rem; line-height:1.2; letter-spacing:-.02em; margin:0 0 .4rem; }
h2 { font-size:1.25rem; margin:2.5rem 0 .75rem; padding-top:.75rem;
  border-top:1px solid var(--border); letter-spacing:-.01em; }
h3 { font-size:1.02rem; margin:1.6rem 0 .5rem; }
h4 { font-size:.95rem; margin:1.2rem 0 .4rem; color:var(--muted); }
p { margin:.6rem 0; }
ul { margin:.4rem 0; padding-left:1.3rem; }
li { margin:.18rem 0; }
blockquote { margin:1.25rem 0; padding:1rem 1.15rem; background:var(--card);
  border:1px solid var(--border); border-left:3px solid var(--accent); border-radius:8px; }
code { background:var(--code); padding:.1em .35em; border-radius:4px;
  font:.875em ui-monospace,SFMono-Regular,Menlo,monospace; }
pre { background:var(--card); border:1px solid var(--border); border-radius:8px;
  padding:.9rem 1rem; overflow-x:auto; }
pre code { background:none; padding:0; white-space:pre-wrap; word-break:break-word; }
.tablewrap { overflow-x:auto; margin:.8rem 0; }
table { border-collapse:collapse; width:100%; font-size:.9rem; }
th,td { text-align:left; padding:.42rem .6rem; border-bottom:1px solid var(--border);
  white-space:nowrap; }
th { color:var(--muted); font-weight:600; }
hr { border:0; border-top:1px solid var(--border); margin:2.5rem 0 1.25rem; }
a { color:var(--accent); }
.bar { position:sticky; top:0; z-index:5; background:var(--bg);
  border-bottom:1px solid var(--border); margin:-2rem -1.25rem 1.5rem;
  padding:.85rem 1.25rem; display:flex; gap:.75rem; align-items:center;
  flex-wrap:wrap; }
.bar span { color:var(--muted); font-size:.875rem; }
button { font:inherit; font-size:.875rem; padding:.45rem .9rem; border-radius:7px;
  border:1px solid var(--border); background:var(--card); color:var(--fg);
  cursor:pointer; }
button:hover { border-color:var(--accent); color:var(--accent); }
""".strip()


def render_html(snapshot: dict[str, Any], markdown: str, *, viewer: dict[str, Any] | None = None) -> str:
    """The served page: the brief as real HTML, plus a copy-out escape hatch."""
    league = snapshot["league"]
    title = league.get("name") or "Sleeper league"
    if viewer:
        title = f"{title} — {viewer['team_name']}"
    body = markdown_to_html(markdown)
    # Tables need their own scroll container so the page body never scrolls sideways.
    body = body.replace("<table>", '<div class="tablewrap"><table>').replace(
        "</table>", "</table></div>"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="index, follow">
<meta name="description" content="{_esc(title)} — live fantasy league brief for AI assistants.">
<title>{_esc(title)} — live league brief</title>
<style>{PAGE_CSS}</style>
</head>
<body>
<main>
  <div class="bar">
    <button id="copy" type="button">Copy entire brief</button>
    <span>If your AI says it can't read this link, tap Copy and paste it into the chat instead.</span>
  </div>
{body}
</main>
<script>
// The markdown source is fetched on demand rather than embedded. Embedding it
// would double the page weight and risk an assistant's HTML-to-text conversion
// reading the whole brief twice.
(function () {{
  var btn = document.getElementById('copy');
  function flash(text) {{
    btn.textContent = text;
    setTimeout(function () {{ btn.textContent = 'Copy entire brief'; }}, 2000);
  }}
  function put(text) {{
    if (navigator.clipboard && navigator.clipboard.writeText) {{
      return navigator.clipboard.writeText(text).then(function () {{ flash('Copied'); }}, legacy);
    }}
    legacy();
    function legacy() {{
      var ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      try {{ document.execCommand('copy'); flash('Copied'); }}
      catch (e) {{ flash('Press Ctrl+C'); }}
      document.body.removeChild(ta);
    }}
  }}
  btn.addEventListener('click', function () {{
    btn.textContent = 'Copying…';
    var url = location.pathname + (location.search ? location.search + '&' : '?') + 'format=md';
    fetch(url, {{ headers: {{ 'Accept': 'text/markdown' }} }})
      .then(function (r) {{ if (!r.ok) throw new Error(r.status); return r.text(); }})
      .then(put)
      .catch(function () {{ put(document.querySelector('main').innerText); }});
  }});
}})();
</script>
</body>
</html>
"""


def render_index(snapshot: dict[str, Any], site_url: str) -> str:
    """Landing page for the static mirror, pointing at the live site."""
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
<style>{PAGE_CSS}</style>
</head>
<body>
<main>
  <h1>{_esc(league.get("name") or "Sleeper league")}</h1>
  <p>A brief for {teams} teams, written to be read by an AI assistant.
     Snapshot taken {_esc(snapshot["generated_at"])} UTC.</p>

  <h2>Ask an AI about your league</h2>
  <p>Paste this into ChatGPT, Claude, Gemini, or any assistant that can browse the web:</p>
  <pre><code>{_esc(prompt)}</code></pre>

  <h2>Files</h2>
  <ul>
    <li><a href="league.md">league.md</a> — the brief in markdown</li>
    <li><a href="league.html">league.html</a> — the same brief as a web page</li>
    <li><a href="league.json">league.json</a> — the same data, structured</li>
  </ul>

  <hr>
  <p>This is a static mirror, rebuilt daily. Source on
     <a href="https://github.com/SamBilletdeaux/sleeper-league-sync">GitHub</a>.</p>
</main>
</body>
</html>
"""
