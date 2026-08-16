"""Offline tests. No network — everything runs against tests/fixtures/."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build  # noqa: E402
import model  # noqa: E402
import prune_players  # noqa: E402
import render  # noqa: E402
from sleeper import FixtureClient, SleeperError, resolve_current_league  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def load(name: str):
    with (FIXTURES / f"{name}.json").open(encoding="utf-8") as handle:
        return json.load(handle)


class ScoreRecordTests(unittest.TestCase):
    def test_recombines_split_decimal_points(self):
        record = model.score_record(
            {"wins": 6, "losses": 2, "fpts": 1024, "fpts_decimal": 7,
             "fpts_against": 980, "fpts_against_decimal": 50}
        )
        self.assertEqual(record["points_for"], 1024.07)
        self.assertEqual(record["points_against"], 980.5)

    def test_missing_fields_default_to_zero(self):
        record = model.score_record({})
        self.assertEqual(
            record, {"wins": 0, "losses": 0, "ties": 0, "points_for": 0.0, "points_against": 0.0}
        )

    def test_none_decimal_does_not_crash(self):
        self.assertEqual(model.score_record({"fpts": 10, "fpts_decimal": None})["points_for"], 10.0)


class TeamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.players = load("players")
        cls.teams = model.build_teams(load("users"), load("rosters"), cls.players, load("league"))

    def test_every_roster_becomes_a_team(self):
        self.assertEqual(len(self.teams), 12)
        self.assertEqual([t["roster_id"] for t in self.teams], list(range(1, 13)))

    def test_bench_is_everyone_not_starting_taxi_or_ir(self):
        for team in self.teams:
            slotted = {p["id"] for p in team["starters"] + team["taxi"] + team["reserve"]}
            bench = {p["id"] for p in team["bench"]}
            self.assertEqual(bench & slotted, set(), f"{team['team_name']} double-counts a player")
            self.assertEqual(bench | slotted, set(team["player_ids"]))

    def test_no_player_appears_on_two_teams(self):
        seen: set[str] = set()
        for team in self.teams:
            ids = set(team["player_ids"])
            self.assertEqual(seen & ids, set())
            seen |= ids

    def test_faab_left_is_budget_minus_used(self):
        team = self.teams[0]
        self.assertEqual(team["faab_left"], 100 - team["faab_used"])

    def test_team_name_falls_back_to_display_name(self):
        users = [{"user_id": "u1", "display_name": "solo", "metadata": {}}]
        rosters = [{"roster_id": 1, "owner_id": "u1", "players": [], "settings": {}}]
        teams = model.build_teams(users, rosters, {}, {"settings": {}})
        self.assertEqual(teams[0]["team_name"], "solo")

    def test_orphaned_roster_still_renders(self):
        rosters = [{"roster_id": 3, "owner_id": None, "players": ["1001"], "settings": {}}]
        teams = model.build_teams([], rosters, {}, {"settings": {}})
        self.assertEqual(teams[0]["team_name"], "Roster 3")
        self.assertEqual(teams[0]["owner"], "unknown")


class PickInventoryTests(unittest.TestCase):
    def setUp(self):
        self.teams = [{"roster_id": i} for i in range(1, 5)]
        self.league = {"settings": {"draft_rounds": 2}, "status": "in_season"}

    def test_untraded_league_gives_everyone_their_own_picks(self):
        picks = model.build_pick_inventory(self.league, self.teams, [], 2026)
        for roster_id, held in picks.items():
            self.assertTrue(all(p["is_own"] for p in held))
            self.assertTrue(all(p["original_roster_id"] == roster_id for p in held))
        # 3 seasons x 2 rounds
        self.assertEqual(len(picks[1]), 6)

    def test_traded_pick_moves_to_new_owner(self):
        traded = [{"season": "2027", "round": 1, "roster_id": 1, "owner_id": 3}]
        picks = model.build_pick_inventory(self.league, self.teams, traded, 2026)
        gained = [p for p in picks[3] if not p["is_own"]]
        self.assertEqual(len(gained), 1)
        self.assertEqual(gained[0], {"season": 2027, "round": 1, "original_roster_id": 1, "is_own": False})
        self.assertNotIn((2027, 1), [(p["season"], p["round"]) for p in picks[1] if p["is_own"]])

    def test_total_pick_count_is_conserved_after_trades(self):
        traded = [
            {"season": "2027", "round": 1, "roster_id": 1, "owner_id": 3},
            {"season": "2028", "round": 2, "roster_id": 4, "owner_id": 2},
        ]
        picks = model.build_pick_inventory(self.league, self.teams, traded, 2026)
        self.assertEqual(sum(len(v) for v in picks.values()), 4 * 2 * 3)

    def test_current_season_excluded_once_draft_is_done(self):
        picks = model.build_pick_inventory(self.league, self.teams, [], 2026)
        self.assertEqual(sorted({p["season"] for p in picks[1]}), [2027, 2028, 2029])

    def test_current_season_included_before_the_draft(self):
        league = {"settings": {"draft_rounds": 2}, "status": "pre_draft"}
        picks = model.build_pick_inventory(league, self.teams, [], 2026)
        self.assertIn(2026, {p["season"] for p in picks[1]})

    def test_malformed_traded_pick_is_skipped_not_fatal(self):
        traded = [{"season": "not-a-year", "round": 1, "roster_id": 1, "owner_id": 2},
                  {"season": "2027"}]
        picks = model.build_pick_inventory(self.league, self.teams, traded, 2026)
        self.assertEqual(sum(len(v) for v in picks.values()), 4 * 2 * 3)


class FreeAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.players = load("players")
        cls.teams = model.build_teams(load("users"), load("rosters"), cls.players, load("league"))
        cls.free = model.build_free_agents(cls.players, cls.teams)

    def test_no_rostered_player_is_listed_as_available(self):
        rostered = {pid for t in self.teams for pid in t["player_ids"]}
        for rows in self.free.values():
            self.assertEqual({p["id"] for p in rows} & rostered, set())

    def test_respects_per_position_limits(self):
        for pos, rows in self.free.items():
            self.assertLessEqual(len(rows), model.FREE_AGENT_LIMITS[pos])

    def test_sorted_by_search_rank(self):
        for rows in self.free.values():
            self.assertEqual([p["rank"] for p in rows], sorted(p["rank"] for p in rows))

    def test_unranked_camp_bodies_are_excluded(self):
        names = {p["name"] for rows in self.free.values() for p in rows}
        self.assertFalse({n for n in names if n.startswith("Camp Body")})

    def test_team_defenses_are_listed_despite_having_no_search_rank(self):
        """Sleeper gives DEF entries no search_rank; the relevance filter must not eat them."""
        players = {
            "DET": {"first_name": "Detroit", "last_name": "Lions", "position": "DEF",
                    "fantasy_positions": ["DEF"], "team": "DET", "rosterable": True,
                    "search_rank": None},
            "SEA": {"first_name": "Seattle", "last_name": "Seahawks", "position": "DEF",
                    "fantasy_positions": ["DEF"], "team": "SEA", "rosterable": True,
                    "search_rank": None},
        }
        rosters = [{"roster_id": 1, "owner_id": "u1", "players": ["SEA"], "settings": {}}]
        teams = model.build_teams([], rosters, players, {"settings": {}})
        free = model.build_free_agents(players, teams)
        self.assertEqual([p["name"] for p in free["DEF"]], ["Detroit Lions"])

    def test_rankless_filter_still_excludes_unranked_skill_players(self):
        players = {"7": {"first_name": "Camp", "last_name": "Body", "position": "WR",
                         "fantasy_positions": ["WR"], "rosterable": True, "search_rank": None}}
        self.assertEqual(model.build_free_agents(players, [])["WR"], [])

    def test_retired_players_excluded_despite_a_strong_search_rank(self):
        """The bug this replaced: Sleeper reports Tom Brady as active with rank 74.

        search_rank is lifetime popularity, so a retired star outranks every real
        free agent. Availability has to come from the precomputed rosterable flag.
        """
        players = {"167": {"full_name": "Tom Brady", "position": "QB", "fantasy_positions": ["QB"],
                           "active": True, "status": "Active", "rosterable": False,
                           "search_rank": 74}}
        self.assertEqual(model.build_free_agents(players, []), {p: [] for p in model.FREE_AGENT_LIMITS})

    def test_player_held_on_a_roster_is_never_offered_as_a_free_agent(self):
        """Tyreek Hill is a real-life free agent but rostered here; he is not available."""
        players = {"3321": {"full_name": "Tyreek Hill", "position": "WR", "fantasy_positions": ["WR"],
                            "rosterable": False, "search_rank": 145}}
        rosters = [{"roster_id": 1, "owner_id": "u1", "players": ["3321"], "settings": {}}]
        teams = model.build_teams([], rosters, players, {"settings": {}})
        self.assertEqual(model.build_free_agents(players, teams)["WR"], [])
        # ...but his name still resolves, which is why the prune keeps him at all.
        self.assertEqual(teams[0]["bench"][0]["name"], "Tyreek Hill")


class CompactPlayerTests(unittest.TestCase):
    def test_builds_name_from_parts_when_full_name_missing(self):
        players = {"5": {"first_name": "Ja'Marr", "last_name": "Chase", "position": "WR"}}
        self.assertEqual(model.compact_player("5", players)["name"], "Ja'Marr Chase")

    def test_unknown_id_degrades_gracefully(self):
        player = model.compact_player("99999", {})
        self.assertEqual(player["name"], "Unknown player 99999")
        self.assertEqual(player["rank"], model.UNRANKED)

    def test_placeholder_zero_ids_are_dropped(self):
        self.assertEqual(model.compact_many(["0", "0"], {}), [])


class RealWorldShapeTests(unittest.TestCase):
    """Shapes the live API returns that the generated fixtures don't cover."""

    def test_team_defenses_keyed_by_abbreviation_not_a_numeric_id(self):
        players = {"KC": {"first_name": "Kansas City", "last_name": "Chiefs",
                          "position": "DEF", "fantasy_positions": ["DEF"],
                          "team": "KC", "active": True, "search_rank": 200}}
        rosters = [{"roster_id": 1, "owner_id": "u1", "players": ["KC"],
                    "starters": ["KC"], "settings": {}}]
        teams = model.build_teams([], rosters, players, {"settings": {}})
        self.assertEqual(teams[0]["starters"][0]["name"], "Kansas City Chiefs")
        self.assertIn("KC Kansas City Chiefs", render._roster_block(teams[0])[1])

    def test_empty_starter_slots_are_dropped(self):
        rosters = [{"roster_id": 1, "owner_id": "u1", "players": ["1", "0"],
                    "starters": ["1", "0", "0"], "settings": {}}]
        teams = model.build_teams([], rosters, {}, {"settings": {}})
        self.assertEqual(len(teams[0]["starters"]), 1)
        self.assertEqual(teams[0]["player_ids"], ["1"])

    def test_null_collections_do_not_crash(self):
        rosters = [{"roster_id": 1, "owner_id": "u1", "players": None,
                    "starters": None, "taxi": None, "reserve": None, "settings": None}]
        teams = model.build_teams([], rosters, {}, {"settings": {}})
        self.assertEqual(teams[0]["player_ids"], [])
        self.assertIsNone(teams[0]["faab_left"])

    def test_user_metadata_may_be_null(self):
        users = [{"user_id": "u1", "display_name": "sam", "metadata": None}]
        rosters = [{"roster_id": 1, "owner_id": "u1", "players": [], "settings": {}}]
        self.assertEqual(model.build_teams(users, rosters, {}, {"settings": {}})[0]["team_name"], "sam")

    def test_player_missing_from_the_map_degrades_instead_of_crashing(self):
        rosters = [{"roster_id": 1, "owner_id": "u1", "players": ["77"],
                    "starters": ["77"], "settings": {}}]
        teams = model.build_teams([], rosters, {}, {"settings": {}})
        self.assertEqual(teams[0]["starters"][0]["name"], "Unknown player 77")


