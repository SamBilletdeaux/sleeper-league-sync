"""Minimal read-only client for the Sleeper API.

Sleeper's API needs no credentials and is read-only. Only the Python standard
library is used so the workflow needs no dependency install step.

Rate limit is roughly 1000 requests/minute, and a full build makes about eight
requests, so throttling is not a concern. The one endpoint that does need care
is ``/players/nfl``: it returns ~5 MB and Sleeper asks that it be called at most
once per day, so it is cached on disk and the workflow persists that cache.
"""

from __future__ import annotations

import json
import os
import re
import time
from concurrent import futures
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API_BASE = "https://api.sleeper.app/v1"
USER_AGENT = "sleeper-league-sync (personal dynasty league brief)"
PLAYER_CACHE_MAX_AGE_SECONDS = 20 * 60 * 60


class SleeperError(RuntimeError):
    """Raised when the Sleeper API cannot be reached or returns garbage."""


class SleeperClient:
    """Fetches from the live Sleeper API."""

    def __init__(self, cache_dir: Path, *, timeout: int = 30, retries: int = 3) -> None:
        self.cache_dir = Path(cache_dir)
        self.timeout = timeout
        self.retries = retries

    def get(self, path: str, **query: Any) -> Any:
        url = f"{API_BASE}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                # 404 means "no such league/week"; retrying will not help.
                if exc.code == 404:
                    return None
                last_error = exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < self.retries - 1:
                time.sleep(1.5 * (attempt + 1))
        raise SleeperError(f"GET {url} failed after {self.retries} attempts: {last_error}")

    def get_many(self, paths: dict[str, str]) -> dict[str, Any]:
        """Fetch several endpoints concurrently, keyed by caller-chosen name.

        A rendered page needs about a dozen endpoints. Sequentially that is
        ~450ms of mostly-waiting; concurrently it is closer to one round trip.
        A failure on any one path raises, matching ``get``.
        """
        if not paths:
            return {}
        results: dict[str, Any] = {}
        with futures.ThreadPoolExecutor(max_workers=min(8, len(paths))) as pool:
            pending = {pool.submit(self.get, path): name for name, path in paths.items()}
            for future in futures.as_completed(pending):
                results[pending[future]] = future.result()
        return results

    def players(self) -> dict[str, dict[str, Any]]:
        """The full NFL player map, cached on disk between runs."""
        cache = self.cache_dir / "players_nfl.json"
        if cache.exists():
            age = time.time() - cache.stat().st_mtime
            if age < PLAYER_CACHE_MAX_AGE_SECONDS:
                try:
                    with cache.open(encoding="utf-8") as handle:
                        cached = json.load(handle)
                    if cached:
                        print(f"players/nfl: cache hit ({age / 3600:.1f}h old)")
                        return cached
                except (json.JSONDecodeError, OSError):
                    pass  # Corrupt cache; fall through and refetch.

        print("players/nfl: fetching ~5 MB player map")
        data = self.get("/players/nfl")
        if not isinstance(data, dict) or not data:
            raise SleeperError("players/nfl returned no data")
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle)
        tmp.replace(cache)
        return data


class FixtureClient:
    """Serves recorded responses from disk so the build can run without network.

    Paths are normalised to stable fixture names, e.g. ``/league/12345/rosters``
    resolves to ``rosters.json`` regardless of which league id is in play.
    """

    _ROUTES: list[tuple[str, str]] = [
        (r"^/state/nfl$", "state_nfl"),
        (r"^/players/nfl/trending/add$", "trending_add"),
        (r"^/players/nfl/trending/drop$", "trending_drop"),
        (r"^/players/nfl$", "players"),
        (r"^/user/[^/]+/leagues/nfl/[^/]+$", "user_leagues"),
        (r"^/league/[^/]+/users$", "users"),
        (r"^/league/[^/]+/rosters$", "rosters"),
        (r"^/league/[^/]+/traded_picks$", "traded_picks"),
        (r"^/league/[^/]+/matchups/[^/]+$", "matchups"),
        (r"^/league/[^/]+/transactions/[^/]+$", "transactions"),
        (r"^/league/[^/]+/winners_bracket$", "winners_bracket"),
        (r"^/league/[^/]+$", "league"),
    ]

    def __init__(self, fixture_dir: Path) -> None:
        self.fixture_dir = Path(fixture_dir)

    def _name_for(self, path: str) -> str:
        path = path.split("?", 1)[0]
        for pattern, name in self._ROUTES:
            if re.match(pattern, path):
                return name
        raise SleeperError(f"no fixture route for {path}")

    def get(self, path: str, **query: Any) -> Any:
        target = self.fixture_dir / f"{self._name_for(path)}.json"
        if not target.exists():
            raise SleeperError(f"missing fixture {target}")
        with target.open(encoding="utf-8") as handle:
            return json.load(handle)

    def get_many(self, paths: dict[str, str]) -> dict[str, Any]:
        return {name: self.get(path) for name, path in paths.items()}

    def players(self) -> dict[str, dict[str, Any]]:
        return self.get("/players/nfl")


def resolve_current_league(client: Any, root_league_id: str, user_id: str) -> dict[str, Any]:
    """Follow a dynasty league forward into the current Sleeper season.

    Sleeper mints a new league id every season and links it to the prior one via
    ``previous_league_id``. A hard-coded id therefore goes stale each summer. This
    looks at the user's leagues for the current season and picks the one whose
    ancestry traces back to the configured root. Lineage is the source of truth;
    the league name is only used to try the likeliest candidate first.
    """
    root = client.get(f"/league/{root_league_id}")
    if not root:
        raise SleeperError(f"root league {root_league_id} not found")

    state = client.get("/state/nfl") or {}
    seasons: list[str] = []
    for value in (state.get("league_season"), state.get("season"), root.get("season")):
        if value and str(value) not in seasons:
            seasons.append(str(value))

    # Fast path: a league whose season is the current one and which has not been
    # marked complete is already current, so skip the user-leagues lookup. This
    # is the case on every request except the days right after a rollover.
    if seasons and str(root.get("season")) == seasons[0] and root.get("status") != "complete":
        return root

    for season in seasons:
        leagues = client.get(f"/user/{user_id}/leagues/nfl/{season}") or []
        if not isinstance(leagues, list):
            continue
        for league in leagues:
            if str(league.get("league_id")) == str(root_league_id):
                return league
        likely = [l for l in leagues if l.get("name") == root.get("name")]
        rest = [l for l in leagues if l not in likely]
        for league in likely + rest:
            if _descends_from(client, league, str(root_league_id)):
                print(f"season rollover: following {root_league_id} -> {league['league_id']}")
                return league
    return root


def _descends_from(client: Any, league: dict[str, Any], ancestor_id: str, max_depth: int = 8) -> bool:
    current = league
    seen: set[str] = set()
    for _ in range(max_depth):
        current_id = str(current.get("league_id") or "")
        if current_id == ancestor_id:
            return True
        if current_id in seen:
            return False
        seen.add(current_id)
        previous = str(current.get("previous_league_id") or "")
        if not previous:
            return False
        if previous == ancestor_id:
            return True
        try:
            parent = client.get(f"/league/{previous}")
        except SleeperError:
            return False
        if not parent:
            return False
        current = parent
    return False


def make_client(fixtures: str | None, cache_dir: Path) -> Any:
    if fixtures:
        return FixtureClient(Path(fixtures))
    return SleeperClient(cache_dir)


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()
