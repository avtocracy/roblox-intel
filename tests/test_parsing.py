from roblox_intel.platform import parse_feed
from roblox_intel.roblox import Roblox, _game_items, place_id_from, pretty_genre


class FakeHttp:
    """Maps url-prefix -> JSON so API wrappers can be tested without a network."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get_json(self, url, params=None, ttl=None, headers=None):
        self.calls.append((url, params))
        key = url.rsplit("/", 1)[-1]
        value = self.routes[key]
        return value(params) if callable(value) else value


def test_chart_sorts_pagination_sponsored_and_dedupe():
    sorts_page1 = {"sorts": [
        {"contentType": "Filters", "sortId": "filters", "filters": [{"filterType": "device"}]},
        {"sortId": "top-trending", "sortDisplayName": "Top Trending", "nextPageToken": "t2",
         "games": [{"universeId": 1}, {"universeId": 2, "isSponsored": True}, {"universeId": 1}]},
    ], "nextSortsPageToken": "s2"}
    sorts_page2 = {"sorts": [{"topicId": 77, "topic": "Up-and-Coming", "content": {"items": [{"universeId": 5}]}}]}
    http = FakeHttp({
        "get-sorts": lambda p: sorts_page2 if p.get("sortsPageToken") == "s2" else sorts_page1,
        "get-sort-content": {"games": [{"universeId": 3}, {"universeId": 2}], "nextPageToken": None},
    })
    sorts = Roblox(http, {"apis": "https://apis.example"}).chart_sorts(max_pages=3)
    assert [s.sort_id for s in sorts] == ["top-trending", "77"]
    trending = sorts[0]
    assert [e["universe_id"] for e in trending.entries] == [1, 2, 3]
    assert [e["rank"] for e in trending.entries] == [1, 2, 3]
    assert trending.entries[1]["sponsored"] is True
    assert sorts[1].name == "Up-and-Coming" and sorts[1].entries[0]["universe_id"] == 5


def test_game_items_finds_nested_lists():
    assert _game_items({"a": {"b": [{"universeId": 9}]}}) == [{"universeId": 9}]
    assert _game_items({"filters": [{"x": 1}]}) == []


def test_pretty_genre_and_place_ids():
    assert pretty_genre("roleplay_and_avatar_sim") == "Roleplay & Avatar Sim"
    assert pretty_genre("Obby & platformer") == "Obby & Platformer"
    assert pretty_genre("RPG") == "RPG"
    assert pretty_genre("tower_defense") == "Tower Defense"
    assert pretty_genre("") == ""
    assert place_id_from("https://www.roblox.com/games/2753915549/Blox-Fruits") == 2753915549
    assert place_id_from("https://www.roblox.com/games/start?placeId=42") == 42
    assert place_id_from(123) == 123
    assert place_id_from("not a link") is None


def test_parse_rss_and_atom():
    rss = """<?xml version="1.0"?><rss version="2.0"><channel><item><title>Release Notes for 700</title>
      <link>https://devforum.roblox.com/t/release-notes-for-700/123</link>
      <pubDate>Wed, 15 Oct 2026 20:00:00 +0000</pubDate><description>&lt;p&gt;Fixes&lt;/p&gt;</description>
      </item></channel></rss>"""
    items = parse_feed(rss)
    assert items[0]["title"] == "Release Notes for 700"
    assert items[0]["published"].startswith("2026-10-15T20:00:00")
    assert "<p>Fixes</p>" in items[0]["html"]
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Post</title>
      <link rel="self" href="https://example.test/self"/><link rel="alternate" href="https://example.test/post"/>
      <updated>2026-10-01T10:00:00Z</updated><summary>Hello</summary></entry></feed>"""
    entry = parse_feed(atom)[0]
    assert entry["link"] == "https://example.test/post" and entry["published"].startswith("2026-10-01")
