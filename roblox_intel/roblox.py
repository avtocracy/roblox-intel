"""Thin wrappers around Roblox's public, unauthenticated web APIs.

Endpoints used (all public, no login):
  apis.roblox.com/explore-api/v1/get-sorts           Charts page sorts
  apis.roblox.com/explore-api/v1/get-sort-content    more games for one sort
  games.roblox.com/v1/games?universeIds=             players, visits, favorites, genre, dates
  games.roblox.com/v1/games/votes?universeIds=       likes / dislikes
  games.roblox.com/v2/users/{id}/games               a user's public games
  games.roblox.com/v2/groups/{id}/gamesV2            a group's public games
  apis.roblox.com/universes/v1/places/{id}/universe  place id -> universe id
  users.roblox.com/v1/users/{id}, groups.roblox.com/v1/groups/{id}
  thumbnails.roblox.com/v1/games/icons               game icons for the dashboard
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from .http import HttpClient, HttpError

BATCH = 50
WEEK = 7 * 86400
_ACRONYMS = {"rpg", "fps", "pvp", "pve", "rng", "ugc", "vr", "tv"}
_SMALL = {"and", "or", "of", "the", "a", "in"}
# Official genre names (create.roblox.com/docs/production/publishing/experience-genres)
_GENRES = ["Action", "Adventure", "Education", "Entertainment", "Obby & Platformer", "Party & Casual",
           "Puzzle", "RPG", "Roleplay & Avatar Sim", "Shooter", "Shopping", "Simulation", "Social",
           "Sports & Racing", "Strategy", "Survival", "Utility & Other"]
_GENRE_KEYS = {re.sub(r"[^a-z0-9]", "", g.lower().replace("&", "and")): g for g in _GENRES}


def chunks(items: list, size: int = BATCH) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def pretty_genre(value: str | None) -> str:
    """'roleplay_and_avatar_sim' / 'Roleplay & Avatar Sim' -> 'Roleplay & Avatar Sim'."""
    if not value:
        return ""
    text = " ".join(str(value).replace("_", " ").split())
    canonical = _GENRE_KEYS.get(re.sub(r"[^a-z0-9]", "", text.lower().replace("&", "and")))
    if canonical:
        return canonical
    if text != text.lower() and text != text.upper():
        return text  # already nicely cased
    words = []
    for i, word in enumerate(text.lower().split(" ")):
        if word in _ACRONYMS:
            words.append(word.upper())
        elif i and word in _SMALL:
            words.append(word)
        else:
            words.append(word[:1].upper() + word[1:])
    return " ".join(words)


def place_id_from(value: Any) -> int | None:
    """Accept 123, '123', or any roblox.com/games/123/... link."""
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    if text.isdigit():
        return int(text)
    m = re.search(r"/games/(\d+)", text) or re.search(r"[?&]placeId=(\d+)", text, re.I)
    return int(m.group(1)) if m else None


@dataclass
class ChartSort:
    sort_id: str
    name: str
    entries: list[dict] = field(default_factory=list)  # [{universe_id, rank, sponsored, players}]


def _game_items(obj: Any) -> list[dict]:
    """Find the list of game entries in an explore-api object, wherever it sits."""
    if isinstance(obj, dict):
        games = obj.get("games")
        if isinstance(games, list) and games and isinstance(games[0], dict):
            return games
        for value in obj.values():
            if isinstance(value, (dict, list)):
                found = _game_items(value)
                if found:
                    return found
    elif isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and "universeId" in obj[0]:
            return obj
        for value in obj:
            found = _game_items(value)
            if found:
                return found
    return []


def _entry(item: dict, rank: int) -> dict | None:
    uid = item.get("universeId") or item.get("universe_id")
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return None
    return {
        "universe_id": uid,
        "rank": rank,
        "sponsored": bool(item.get("isSponsored") or item.get("adId") or item.get("isShowSponsoredLabel")),
        "players": item.get("playerCount"),
    }


class Roblox:
    def __init__(self, http: HttpClient, endpoints: dict[str, str]):
        self.http = http
        self.ep = {k: v.rstrip("/") for k, v in endpoints.items()}
        self.session_id = str(uuid.uuid4())

    # ---- Charts (Discover) --------------------------------------------------

    def chart_sorts(self, device: str = "computer", country: str = "all", max_sorts: int = 40,
                    max_pages: int = 2, extra_params: dict | None = None) -> list[ChartSort]:
        base = f"{self.ep['apis']}/explore-api/v1"
        params = {"sessionId": self.session_id, "device": device, "country": country,
                  **(extra_params or {})}
        sorts: list[ChartSort] = []
        token = None
        for _ in range(10):  # the sort list itself is paginated
            data = self.http.get_json(f"{base}/get-sorts", params={**params, "sortsPageToken": token})
            for raw in data.get("sorts") or []:
                sort_id = raw.get("sortId") or raw.get("topicId") or raw.get("id")
                items = _game_items(raw)
                if not sort_id or not items:
                    continue  # filter bars, banners, etc.
                name = (raw.get("sortDisplayName") or raw.get("topic") or raw.get("title")
                        or raw.get("name") or str(sort_id))
                sort = ChartSort(str(sort_id), str(name))
                self._extend(sort, items)
                page_token = raw.get("nextPageToken")
                pages = 1
                while page_token and pages < max_pages:
                    more = self.http.get_json(f"{base}/get-sort-content", params={
                        **params, "sortId": sort_id, "pageToken": page_token})
                    self._extend(sort, _game_items(more))
                    page_token = more.get("nextPageToken")
                    pages += 1
                sorts.append(sort)
                if len(sorts) >= max_sorts:
                    return sorts
            token = data.get("nextSortsPageToken")
            if not token:
                break
        return sorts

    @staticmethod
    def _extend(sort: ChartSort, items: list[dict]) -> None:
        seen = {e["universe_id"] for e in sort.entries}
        for item in items:
            entry = _entry(item, len(sort.entries) + 1)
            if entry and entry["universe_id"] not in seen:
                seen.add(entry["universe_id"])
                sort.entries.append(entry)

    # ---- game stats ---------------------------------------------------------

    def games(self, universe_ids: Iterable[int]) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for batch in chunks(sorted(set(universe_ids))):
            data = self.http.get_json(f"{self.ep['games']}/v1/games",
                                      params={"universeIds": ",".join(map(str, batch))})
            for game in data.get("data") or []:
                out[int(game["id"])] = game
        return out

    def votes(self, universe_ids: Iterable[int]) -> dict[int, tuple[int, int]]:
        out: dict[int, tuple[int, int]] = {}
        for batch in chunks(sorted(set(universe_ids))):
            data = self.http.get_json(f"{self.ep['games']}/v1/games/votes",
                                      params={"universeIds": ",".join(map(str, batch))})
            for row in data.get("data") or []:
                out[int(row["id"])] = (int(row.get("upVotes") or 0), int(row.get("downVotes") or 0))
        return out

    def icons(self, universe_ids: Iterable[int]) -> dict[int, str]:
        out: dict[int, str] = {}
        for batch in chunks(sorted(set(universe_ids))):
            data = self.http.get_json(f"{self.ep['thumbnails']}/v1/games/icons", ttl=WEEK, params={
                "universeIds": ",".join(map(str, batch)), "returnPolicy": "PlaceHolder",
                "size": "150x150", "format": "Png", "isCircular": "false"})
            for row in data.get("data") or []:
                if row.get("state") == "Completed" and row.get("imageUrl"):
                    out[int(row["targetId"])] = row["imageUrl"]
        return out

    def place_universe(self, place_id: int) -> int | None:
        data = self.http.get_json(f"{self.ep['apis']}/universes/v1/places/{place_id}/universe", ttl=WEEK)
        uid = data.get("universeId")
        return int(uid) if uid else None

    # ---- creators -----------------------------------------------------------

    def creator_games(self, kind: str, creator_id: int, limit: int = 100) -> list[dict]:
        if kind == "group":
            urls = [f"{self.ep['games']}/v2/groups/{creator_id}/gamesV2",
                    f"{self.ep['games']}/v2/groups/{creator_id}/games"]
        else:
            urls = [f"{self.ep['games']}/v2/users/{creator_id}/games"]
        last_error: HttpError | None = None
        for url in urls:
            try:
                return self._paged(url, limit)
            except HttpError as exc:  # gamesV2 missing -> try the older route
                last_error = exc
                if exc.status not in (400, 404):
                    raise
        raise last_error or HttpError(urls[0], None, "no endpoint worked")

    def _paged(self, url: str, limit: int) -> list[dict]:
        rows: list[dict] = []
        cursor = None
        while len(rows) < limit:
            data = self.http.get_json(url, params={
                "accessFilter": 2, "limit": 50, "sortOrder": "Desc", "cursor": cursor})
            rows.extend(data.get("data") or [])
            cursor = data.get("nextPageCursor")
            if not cursor:
                break
        return rows[:limit]

    def creator_info(self, kind: str, creator_id: int) -> dict:
        if kind == "group":
            return self.http.get_json(f"{self.ep['groups']}/v1/groups/{creator_id}", ttl=WEEK)
        return self.http.get_json(f"{self.ep['users']}/v1/users/{creator_id}", ttl=WEEK)
