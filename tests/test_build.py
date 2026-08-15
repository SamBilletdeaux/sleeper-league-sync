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
                    "fantasy_positions": ["DEF"], "team": "DET", "active": True,
                    "search_rank": None},
            "SEA": {"first_name": "Seattle", "last_name": "Seahawks", "position": "DEF",
                    "fantasy_positions": ["DEF"], "team": "SEA", "active": True,
                    "search_rank": None},
        }
        rosters = [{"roster_id": 1, "owner_id": "u1", "players": ["SEA"], "settings": {}}]
        teams = model.build_teams([], rosters, players, {"settings": {}})
        free = model.build_free_agents(players, teams)
        self.assertEqual([p["name"] for p in free["DEF"]], ["Detroit Lions"])

    def test_rankless_filter_still_excludes_unranked_skill_players(self):
        players = {"7": {"first_name": "Camp", "last_name": "Body", "position": "WR",
                         "fantasy_positions": ["WR"], "active": True, "search_rank": None}}
        self.assertEqual(model.build_free_agents(players, [])["WR"], [])

    def test_inactive_players_excluded(self):
        players = {"9": {"full_name": "Retired Guy", "position": "RB",
                         "fantasy_positions": ["RB"], "active": False, "search_rank": 1}}
        self.assertEqual(model.build_free_agents(players, []), {p: [] for p in model.FREE_AGENT_LIMITS})


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
        for heading in ("# The Dynasty League", "## League settings", "## Standings",
                        "## Rosters", "## Future rookie draft picks",
                        "## Top available free agents", "## Live data"):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
