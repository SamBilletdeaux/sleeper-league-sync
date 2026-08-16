"""Vercel handler: renders the league brief live, per request.

Routes
    /                    the full league brief, as HTML
    /me/<username>       the same brief addressed to one manager
    /league.json         the same data, structured
    /health              liveness probe

Any route also accepts ``?format=md`` (or ``Accept: text/markdown``) for the
markdown source, and ``?format=json``.

Freshness lives here rather than in the reader. The previous design published a
snapshot every six hours and asked the assistant to fetch the Sleeper API itself
for anything newer — the least reliable capability a consumer assistant has, and
it carried the whole freshness promise. Rendering on request means the only
thing an assistant has to do is read one page.

The ~16 MB player map is far too large to fetch per request, so it is pruned to
the ~1,000 players this league could plausibly reference and committed as
``data/players.json`` by the daily job. Only the volatile endpoints (~18 KB) are
fetched here.
"""

from __future__ import annotations

import json
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build  # noqa: E402
import render  # noqa: E402
from sleeper import SleeperError, env, make_client  # noqa: E402

LEAGUE_ID = env("SLEEPER_LEAGUE_ID") or build.DEFAULT_LEAGUE_ID
USER_ID = env("SLEEPER_USER_ID") or build.DEFAULT_USER_ID

# Long enough that a burst of leaguemates opening the link costs Sleeper one
# fetch, short enough that "up to date" stays true. stale-while-revalidate keeps
# the page answering from cache while a rebuild runs, and covers a Sleeper blip.
CACHE_CONTROL = "public, s-maxage=60, stale-while-revalidate=300"

_PLAYERS: dict | None = None


def players() -> dict:
    """Baked player dictionary, read once per warm instance."""
    global _PLAYERS
    if _PLAYERS is None:
        with (ROOT / "data" / "players.json").open(encoding="utf-8") as handle:
            _PLAYERS = json.load(handle)
    return _PLAYERS


def request_path(handler: BaseHTTPRequestHandler) -> str:
    """The path the visitor asked for, not the one the rewrite landed on.

    vercel.json rewrites everything to /api/index; depending on how the request
    arrives, the original may only survive in a header.
    """
    for header in ("x-vercel-original-path", "x-rewrite-url", "x-original-url"):
        value = handler.headers.get(header)
        if value:
            return urlparse(value).path
    path = urlparse(handler.path).path
    if path.rstrip("/") in ("/api/index", "/api"):
        return "/"
    return path


def wants(handler: BaseHTTPRequestHandler, path: str) -> str:
    query = parse_qs(urlparse(handler.path).query)
    explicit = (query.get("format") or [""])[0].lower()
    if explicit in ("md", "markdown", "text"):
        return "md"
    if explicit == "json" or path == "/league.json":
        return "json"
    accept = (handler.headers.get("accept") or "").lower()
    # Only honour an explicit markdown preference; browsers and most fetchers
    # send */* and must get HTML.
    if "text/markdown" in accept and "text/html" not in accept:
        return "md"
    return "html"


def viewer_name(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0] in ("me", "team"):
        return unquote(parts[1])
    return ""


class handler(BaseHTTPRequestHandler):
    # Set per-request so _send knows whether to write a body.
    _body = True

    def do_HEAD(self) -> None:  # noqa: N802 - required name
        """Answer HEAD the same way as GET, minus the body.

        BaseHTTPRequestHandler replies 501 to any verb it has no method for, and
        some fetchers probe with HEAD before deciding whether to read a URL — a
        501 there reads as "this page is not fetchable".
        """
        self._body = False
        try:
            self.do_GET()
        finally:
            self._body = True

    def do_GET(self) -> None:  # noqa: N802 - required name
        path = request_path(self)

        if path.rstrip("/") == "/health":
            return self._send(200, "application/json", '{"ok":true}\n', cache=False)

        try:
            snapshot = build.build_snapshot(
                make_client(None, ROOT / ".cache"), LEAGUE_ID, USER_ID, players=players()
            )
        except SleeperError as exc:
            return self._fail(f"Sleeper API unavailable: {exc}")
        except Exception as exc:  # noqa: BLE001 - a broken page must still explain itself
            traceback.print_exc()
            return self._fail(f"{type(exc).__name__}: {exc}")

        wanted_name = viewer_name(path)
        viewer = build.viewer_for(snapshot, wanted_name) if wanted_name else None
        fmt = wants(self, path)

        if fmt == "json":
            payload = dict(snapshot)
            if viewer:
                payload["viewer"] = {
                    "roster_id": viewer["roster_id"],
                    "team_name": viewer["team_name"],
                    "owner": viewer["owner"],
                }
            return self._send(200, "application/json; charset=utf-8", render.render_json(payload))

        markdown = render.render_markdown(snapshot, viewer=viewer)
        if wanted_name and not viewer:
            known = ", ".join(sorted(t["owner"] for t in snapshot["teams"]))
            markdown = (
                f"> **Note.** No manager named `{wanted_name}` is in this league, so this is the "
                f"neutral league-wide brief. Managers in this league: {known}.\n\n" + markdown
            )

        if fmt == "md":
            return self._send(200, "text/markdown; charset=utf-8", markdown)
        return self._send(200, "text/html; charset=utf-8", render.render_html(snapshot, markdown, viewer=viewer))

    def _send(self, status: int, content_type: str, body: str, *, cache: bool = True) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", CACHE_CONTROL if cache else "no-store")
        # Lets a browser-side tool or another page read this without a proxy.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self._body:
            self.wfile.write(raw)

    def _fail(self, detail: str) -> None:
        """Explain the failure in the response body.

        A blank 500 reads to an assistant as "the page has no content", which is
        indistinguishable from a page that legitimately says nothing.
        """
        body = (
            "<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<title>League brief unavailable</title></head><body>"
            "<h1>The league brief could not be built</h1>"
            f"<p>{render._esc(detail)}</p>"
            "<p>This page is rendered live from the public Sleeper API, so this usually means "
            "Sleeper is briefly unavailable. Try again in a minute.</p>"
            "</body></html>\n"
        )
        self._send(503, "text/html; charset=utf-8", body, cache=False)