class RolloverTests(unittest.TestCase):
    """A dynasty league gets a new league_id every season; the build has to follow it."""

    class FakeClient:
        def __init__(self, leagues, user_leagues):
            self.leagues = leagues
            self.user_leagues = user_leagues

        def get(self, path, **query):
            if path == "/state/nfl":
                return {"season": "2027", "league_season": "2027"}
            if path.startswith("/user/"):
                return self.user_leagues
            key = path.rsplit("/", 1)[-1]
            return self.leagues.get(key)

    def test_follows_previous_league_id_into_the_new_season(self):
        old = {"league_id": "OLD", "name": "The Dynasty League", "season": "2026"}
        new = {"league_id": "NEW", "name": "The Dynasty League", "season": "2027",
               "previous_league_id": "OLD"}
        client = self.FakeClient({"OLD": old, "NEW": new}, [new])
        self.assertEqual(resolve_current_league(client, "OLD", "u1")["league_id"], "NEW")

    def test_walks_more_than_one_season_of_lineage(self):
        a = {"league_id": "A", "name": "L", "season": "2025"}
        b = {"league_id": "B", "name": "L", "season": "2026", "previous_league_id": "A"}
        c = {"league_id": "C", "name": "L", "season": "2027", "previous_league_id": "B"}
        client = self.FakeClient({"A": a, "B": b, "C": c}, [c])
        self.assertEqual(resolve_current_league(client, "A", "u1")["league_id"], "C")

    def test_unrelated_league_is_not_followed(self):
        old = {"league_id": "OLD", "name": "Mine", "season": "2026"}
        other = {"league_id": "OTHER", "name": "Someone else's", "season": "2027"}
        client = self.FakeClient({"OLD": old, "OTHER": other}, [other])
        self.assertEqual(resolve_current_league(client, "OLD", "u1")["league_id"], "OLD")

    def test_lineage_cycle_terminates_and_falls_back_to_root(self):
        root = {"league_id": "ROOT", "name": "L", "season": "2026"}
        a = {"league_id": "A", "name": "L", "season": "2027", "previous_league_id": "B"}
        b = {"league_id": "B", "name": "L", "season": "2026", "previous_league_id": "A"}
        client = self.FakeClient({"ROOT": root, "A": a, "B": b}, [a])
        self.assertEqual(resolve_current_league(client, "ROOT", "u1")["league_id"], "ROOT")

    def test_missing_root_league_is_a_hard_failure(self):
        client = self.FakeClient({}, [])
        with self.assertRaises(SleeperError):
            resolve_current_league(client, "MISSING", "u1")


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        client = FixtureClient(FIXTURES)
        cls.snapshot = build.build_snapshot(client, "1359238964875132928", "1260412048202813440")
        cls.markdown = render.render_markdown(cls.snapshot)

    def test_brief_fits_in_a_single_fetch(self):
        self.assertLess(len(self.markdown.encode("utf-8")), build.FAIL_BYTES)

    def test_every_section_is_present(self):
        for heading in ("# The Dynasty League", "## Where the season stands",
                        "## How this league works", "## Standings", "## Rosters",
                        "## Future rookie draft picks", "## Recent moves",
                        "## Available free agents", "## Reference"):
            self.assertIn(heading, self.markdown)

    def test_superflex_is_called_out_when_present(self):
        snapshot = dict(self.snapshot)
        snapshot["league"] = dict(snapshot["league"])
        snapshot["league"]["starting_lineup"] = ["QB", "RB", "WR", "SUPER_FLEX"]
        self.assertIn("Superflex league", render.render_markdown(snapshot))

    def test_superflex_note_absent_in_a_standard_league(self):
        snapshot = dict(self.snapshot)
        snapshot["league"] = dict(snapshot["league"])
        snapshot["league"]["starting_lineup"] = ["QB", "RB", "WR", "FLEX"]
        self.assertNotIn("Superflex league", render.render_markdown(snapshot))

    def test_live_section_warns_against_the_5mb_player_map(self):
        self.assertIn("Do not fetch", self.markdown)
        self.assertIn("players/nfl", self.markdown)

    def test_player_ids_are_present_so_live_responses_can_be_decoded(self):
        team = self.snapshot["teams"][0]
        player = team["starters"][0]
        self.assertIn(f"{player['id']} {player['name']}", self.markdown)

    def test_standings_ordered_by_wins(self):
        wins = [t["record"]["wins"] for t in self.snapshot["standings"]]
        self.assertEqual(wins, sorted(wins, reverse=True))

    def test_json_output_is_valid_and_round_trips(self):
        self.assertEqual(json.loads(render.render_json(self.snapshot))["league"]["season"], "2026")

    def test_index_html_embeds_the_shareable_url(self):
        html = render.render_index(self.snapshot, "https://example.github.io/repo")
        self.assertIn("https://example.github.io/repo/league.md", html)

    def test_write_site_emits_all_files_and_passes_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = build.write_site(self.snapshot, Path(tmp), "https://example.com")
            self.assertEqual(code, 0)
            for name in ("league.md", "league.json", "index.html", ".nojekyll"):
                self.assertTrue((Path(tmp) / name).exists(), f"{name} missing")

    def test_missing_rosters_is_a_hard_failure(self):
        class Empty(FixtureClient):
            def get(self, path, **query):
                if path.endswith("/rosters"):
                    return []
                return super().get(path, **query)

        with self.assertRaises(SleeperError):
            build.build_snapshot(Empty(FIXTURES), "1359238964875132928", "1260412048202813440")


