import pytest

from roblox_intel import discord
from roblox_intel.config import DEFAULTS
from roblox_intel.http import HttpError
from roblox_intel.report import md, to_markdown, trend_highlights


def report(**over):
    base = {
        "date": "2026-10-18", "dashboard_url": "https://me.github.io/roblox-intel/", "history_note": "",
        "highlights": [{"text": f"[**Game {i}**](https://www.roblox.com/games/{i}) +{i}0% players in 24h"} for i in range(5)],
        "developer_moves": [{"text": "Studio X released a new game: Y (Simulation)", "type": "new_game"}],
        "developer_moves_total": 3,
        "platform": {"counts": {"high": 1, "medium": 2, "low": 3}, "featured": [
            {"title": "DevEx rate update", "url": "https://devforum.roblox.com/t/x/1", "summary": "Rates rise.",
             "importance": "high"}]},
        "health": [{"id": "charts", "label": "Roblox Charts", "status": "fail", "detail": "HTTP 403",
                    "fallback": "tracked games only"}],
    }
    base.update(over)
    return base


def test_payload_shape_and_limits():
    p = discord.build_payload(report(), DEFAULTS)
    assert p["allowed_mentions"] == {"parse": []}
    titles = [e["title"] for e in p["embeds"]]
    assert titles[0].startswith("📈") and "Sun Oct 18" in titles[0]
    assert any("Data source issues" in t for t in titles)
    assert p["embeds"][0]["url"] == "https://me.github.io/roblox-intel/"
    assert "+2 more on the dashboard" in p["embeds"][1]["description"]
    assert "1 high · 2 medium · 3 low" in p["embeds"][2]["footer"]["text"]


def test_payload_is_trimmed_to_discord_limits():
    long = "x" * 900
    r = report(highlights=[{"text": long}] * 5,
               developer_moves=[{"text": long, "type": "new_game"}] * 5,
               platform={"counts": {"high": 5, "medium": 0, "low": 0},
                         "featured": [{"title": long, "url": "https://x.test", "summary": long, "importance": "high"}] * 5})
    p = discord.build_payload(r, DEFAULTS)
    total = sum(len(e["title"]) + len(e["description"]) + len((e.get("footer") or {}).get("text", "")) for e in p["embeds"])
    assert total <= 6000 and all(len(e["description"]) <= 4096 for e in p["embeds"])


def test_markdown_escaping_of_game_names():
    assert md("**Free Robux** _now_ @everyone") == "\\*\\*Free Robux\\*\\* \\_now\\_ @everyone"


def test_send_validates_url_and_hides_token():
    class Boom:
        def post_json(self, url, payload, headers=None, retry_statuses=None, label=None):
            raise HttpError(label, 404, "HTTP 404")

    with pytest.raises(ValueError):
        discord.send({}, "https://evil.example/api/webhooks/1/abc", Boom())
    with pytest.raises(RuntimeError) as err:
        discord.send({}, "https://discord.com/api/webhooks/123/SECRET-token", Boom())
    assert "SECRET" not in str(err.value) and "deleted" in str(err.value)


def test_day_one_highlights_use_roblox_charts():
    analysis = {"available": True, "windows": {"24h": {"available": False}}, "genres": {},
                "breakouts": [], "genre_now": [{"genre": "Simulation", "share": 31.5, "ccu": 900000, "games": 40}],
                "chart_leaders": [{"name": "Up-and-Coming", "games": [{"universe_id": 1, "name": "Rise", "place_id": 5, "ccu_now": 1200}]},
                                  {"name": "Top Trending", "games": [{"universe_id": 2, "name": "Hot", "place_id": 6, "ccu_now": 9000}]}],
                "biggest": [{"universe_id": 3, "name": "Giant", "place_id": 7, "ccu_now": 500000}]}
    picks = trend_highlights(analysis)
    kinds = [p["kind"] for p in picks]
    assert kinds[:3] == ["chart_leader", "chart_leader", "genre_share"] and "biggest" in kinds
    md_text = to_markdown(report())
    assert "## Trend highlights" in md_text and "FAIL: Roblox Charts" in md_text
