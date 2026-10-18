"""Trend tracker: daily snapshot of popular games and growth analysis."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .context import Context
from .http import HttpError
from .roblox import pretty_genre
from .util import days_between, pct_change, shift_day

CHARTS = ("charts", "Roblox Charts (explore-api)")
GAMES = ("games-api", "Game stats (games.roblox.com)")
VOTES = ("votes-api", "Likes/dislikes (games.roblox.com)")
ICONS = ("icons-api", "Game icons (thumbnails.roblox.com)")
WINDOWS = (("24h", 1, 0), ("7d", 7, 1), ("30d", 30, 3))  # label, days back, tolerance
MAX_NEW_ICONS = 150


def genre_of(game: dict) -> str:
    return game.get("genre") or "Unclassified"


# ---- collection --------------------------------------------------------------

def collect(ctx: Context, extra_uids: Iterable[int] = ()) -> tuple[dict | None, list[dict]]:
    """Fetch today's charts + stats, save the snapshot, refresh the catalog.

    Returns (snapshot, ownership_changes). snapshot is None only when the game
    stats API itself is unreachable, in which case nothing is stored for today.
    """
    cfg = ctx.settings["trends"]
    charts_cfg = cfg["charts"]
    store = ctx.store
    catalog = store.catalog()

    sorts = []
    try:
        sorts = ctx.roblox.chart_sorts(
            device=charts_cfg["device"], country=charts_cfg["country"],
            max_sorts=int(charts_cfg["max_sorts"]), max_pages=int(charts_cfg["max_pages_per_sort"]),
            extra_params=charts_cfg.get("extra_params") or {})
        count = len({e["universe_id"] for s in sorts for e in s.entries})
        if sorts:
            ctx.health.ok(*CHARTS, f"{len(sorts)} sorts, {count} games")
        else:
            ctx.health.warn(*CHARTS, "returned no sorts",
                            fallback="refreshed previously tracked and developer games only")
    except HttpError as exc:
        ctx.health.fail(*CHARTS, exc.reason, fallback="refreshed previously tracked and developer games only")

    exclude_ads = bool(cfg.get("exclude_sponsored", True))
    chart_uids = {e["universe_id"] for s in sorts for e in s.entries
                  if not (exclude_ads and e["sponsored"])}
    keep_days = int(cfg["keep_tracking_days"])
    recent = {int(uid) for uid, g in catalog.items()
              if g.get("last_seen_in_charts") and days_between(g["last_seen_in_charts"], ctx.day) <= keep_days}
    tracked = chart_uids | recent | {int(u) for u in extra_uids}
    if not tracked:
        ctx.health.fail(*GAMES, "nothing to track (charts unavailable and no history yet)")
        return None, []

    try:
        details = ctx.roblox.games(tracked)
    except HttpError as exc:
        ctx.health.fail(*GAMES, exc.reason, fallback="no snapshot stored today")
        return None, []
    if not details:
        ctx.health.fail(*GAMES, f"no data returned for {len(tracked)} games", fallback="no snapshot stored today")
        return None, []
    ctx.health.ok(*GAMES, f"{len(details)} of {len(tracked)} games")

    votes: dict[int, tuple[int, int]] = {}
    try:
        votes = ctx.roblox.votes(details)
        ctx.health.ok(*VOTES, f"{len(votes)} games")
    except HttpError as exc:
        ctx.health.warn(*VOTES, exc.reason, fallback="like ratios missing today")

    ownership_changes: list[dict] = []
    games: dict[str, dict] = {}
    for uid, g in details.items():
        key = str(uid)
        rec = catalog.get(key, {})
        creator = g.get("creator") or {}
        new_owner = (str(creator.get("type", "")).lower(), creator.get("id"))
        old_owner = (rec.get("creator_type"), rec.get("creator_id"))
        if rec.get("creator_id") and new_owner[1] and old_owner != new_owner:
            ownership_changes.append({
                "universe_id": uid, "game": g.get("name"),
                "from": {"type": old_owner[0], "id": old_owner[1], "name": rec.get("creator_name")},
                "to": {"type": new_owner[0], "id": new_owner[1], "name": creator.get("name")},
            })
        rec.update({
            "name": g.get("name") or rec.get("name") or f"Universe {uid}",
            "root_place_id": g.get("rootPlaceId"),
            "creator_type": new_owner[0] or rec.get("creator_type"),
            "creator_id": new_owner[1] or rec.get("creator_id"),
            "creator_name": creator.get("name") or rec.get("creator_name"),
            "genre": pretty_genre(g.get("genre_l1")) or (pretty_genre(g.get("genre"))
                                                       if g.get("genre") not in (None, "", "All") else ""),
            "subgenre": pretty_genre(g.get("genre_l2")),
            "created": g.get("created"),
            "updated": g.get("updated"),
            "max_players": g.get("maxPlayers"),
            "price": g.get("price"),
            "last_seen": ctx.day,
        })
        rec.setdefault("first_seen", ctx.day)
        if uid in chart_uids:
            rec["last_seen_in_charts"] = ctx.day
        catalog[key] = rec
        up, down = votes.get(uid, (None, None))
        games[key] = {
            "playing": int(g.get("playing") or 0),
            "visits": int(g.get("visits") or 0),
            "favorites": int(g.get("favoritedCount") or 0),
            "likes": up,
            "dislikes": down,
        }

    _refresh_icons(ctx, catalog, games)

    # forget games nobody has seen for over a year (keeps games.json from growing forever)
    catalog = {k: v for k, v in catalog.items() if days_between(v.get("last_seen") or ctx.day, ctx.day) <= 400}
    snapshot = {
        "date": ctx.day,
        "fetched_at": ctx.now.isoformat(timespec="seconds"),
        "sorts": [{"id": s.sort_id, "name": s.name,
                   "universe_ids": [e["universe_id"] for e in s.entries],
                   "sponsored": [e["universe_id"] for e in s.entries if e["sponsored"]]}
                  for s in sorts],
        "games": games,
    }
    store.save_snapshot(ctx.day, snapshot)
    store.save_catalog(catalog)
    return snapshot, ownership_changes


def _refresh_icons(ctx: Context, catalog: dict, games: dict) -> None:
    """Fetch icons for the biggest games that don't have one yet (dashboard only)."""
    missing = [int(uid) for uid in sorted(games, key=lambda u: -games[u]["playing"])
               if not catalog[uid].get("icon")][:MAX_NEW_ICONS]
    if not missing:
        return
    try:
        icons = ctx.roblox.icons(missing)
        for uid, url in icons.items():
            catalog[str(uid)]["icon"] = url
        ctx.health.ok(*ICONS, f"{len(icons)} new icons")
    except HttpError as exc:
        ctx.health.warn(*ICONS, exc.reason, fallback="dashboard shows games without icons")