class SeasonStateTests(unittest.TestCase):
    """The preseason case is the one that misleads a reader left unexplained."""

    def test_preseason_says_the_zeros_are_expected(self):
        plain = model.season_state({"season": "2026"}, {"season_type": "pre", "week": 1})["plain"]
        self.assertIn("preseason", plain)
        self.assertIn("0-0", plain)
        self.assertIn("not missing data", plain)

    def test_preseason_is_not_marked_as_started(self):
        self.assertFalse(
            model.season_state({"season": "2026"}, {"season_type": "pre", "week": 1})[
                "regular_season_started"
            ]
        )

    def test_regular_season_reports_the_week(self):
        state = model.season_state({"season": "2026"}, {"season_type": "regular", "week": 7})
        self.assertTrue(state["regular_season_started"])
        self.assertIn("week 7", state["plain"])

    def test_unknown_season_type_still_produces_a_sentence(self):
        self.assertTrue(model.season_state({"season": "2026"}, {})["plain"])


class RosterProfileTests(unittest.TestCase):
    def setUp(self):
        self.team = {
            "starters": [{"pos": "QB", "age": 24}, {"pos": "RB", "age": 30}],
            "bench": [{"pos": "WR", "age": 22}],
            "taxi": [{"pos": "WR", "age": None}],
            "reserve": [],
        }

    def test_counts_every_slot_not_just_starters(self):
        profile = model.roster_profile(self.team)
        self.assertEqual(profile["size"], 4)
        self.assertEqual(profile["by_position"], {"QB": 1, "RB": 1, "WR": 2})

    def test_average_age_ignores_missing_ages(self):
        self.assertEqual(model.roster_profile(self.team)["average_age"], 25.3)

    def test_counts_players_25_and_under(self):
        self.assertEqual(model.roster_profile(self.team)["age_25_and_under"], 2)

    def test_empty_roster_does_not_divide_by_zero(self):
        empty = {"starters": [], "bench": [], "taxi": [], "reserve": []}
        self.assertIsNone(model.roster_profile(empty)["average_age"])


