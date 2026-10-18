"""A local stand-in for every external service, driven by a scripted multi-day scenario.

TEST DATA ONLY. Everything here is synthetic and exists so the full pipeline can be
exercised end to end without network access. None of it is written to data/.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

BASE_DAY = date(2026, 3, 1)


def day_str(k: int) -> str:
    return (BASE_DAY + timedelta(days=k)).isoformat()


class Scenario:
    """Game stats as a function of the simulated day index k (0 = first run)."""

    def __init__(self) -> None:
        self.k = 0
        self.discord_payloads: list[dict] = []
        self.llm_calls = 0
        self.requests: list[str] = []
        self.fail_paths: set[str] = set()  # paths that should return 503

    # universe id -> static info
    GAMES = {
        1001: {"name": "Mega Obby", "creator": ("Group", 501, "Obby Studio"), "genre_l1": "obby_and_platformer",
               "created": "2024-01-01T00:00:00Z", "place": 2004},
        1002: {"name": "Pet Tycoon", "creator": ("Group", 502, "Tycoon Bros"), "genre_l1": "simulation",
               "created": "2025-06-01T00:00:00Z", "place": 2001},
        1003: {"name": "Battle Arena", "creator": ("User", 601, "SoloDev"), "genre_l1": "action",
               "created": "2023-01-01T00:00:00Z", "place": 2003},
        1004: {"name": "Fresh Hit", "creator": ("Group", 502, "Tycoon Bros"), "genre_l1": "simulation",
               "created": f"{day_str(-10)}T12:00:00.1234567Z", "place": 2006},
        1005: {"name": "Roleplay Town", "creator": ("Group", 503, "RP Inc"), "genre_l1": "roleplay_and_avatar_sim",
               "created": "2022-05-01T00:00:00Z", "place": 2005},
        1006: {"name": "Tiny Test Place", "creator": ("Group", 501, "Obby Studio"), "genre_l1": "",
               "created": "2021-01-01T00:00:00Z", "place": 2007},
        1007: {"name": "Ad Game [*sponsored*]", "creator": ("Group", 504, "Ad Co"), "genre_l1": "shooter",
               "created": "2024-02-02T00:00:00Z", "place": 2008},
        1008: {"name": "Secret Project", "creator": ("Group", 502, "Tycoon Bros"), "genre_l1": "puzzle",
               "created": f"{day_str(2)}T09:00:00Z", "place": 2009},
    }

    def alive(self, uid: int) -> bool:
        return {1004: self.k >= 3, 1008: self.k >= 2}.get(uid, True)

    def playing(self, uid: int) -> int:
        k = self.k
        return {
            1001: int(20000 * (1 + 0.05 * k)),
            1002: int(50000 * (1 + 0.5 * k)),
            1003: int(30000 * (1 - 0.08 * k)),
            1004: 5000 * (k - 2) if k >= 3 else 0,
            1005: 100000 if k <= 1 else 100000 + int(60000 * (k - 1) / 7),
            1006: 1,
            1007: 4000,
            1008: 2000 if k >= 6 else 0,
        }[uid]

    def visits(self, uid: int) -> int:
        if uid in (1006, 1008):  # test place / unreleased placeholder
            return 200 if uid == 1006 else (40 if self.k < 6 else 50_000)
        return 10_000_000 + self.k * self.playing(uid) * 20

    def updated(self, uid: int) -> str:
        if uid == 1005:  # Roleplay Town ships an update on day 1
            return f"{day_str(1)}T18:00:00.000Z" if self.k >= 1 else f"{day_str(-30)}T18:00:00.000Z"
        return f"{day_str(-40)}T10:00:00Z"

    def game_json(self, uid: int) -> dict:
        info = self.GAMES[uid]
        ctype, cid, cname = info["creator"]
        return {"id": uid, "rootPlaceId": info["place"], "name": info["name"], "description": "",
                "creator": {"id": cid, "name": cname, "type": ctype, "hasVerifiedBadge": False},
                "price": None, "playing": self.playing(uid), "visits": self.visits(uid), "maxPlayers": 20,
                "created": info["created"], "updated": self.updated(uid), "genre": "All",
                "genre_l1": info["genre_l1"], "genre_l2": "", "favoritedCount": 1000 + self.k * 10}

    def sorts(self) -> list[dict]:
        def entry(uid, sponsored=False):
            return {"universeId": uid, "rootPlaceId": self.GAMES[uid]["place"], "name": self.GAMES[uid]["name"],
                    "playerCount": self.playing(uid), "isSponsored": sponsored}
        up = ([entry(1004)] if self.alive(1004) else []) + [entry(1001)]
        return [
            {"contentType": "Filters", "sortId": "filters", "filters": []},
            {"contentType": "Games", "sortId": "top-trending", "sortDisplayName": "Top Trending",
             "games": [entry(1002), entry(1001), entry(1005), entry(1007, sponsored=True)], "nextPageToken": "p2"},
            {"contentType": "Games", "sortId": "up-and-coming", "sortDisplayName": "Up-and-Coming", "games": up},
        ]

    def creator_games(self, kind: str, cid: int) -> list[dict]:
        rows = []
        for uid, info in self.GAMES.items():
            ctype, owner, _ = info["creator"]
            if ctype.lower() == kind and owner == cid and self.alive(uid):
                rows.append({"id": uid, "name": info["name"], "created": info["created"], "updated": self.updated(uid),
                             "placeVisits": self.visits(uid), "rootPlace": {"id": info["place"], "type": "Place"},
                             "creator": {"id": cid, "type": ctype}})
        return rows

    # ---- platform sources ------------------------------------------------------
    def topics(self, category: int) -> list[dict]:
        if category == 36:
            topics = [
                {"id": 9000, "title": "About the Announcements category", "slug": "about", "pinned": True,
                 "created_at": "2020-01-01T00:00:00Z"},
                {"id": 9001, "title": "Changes to DevEx rates", "slug": "changes-to-devex-rates",
                 "created_at": f"{day_str(-2)}T17:00:00.000Z"},
                {"id": 9002, "title": "Introducing a new Studio panel", "slug": "studio-panel",
                 "created_at": f"{day_str(-20)}T17:00:00.000Z"},
            ]
            if self.k >= 4:
                topics.insert(1, {"id": 9003, "title": "Update to the Discovery algorithm", "slug": "discovery",
                                  "created_at": f"{day_str(4)}T16:00:00.000Z"})
            return topics
        if category == 62:
            return [{"id": 9101, "title": "Release Notes for 700", "slug": "release-notes-700",
                     "created_at": f"{day_str(-1)}T20:00:00.000Z"}]
        return []

    RAW = {
        9001: "Hi Creators,\n\nStarting next month the DevEx rate increases to $0.0040 per Robux for all eligible "
              "creators. Payouts for earned Robux will use the new exchange rate automatically.\n\nThanks,\nThe Roblox Team",
        9002: "We are introducing a new Studio panel for organizing assets in large projects.",
        9003: "We are changing how the recommendation algorithm ranks games on the Home page. Games with strong "
              "retention will be recommended more often in Discovery.",
        9101: "Release Notes for 700. Improvements: faster physics stepping. Fixes: a crash when opening plugins.",
    }

    def help_article(self) -> dict:
        rate = "$0.0038" if self.k < 3 else "$0.0040"
        body = ("<p>The Developer Exchange (DevEx) program lets creators exchange earned Robux for real money.</p>"
                f"<p>The current DevEx rate is {rate} USD per Robux for eligible creators who meet the "
                "requirements.</p><p>You must be at least 13 years old, have a verified email address and a "
                "valid DevEx portal account, and be in good standing with the Terms of Use.</p>"
                "<p>Requests are reviewed by the DevEx team before payment.</p>")
        return {"article": {"id": 203314100, "title": "Developer Exchange (DevEx) FAQs", "body": body,
                            "html_url": "https://en.help.roblox.com/hc/en-us/articles/203314100",
                            "edited_at": f"{day_str(3 if self.k >= 3 else -100)}T00:00:00Z"}}


class Handler(BaseHTTPRequestHandler):
    scenario: Scenario

    def log_message(self, *args) -> None:  # keep test output quiet
        pass

    def _send(self, status: int, body: object = None, ctype: str = "application/json") -> None:
        raw = b"" if body is None else (body if isinstance(body, bytes) else
                                        (body.encode() if isinstance(body, str) else json.dumps(body).encode()))
        self.send_response(status)
        if raw:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: C901 - one routing table for the whole fake internet
        sc = self.scenario
        url = urlsplit(self.path)
        path, q = url.path, {k: v[0] for k, v in parse_qs(url.query).items()}
        sc.requests.append(path)
        if path in sc.fail_paths:
            return self._send(503, {"error": "down"})
        ids = [int(x) for x in q.get("universeIds", "").split(",") if x]
        if path == "/apis/explore-api/v1/get-sorts":
            if q.get("sortsPageToken") == "s2":
                return self._send(200, {"sorts": [{"sortId": "top-playing-now", "sortDisplayName": "Top Playing Now",
                                                   "games": [{"universeId": 1005}, {"universeId": 1002},
                                                             {"universeId": 1003}]}]})
            return self._send(200, {"sorts": sc.sorts(), "nextSortsPageToken": "s2"})
        if path == "/apis/explore-api/v1/get-sort-content":
            return self._send(200, {"games": [{"universeId": 1003, "playerCount": sc.playing(1003)}],
                                    "nextPageToken": ""})
        m = re.fullmatch(r"/apis/universes/v1/places/(\d+)/universe", path)
        if m:
            pid = int(m.group(1))
            uid = next((u for u, g in sc.GAMES.items() if g["place"] == pid), None)
            return self._send(200, {"universeId": uid}) if uid else self._send(404, {"errors": []})
        if path == "/games/v1/games":
            return self._send(200, {"data": [sc.game_json(u) for u in ids if u in sc.GAMES and sc.alive(u)]})
        if path == "/games/v1/games/votes":
            return self._send(200, {"data": [{"id": u, "upVotes": 900, "downVotes": 100} for u in ids]})
        m = re.fullmatch(r"/games/v2/(groups|users)/(\d+)/(gamesV2|games)", path)
        if m:
            kind = "group" if m.group(1) == "groups" else "user"
            return self._send(200, {"data": sc.creator_games(kind, int(m.group(2))), "nextPageCursor": None})
        if path == "/thumbnails/v1/games/icons":
            return self._send(200, {"data": [{"targetId": u, "state": "Completed",
                                              "imageUrl": f"https://tr.rbxcdn.com/icon-{u}.png"} for u in ids]})
        m = re.fullmatch(r"/(groups|users)/v1/(groups|users)/(\d+)", path)
        if m:
            return self._send(200, {"id": int(m.group(3)), "name": "SoloDev" if m.group(1) == "users" else "Group"})
        # DevForum (Discourse)
        if path == "/devforum/c/updates/announcements/36.json":
            return self._send(200, {"topic_list": {"topics": sc.topics(36)}})
        if path == "/devforum/c/updates/release-notes.json":
            return self._send(404, {"errors": ["not found"]})
        if path == "/devforum/categories.json":
            return self._send(200, {"category_list": {"categories": [
                {"id": 45, "slug": "updates", "subcategory_list": [
                    {"id": 36, "slug": "announcements"}, {"id": 62, "slug": "release-notes"}]}]}})
        if path == "/devforum/c/updates/release-notes/62.json":
            return self._send(200, {"topic_list": {"topics": sc.topics(62)}})
        m = re.fullmatch(r"/devforum/raw/(\d+)/1", path)
        if m:
            return self._send(200, sc.RAW.get(int(m.group(1)), ""), "text/plain")
        # Newsroom
        if path == "/news/newsroom":
            html = ('<html><body><a href="/newsroom/2026/02/creator-earnings-update">Creator earnings</a>'
                    '<a href="/en-au/newsroom/2026/02/creator-earnings-update">AU copy</a>'
                    '<a href="https://example.test/newsroom/2025/06/old-post">Old</a></body></html>')
            return self._send(200, html, "text/html")
        if path == "/news/newsroom/2026/02/creator-earnings-update":
            html = (f'<html><head><meta property="og:title" content="Creator earnings reach a new high | Roblox">'
                    f'<meta property="article:published_time" content="{day_str(-3)}T15:00:00Z"></head>'
                    '<body><article><p>Creators earned more than ever this quarter through the Developer Exchange '
                    'program and engagement-based payouts.</p><p>We are expanding payouts to more creators.</p>'
                    '</article></body></html>')
            return self._send(200, html, "text/html")
        if path == "/news/newsroom/2025/06/old-post":
            html = ('<html><head><meta property="og:title" content="Old post">'
                    '<meta property="article:published_time" content="2025-06-01T00:00:00Z"></head>'
                    '<body><article><p>Old news.</p></article></body></html>')
            return self._send(200, html, "text/html")
        # Help center
        if path == "/help/api/v2/help_center/en-us/articles/203314100.json":
            return self._send(200, sc.help_article())
        return self._send(404, {"errors": [f"no route for {path}"]})

    def do_POST(self) -> None:
        sc = self.scenario
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        sc.requests.append("POST " + self.path)
        if self.path.startswith("/discord/api/webhooks/"):
            sc.discord_payloads.append(body)
            return self._send(204)
        if self.path == "/models/inference/chat/completions":
            sc.llm_calls += 1
            if self.headers.get("Authorization") != "Bearer test-token":
                return self._send(401, {"error": "bad token"})
            if body.get("model") == "openai/missing-model":
                return self._send(404, {"error": {"code": "unknown_model", "message": "Unknown model"}})
            prompt = body["messages"][-1]["content"]
            level = "high" if re.search(r"devex|algorithm|payout", prompt, re.I) else "low"
            title = re.search(r"Title: (.*)", prompt).group(1)
            content = json.dumps({"summary": f"AI summary of {title}. Two sentences here.",
                                  "importance": level, "why": "test rubric", "tags": ["devex"]})
            return self._send(200, {"choices": [{"message": {"role": "assistant", "content": content}}]})
        return self._send(404, {"errors": ["no route"]})


class MockServices:
    def __init__(self) -> None:
        self.scenario = Scenario()
        handler = type("BoundHandler", (Handler,), {"scenario": self.scenario})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "MockServices":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