# ---- analysis ----------------------------------------------------------------

def find_base_day(dates: list[str], day: str, back: int, tolerance: int) -> str | None:
    """Latest snapshot date in [day-back-tolerance, day-back]."""
    lo, hi = shift_day(day, -back - tolerance), shift_day(day, -back)
    candidates = [d for d in dates if lo <= d <= hi]
    return candidates[-1] if candidates else None


def _row(uid: str, cat: dict, now: dict, then: dict, span: int) -> dict:
    visits_gain = max(0, now["visits"] - then["visits"])
    return {
        "universe_id": int(uid),
        "place_id": cat.get("root_place_id"),
        "name": cat.get("name", uid),
        "genre": genre_of(cat),
        "creator": cat.get("creator_name"),
        "ccu_now": now["playing"],
        "ccu_then": then["playing"],
        "ccu_delta": now["playing"] - then["playing"],
        "ccu_pct": pct_change(now["playing"], then["playing"]),
        "visits_per_day": round(visits_gain / max(1, span)),
        "favorites_gain": max(0, now["favorites"] - then["favorites"]),
    }


def analyze(ctx: Context) -> dict[str, Any]:
    cfg = ctx.settings["trends"]
    store = ctx.store
    day = ctx.day
    snap = store.load_snapshot(day)
    if not snap:
        return {"available": False, "reason": "no snapshot for today"}
    catalog = store.catalog()
    dates = [d for d in store.snapshot_dates() if d <= day]
    top_n = int(cfg["top_n"])
    min_ccu, min_base = int(cfg["min_ccu_for_growth"]), int(cfg["min_base_ccu"])

    result: dict[str, Any] = {"available": True, "date": day, "history_days": len(dates),
                              "first_day": dates[0] if dates else day, "windows": {}, "genres": {}}

    for label, back, tol in WINDOWS:
        base_day = find_base_day(dates, day, back, tol)
        if not base_day:
            result["windows"][label] = {"available": False, "needs_days": back}
            continue
        base = store.load_snapshot(base_day) or {"games": {}}
        span = days_between(base_day, day)
        rows = [_row(uid, catalog.get(uid, {}), now, base["games"][uid], span)
                for uid, now in snap["games"].items() if uid in base["games"]]
        _add_visit_growth(store, dates, base_day, span, tol, rows)
        eligible = [r for r in rows if r["ccu_now"] >= min_ccu and r["ccu_then"] >= min_base
                    and (r["ccu_pct"] or 0) > 0]
        decliners = [r for r in rows if r["ccu_then"] >= min_ccu and (r["ccu_pct"] or 0) < 0]
        result["windows"][label] = {
            "available": True,
            "base_day": base_day,
            "span_days": span,
            "compared_games": len(rows),
            "gainers": sorted(eligible, key=lambda r: -(r["ccu_pct"] or 0))[:top_n],
            "losers": sorted(decliners, key=lambda r: r["ccu_pct"] or 0)[:top_n],
            "abs_gainers": sorted((r for r in rows if r["ccu_delta"] > 0), key=lambda r: -r["ccu_delta"])[:top_n],
        }
        result["genres"][label] = genre_changes(snap, base, catalog)

    result["genre_now"] = genre_totals(snap, catalog)
    result["breakouts"] = breakouts(ctx, snap, catalog)
    result["chart_leaders"] = chart_leaders(snap, catalog)
    result["biggest"] = sorted(
        ({"universe_id": int(u), "place_id": catalog.get(u, {}).get("root_place_id"),
          "name": catalog.get(u, {}).get("name", u), "genre": genre_of(catalog.get(u, {})),
          "ccu_now": g["playing"]}
         for u, g in snap["games"].items()), key=lambda r: -r["ccu_now"])[:top_n]
    return result


