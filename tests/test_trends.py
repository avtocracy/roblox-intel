from datetime import datetime, timezone

from roblox_intel.config import DEFAULTS
from roblox_intel.context import Context
from roblox_intel.storage import Store
from roblox_intel.trends import analyze, find_base_day, genre_changes


def make_ctx(tmp_path, day):
    return Context(settings=DEFAULTS, store=Store(tmp_path), http=None, roblox=None, day=day,
                   now=datetime(2026, 10, 18, tzinfo=timezone.utc))


def snap(day, games):
    return {"date": day, "sorts": [{"id": "up-and-coming", "name": "Up-and-Coming", "universe_ids": [3], "sponsored": []}],
            "games": {str(u): {"playing": p, "visits": v, "favorites": 0, "likes": 1, "dislikes": 0,
                               "updated": None} for u, (p, v) in games.items()}}


def test_find_base_day_tolerance():
    dates = ["2026-10-01", "2026-10-10", "2026-10-16", "2026-10-17"]
    assert find_base_day(dates, "2026-10-18", 1, 0) == "2026-10-17"
    assert find_base_day(dates, "2026-10-18", 7, 1) == "2026-10-10"  # 8 days back is within tolerance
    assert find_base_day(dates + ["2026-10-11"], "2026-10-18", 7, 1) == "2026-10-11"
    assert find_base_day(dates, "2026-10-18", 30, 3) is None
    assert find_base_day(["2026-09-16"], "2026-10-18", 30, 3) == "2026-09-16"


def test_analyze_growth_windows_genres_and_breakouts(tmp_path):
    store = Store(tmp_path)
    store.save_catalog({
        "1": {"name": "Big Sim", "genre": "Simulation", "created": "2020-01-01T00:00:00Z", "root_place_id": 11},
        "2": {"name": "Shooter X", "genre": "Shooter", "created": "2021-01-01T00:00:00Z"},
        "3": {"name": "New Hit", "genre": "Simulation", "created": "2026-10-01T00:00:00Z"},
        "4": {"name": "Tiny", "genre": "Simulation", "created": "2020-01-01T00:00:00Z"},
    })
    store.save_snapshot("2026-10-17", snap("2026-10-17", {1: (10000, 1_000_000), 2: (8000, 500_000),
                                                         4: (10, 100)}))
    store.save_snapshot("2026-10-18", snap("2026-10-18", {1: (15000, 1_300_000), 2: (6000, 560_000),
                                                         3: (4000, 900_000), 4: (90, 150)}))
    result = analyze(make_ctx(tmp_path, "2026-10-18"))
    w = result["windows"]["24h"]
    assert w["available"] and w["span_days"] == 1
    assert [r["name"] for r in w["gainers"]] == ["Big Sim"], "tiny games and new games are not ranked"
    assert w["gainers"][0]["ccu_pct"] == 50 and w["gainers"][0]["visits_per_day"] == 300_000
    assert w["gainers"][0]["place_id"] == 11
    assert w["losers"][0]["name"] == "Shooter X"
    assert not result["windows"]["7d"]["available"]
    genres = {g["genre"]: g for g in result["genres"]["24h"]}
    assert genres["Simulation"]["ccu_then"] == 10010 and genres["Simulation"]["ccu_now"] == 15090, \
        "genre change only compares games present on both days"
    assert [b["name"] for b in result["breakouts"]] == ["New Hit"]
    assert result["breakouts"][0]["age_days"] == 17 and result["breakouts"][0]["charts"] == ["Up-and-Coming"]
    assert result["history_days"] == 2


def test_genre_changes_minimum_size():
    s = {"games": {"1": {"playing": 100}}}
    b = {"games": {"1": {"playing": 50}}}
    assert genre_changes(s, b, {"1": {"genre": "Puzzle"}}) == []  # below min_total