class RosterGapTests(unittest.TestCase):
    def _team(self, counts):
        return {"profile": {"by_position": counts}}

    def test_missing_position_is_reported_as_unfillable(self):
        gaps = render.roster_gaps(self._team({"QB": 2}), ["QB", "K"])
        self.assertIn("cannot fill the K slot", " ".join(gaps))

    def test_exactly_enough_is_reported_as_no_backup(self):
        gaps = render.roster_gaps(self._team({"QB": 1}), ["QB"])
        self.assertIn("no backup", " ".join(gaps))

    def test_depth_produces_no_gap(self):
        self.assertEqual(render.roster_gaps(self._team({"QB": 3}), ["QB"]), [])

    def test_flex_slots_are_not_treated_as_a_required_position(self):
        self.assertEqual(render.lineup_requirements(["QB", "FLEX", "SUPER_FLEX"]), {"QB": 1})


class TransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.players = load("players")
        cls.teams = model.build_teams(load("users"), load("rosters"), cls.players, load("league"))
        cls.rows = model.build_transactions(load("transactions"), cls.players, cls.teams)

    def test_failed_transactions_are_excluded(self):
        self.assertEqual(len(self.rows), 3)

    def test_newest_first(self):
        stamps = [r["when"] for r in self.rows]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_players_are_named_not_left_as_ids(self):
        summary = self.rows[0]["summary"]
        self.assertIn("added", summary)
        self.assertNotRegex(summary, r"\b\d{4,}\b")

    def test_teams_are_named_not_left_as_roster_ids(self):
        names = {t["team_name"] for t in self.teams}
        self.assertTrue(any(n in self.rows[0]["summary"] for n in names))

    def test_trade_reports_pick_and_faab_movement(self):
        trade = next(r for r in self.rows if r["type"] == "trade")
        self.assertIn("2027 round 1", trade["summary"])
        self.assertIn("$12", trade["summary"])

    def test_no_transactions_is_not_an_error(self):
        self.assertEqual(model.build_transactions(None, self.players, self.teams), [])


class MatchupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.teams = model.build_teams(load("users"), load("rosters"), load("players"), load("league"))
        cls.matchups = model.build_matchups(load("matchups"), cls.teams)

    def test_pairs_teams_by_matchup_id(self):
        self.assertTrue(all(len(m["teams"]) == 2 for m in self.matchups))

    def test_teams_are_named(self):
        names = {t["team_name"] for t in self.teams}
        self.assertIn(self.matchups[0]["teams"][0]["team_name"], names)

    def test_scoreless_week_is_flagged(self):
        self.assertFalse(any(m["any_points"] for m in self.matchups))

    def test_missing_matchups_do_not_crash(self):
        self.assertEqual(model.build_matchups(None, self.teams), [])


class PriorSeasonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prior = model.prior_season(
            load("league"), load("rosters"), load("users"), load("winners_bracket")
        )

    def test_champion_comes_from_the_placement_game(self):
        self.assertEqual(self.prior["champion_roster_id"], 2)
        self.assertTrue(self.prior["champion"])

    def test_standings_are_ordered_by_wins(self):
        wins = [r["record"]["wins"] for r in self.prior["standings"]]
        self.assertEqual(wins, sorted(wins, reverse=True))

    def test_absent_prior_season_is_none_not_an_error(self):
        self.assertIsNone(model.prior_season(None, None, None, None))

    def test_bracket_without_a_final_leaves_champion_unset(self):
        prior = model.prior_season(load("league"), load("rosters"), load("users"), [{"m": 1, "w": 3}])
        self.assertIsNone(prior["champion_roster_id"])


class ViewerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = build.build_snapshot(
            FixtureClient(FIXTURES), "1359238964875132928", "1260412048202813440"
        )

    def test_resolves_by_sleeper_username(self):
        owner = self.snapshot["teams"][0]["owner"]
        self.assertEqual(build.viewer_for(self.snapshot, owner)["roster_id"],
                         self.snapshot["teams"][0]["roster_id"])

    def test_resolves_by_team_name_and_roster_id(self):
        team = self.snapshot["teams"][1]
        self.assertEqual(build.viewer_for(self.snapshot, team["team_name"])["roster_id"], team["roster_id"])
        self.assertEqual(build.viewer_for(self.snapshot, str(team["roster_id"]))["roster_id"], team["roster_id"])

    def test_lookup_is_case_insensitive(self):
        owner = self.snapshot["teams"][0]["owner"]
        self.assertIsNotNone(build.viewer_for(self.snapshot, owner.upper()))

    def test_unknown_name_returns_none(self):
        self.assertIsNone(build.viewer_for(self.snapshot, "nobody-by-that-name"))

    def test_viewer_carries_pick_capital_and_gaps(self):
        viewer = build.viewer_for(self.snapshot, self.snapshot["teams"][0]["owner"])
        self.assertIn("total", viewer["pick_summary"])
        self.assertIsInstance(viewer["gaps"], list)

    def test_personalised_brief_names_the_manager(self):
        viewer = build.viewer_for(self.snapshot, self.snapshot["teams"][0]["owner"])
        markdown = render.render_markdown(self.snapshot, viewer=viewer)
        self.assertIn("## You are talking to", markdown)
        self.assertIn(viewer["team_name"], markdown.split("## Where the season stands")[0])

    def test_neutral_brief_has_no_viewer_block(self):
        self.assertNotIn("## You are talking to", render.render_markdown(self.snapshot))