def _add_visit_growth(store, dates: list[str], mid: str, span: int, tol: int, rows: list[dict]) -> None:
    """Compare visits/day in this window with the window before it (acceleration)."""
    start = find_base_day(dates, mid, span, tol)
    if not start:
        return
    mid_snap, start_snap = store.load_snapshot(mid), store.load_snapshot(start)
    prev_span = max(1, days_between(start, mid))
    for r in rows:
        uid = str(r["universe_id"])
        a, b = start_snap["games"].get(uid), mid_snap["games"].get(uid)
        if a and b:
            prev = max(0, b["visits"] - a["visits"]) / prev_span
            r["visits_growth_pct"] = pct_change(r["visits_per_day"], prev)


def genre_totals(snap: dict, catalog: dict) -> list[dict]:
    totals: dict[str, int] = defaultdict(int)
    counts: dict[str, int] = defaultdict(int)
    for uid, g in snap["games"].items():
        genre = genre_of(catalog.get(uid, {}))
        totals[genre] += g["playing"]
        counts[genre] += 1
    grand = sum(totals.values()) or 1
    return sorted(({"genre": k, "ccu": v, "games": counts[k], "share": round(v / grand * 100, 1)}
                   for k, v in totals.items()), key=lambda r: -r["ccu"])


def genre_changes(snap: dict, base: dict, catalog: dict, min_total: int = 2000) -> list[dict]:
    """Players per genre, compared over the same set of games in both snapshots."""
    now_t: dict[str, int] = defaultdict(int)
    then_t: dict[str, int] = defaultdict(int)
    counts: dict[str, int] = defaultdict(int)
    for uid, g in snap["games"].items():
        old = base["games"].get(uid)
        if not old:
            continue
        genre = genre_of(catalog.get(uid, {}))
        now_t[genre] += g["playing"]
        then_t[genre] += old["playing"]
        counts[genre] += 1
    rows = [{"genre": k, "ccu_now": now_t[k], "ccu_then": then_t[k], "delta": now_t[k] - then_t[k],
             "pct": pct_change(now_t[k], then_t[k]), "games": counts[k]}
            for k in now_t if max(now_t[k], then_t[k]) >= min_total]
    return sorted(rows, key=lambda r: -(r["pct"] or 0))


def breakouts(ctx: Context, snap: dict, catalog: dict) -> list[dict]:
    cfg = ctx.settings["trends"]
    max_age, min_ccu = int(cfg["new_game_days"]), int(cfg["breakout_min_ccu"])
    in_sorts: dict[str, list[str]] = defaultdict(list)
    for s in snap.get("sorts", []):
        for uid in s["universe_ids"]:
            in_sorts[str(uid)].append(s["name"])
    rows = []
    for uid, g in snap["games"].items():
        cat = catalog.get(uid, {})
        if not cat.get("created"):
            continue
        age = days_between(cat["created"], ctx.day)
        if age > max_age or g["playing"] < min_ccu:
            continue
        rows.append({
            "universe_id": int(uid), "place_id": cat.get("root_place_id"), "name": cat.get("name"),
            "genre": genre_of(cat),
            "creator": cat.get("creator_name"), "age_days": max(0, age), "ccu_now": g["playing"],
            "visits": g["visits"], "visits_per_day": round(g["visits"] / max(1, age)),
            "favorites": g["favorites"], "first_seen": cat.get("first_seen"),
            "charts": sorted(set(in_sorts.get(uid, [])))[:4],
        })
    return sorted(rows, key=lambda r: -r["ccu_now"])[: int(cfg["top_n"])]


def chart_leaders(snap: dict, catalog: dict, per_sort: int = 3) -> list[dict]:
    """Top organic entries of each chart, as Roblox ranks them."""
    out = []
    for s in snap.get("sorts", []):
        ads = set(s.get("sponsored") or [])
        top = [u for u in s["universe_ids"] if u not in ads][:per_sort]
        out.append({"sort_id": s["id"], "name": s["name"], "games": [
            {"universe_id": u, "place_id": catalog.get(str(u), {}).get("root_place_id"),
             "name": catalog.get(str(u), {}).get("name", str(u)),
             "ccu_now": snap["games"].get(str(u), {}).get("playing")} for u in top]})
    return out