class PickSummaryTests(unittest.TestCase):
    def test_counts_totals_firsts_and_seasons(self):
        held = [
            {"season": 2027, "round": 1}, {"season": 2027, "round": 2},
            {"season": 2028, "round": 1},
        ]
        summary = model.pick_summary(held)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["firsts"], 2)
        self.assertEqual(summary["by_season"], {2027: 2, 2028: 1})

    def test_no_picks_is_zeroes_not_an_error(self):
        self.assertEqual(model.pick_summary([])["total"], 0)


class MarkdownToHtmlTests(unittest.TestCase):
    def test_tags_are_balanced_for_the_real_brief(self):
        snapshot = build.build_snapshot(
            FixtureClient(FIXTURES), "1359238964875132928", "1260412048202813440"
        )
        html = render.markdown_to_html(render.render_markdown(snapshot))
        for tag in ("ul", "li", "table", "blockquote", "p"):
            self.assertEqual(html.count(f"<{tag}>"), html.count(f"</{tag}>"), tag)

    def test_nested_lists_stay_inside_their_parent_item(self):
        html = render.markdown_to_html("- outer\n  - inner\n")
        self.assertIn("<li>outer\n<ul>", html)

    def test_tables_become_real_tables(self):
        html = render.markdown_to_html("| A | B |\n|---|---|\n| 1 | 2 |\n")
        self.assertIn("<th>A</th>", html)
        self.assertIn("<td>1</td>", html)

    def test_headings_map_to_levels(self):
        html = render.markdown_to_html("# One\n\n## Two\n\n### Three\n")
        self.assertIn("<h1>One</h1>", html)
        self.assertIn("<h3>Three</h3>", html)

    def test_html_in_content_is_escaped(self):
        self.assertIn("&lt;script&gt;", render.markdown_to_html("- <script>alert(1)</script>\n"))

    def test_inline_code_and_bold_survive(self):
        html = render.markdown_to_html("Use `roster_id` and **weigh** it.\n")
        self.assertIn("<code>roster_id</code>", html)
        self.assertIn("<strong>weigh</strong>", html)

    def test_code_fence_is_not_treated_as_markdown(self):
        html = render.markdown_to_html("```\na=1, b=2\n```\n")
        self.assertIn("<pre><code>a=1, b=2</code></pre>", html)


class ServedPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = build.build_snapshot(
            FixtureClient(FIXTURES), "1359238964875132928", "1260412048202813440"
        )
        cls.markdown = render.render_markdown(cls.snapshot)
        cls.html = render.render_html(cls.snapshot, cls.markdown)

    def test_page_is_html_with_a_title(self):
        self.assertTrue(self.html.startswith("<!doctype html>"))
        self.assertIn("<title>", self.html)

    def test_copy_button_is_present_for_when_fetching_fails(self):
        self.assertIn("Copy entire brief", self.html)
        self.assertIn("can't read this link", self.html)

    def test_brief_is_not_duplicated_into_the_page(self):
        """The markdown is fetched on click; embedding it would double the page."""
        self.assertNotIn("Read this first.** This is a live brief", self.html)

    def test_league_content_actually_reaches_the_page(self):
        self.assertIn(str(self.snapshot["teams"][0]["roster_id"]), self.html)
        self.assertIn(self.snapshot["teams"][0]["team_name"], self.html)

    def test_personalised_page_titles_the_team(self):
        viewer = build.viewer_for(self.snapshot, self.snapshot["teams"][0]["owner"])
        html = render.render_html(self.snapshot, self.markdown, viewer=viewer)
        self.assertIn(viewer["team_name"], html.split("</title>")[0])


class PrunePlayerTests(unittest.TestCase):
    """The rule that fixes a free agent list which was 68% retired players."""

    def setUp(self):
        self.now = 1_800_000_000.0
        self.fresh = int((self.now - 10 * 86400) * 1000)
        self.stale = int((self.now - 900 * 86400) * 1000)

    def test_active_starter_is_rosterable(self):
        raw = {"position": "QB", "team": "KC", "news_updated": self.fresh}
        self.assertTrue(prune_players.is_rosterable(raw, self.now))

    def test_retired_player_with_no_team_is_not_rosterable(self):
        raw = {"position": "QB", "team": None, "news_updated": self.fresh, "active": True}
        self.assertFalse(prune_players.is_rosterable(raw, self.now))

    def test_player_with_only_stale_news_is_not_rosterable(self):
        raw = {"position": "QB", "team": "PIT", "news_updated": self.stale}
        self.assertFalse(prune_players.is_rosterable(raw, self.now))

    def test_team_defense_skips_the_freshness_test(self):
        """DEF records carry no news_updated; applying it deletes all 32."""
        self.assertTrue(prune_players.is_rosterable({"position": "DEF", "team": "SEA"}, self.now))

    def test_non_fantasy_position_is_dropped(self):
        raw = {"position": "OL", "team": "KC", "news_updated": self.fresh}
        self.assertFalse(prune_players.is_rosterable(raw, self.now))

    def test_rostered_player_is_kept_even_when_not_rosterable(self):
        """Dynasty managers hold real-life free agents; their names must resolve."""
        players = {"3321": {"position": "WR", "team": None, "full_name": "Tyreek Hill"}}
        pruned = prune_players.prune(players, {"3321"}, now=self.now)
        self.assertIn("3321", pruned)
        self.assertFalse(pruned["3321"]["rosterable"])

    def test_unrostered_retired_player_is_dropped_entirely(self):
        players = {"167": {"position": "QB", "team": None, "full_name": "Tom Brady",
                           "search_rank": 74, "active": True}}
        self.assertEqual(prune_players.prune(players, set(), now=self.now), {})

    def test_pruned_records_keep_the_field_names_the_model_reads(self):
        players = {"1": {"position": "RB", "team": "SF", "news_updated": self.fresh,
                         "full_name": "A B", "search_rank": 12, "fantasy_positions": ["RB"]}}
        pruned = prune_players.prune(players, set(), now=self.now)
        compact = model.compact_player("1", pruned)
        self.assertEqual(compact["name"], "A B")
        self.assertEqual(compact["rank"], 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
