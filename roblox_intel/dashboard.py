"""Build the static GitHub Pages dashboard: copy site/ and write data/*.json."""

from __future__ import annotations

import json
import shutil
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import ROOT, site_src_dir
from .context import Context
from .developers import Developer, suggestions
from .report import dashboard_url
from .trends import analyze, find_base_day, genre_of
from .util import days_between, pct_change, shift_day


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _profile_url(kind: str, cid: Any) -> str:
    return (f"https://www.roblox.com/communities/{cid}/" if kind == "group"
            else f"https://www.roblox.com/users/{cid}/profile")


def build(ctx: Context, devs: list[Developer] | None = None, analysis: dict | None = None,
          out_dir: Path | None = None) -> Path:
    cfg = ctx.settings["dashboard"]
    out = Path(out_dir or ROOT / cfg["output_dir"])
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(site_src_dir(), out)
    data_dir = out / "data"
    store = ctx.store
    all_dates = store.snapshot_dates()
    day = ctx.day if ctx.day in all_dates else (all_dates[-1] if all_dates else ctx.day)
    start = shift_day(day, -int(cfg["history_days"]))
    dates = [d for d in all_dates if start <= d <= day]
    snaps = {d: store.load_snapshot(d) or {"games": {}} for d in dates}
    catalog = store.catalog()
    latest = snaps[dates[-1]]["games"] if dates else {}
    if analysis is None or analysis.get("date") != day:
        ctx = replace(ctx, day=day)  # e.g. today's snapshot failed: show the latest good day
        analysis = analyze(ctx) if dates else {"available": False}

    def game_meta(uid: str) -> dict:
        cat = catalog.get(uid, {})
        return {"id": int(uid), "name": cat.get("name", uid), "genre": genre_of(cat),
                "subgenre": cat.get("subgenre") or "", "creator": cat.get("creator_name") or "",
                "creator_key": f"{cat.get('creator_type')}:{cat.get('creator_id')}",
                "icon": cat.get("icon") or "", "created": (cat.get("created") or "")[:10],
                "url": f"https://www.roblox.com/games/{cat['root_place_id']}" if cat.get("root_place_id") else ""}

    # ---- player trends: biggest games + everything mentioned in today's movers
    top = sorted(latest, key=lambda u: -latest[u]["playing"])[: int(cfg["top_games"])]
    featured = list(top)
    for win in (analysis.get("windows") or {}).values():
        for key in ("gainers", "losers", "abs_gainers"):
            featured += [str(r["universe_id"]) for r in win.get(key, [])]
    featured += [str(r["universe_id"]) for r in analysis.get("breakouts", [])]
    featured = list(dict.fromkeys(u for u in featured if u in latest))[:80]
    games = []
    for uid in featured:
        row = game_meta(uid)
        row["ccu"] = [snaps[d]["games"].get(uid, {}).get("playing") for d in dates]
        row["visits"] = [snaps[d]["games"].get(uid, {}).get("visits") for d in dates]
        row["favorites"] = latest[uid].get("favorites")
        likes, dislikes = latest[uid].get("likes"), latest[uid].get("dislikes")
        row["rating"] = round(likes / (likes + dislikes) * 100) if likes is not None and (likes + (dislikes or 0)) else None
        row["top"] = uid in top
        games.append(row)
    _write(data_dir / "games.json", {"dates": dates, "games": games})

    # ---- genre totals per day (all tracked games)
    totals: dict[str, list[int]] = defaultdict(lambda: [0] * len(dates))
    counts: dict[str, list[int]] = defaultdict(lambda: [0] * len(dates))
    for i, d in enumerate(dates):
        for uid, g in snaps[d]["games"].items():
            genre = genre_of(catalog.get(uid, {}))
            totals[genre][i] += g["playing"]
            counts[genre][i] += 1
    genres = sorted(({"genre": k, "ccu": v, "games": counts[k]} for k, v in totals.items()),
                    key=lambda r: -(r["ccu"][-1] if r["ccu"] else 0))
    _write(data_dir / "genres.json", {"dates": dates, "genres": genres})

    # ---- movers (today's analysis)
    _write(data_dir / "movers.json", analysis)

    # ---- developers
    state = store.state("developers")
    base7 = find_base_day(dates, day, 7, 1)
    dev_rows = []
    keys = [d.key for d in devs] if devs is not None else list(state)
    for key in keys:
        st = state.get(key)
        if not st:
            continue
        uids = list(st.get("games", {}))
        series = [sum(snaps[d]["games"].get(u, {}).get("playing", 0) for u in uids) for d in dates]
        now = series[-1] if series else 0
        week_pct = None
        if base7:  # same games on both days, so newly tracked games don't fake growth
            common = [u for u in uids if u in latest and u in snaps[base7]["games"]]
            week_pct = pct_change(sum(latest[u]["playing"] for u in common),
                                  sum(snaps[base7]["games"][u]["playing"] for u in common))
        best = max(uids, key=lambda u: latest.get(u, {}).get("playing", -1), default=None)
        dev_rows.append({
            "key": key, "name": st.get("name", key), "kind": st.get("kind"), "id": st.get("id"),
            "url": _profile_url(st.get("kind"), st.get("id")), "games": sum(1 for u in uids if u in latest),
            "ccu": now,
            "ccu_7d_pct": week_pct, "series": series,
            "top_game": game_meta(best) | {"ccu": latest.get(best, {}).get("playing")} if best and best in latest else None,
        })
    dev_rows.sort(key=lambda r: -r["ccu"])
    events = [e for e in store.events() if e.get("date", "") >= start]
    events.sort(key=lambda e: (e["date"], e.get("score", 0)), reverse=True)
    sugg = suggestions(ctx, devs or [Developer(k, "", 0, "", "") for k in state], limit=12)
    _write(data_dir / "developers.json", {"developers": dev_rows, "events": events, "suggestions": sugg})

    # ---- platform log
    items = [i for i in store.platform_items() if (i.get("published") or i.get("found") or "")[:10] >= start
             or i.get("found", "") >= start]
    items.sort(key=lambda i: i.get("published", ""), reverse=True)
    _write(data_dir / "platform.json", {"items": items})

    # ---- summary / KPIs
    report = store.read(f"reports/{day}.json") or {}
    prev_day = find_base_day(dates, day, 1, 1)
    total_now = sum(g["playing"] for g in latest.values())
    common = [u for u in latest if prev_day and u in snaps[prev_day]["games"]]
    week_ago = shift_day(day, -7)
    _write(data_dir / "summary.json", {
        "generated_at": ctx.now.isoformat(timespec="seconds"),
        "day": day, "first_day": dates[0] if dates else day, "history_days": len(all_dates),
        "dashboard_url": dashboard_url(ctx.settings),
        "kpis": {
            "tracked_games": len(latest),
            "total_ccu": total_now,
            "total_ccu_prev": sum(snaps[prev_day]["games"][u]["playing"] for u in common) if prev_day else None,
            "total_ccu_now_common": sum(latest[u]["playing"] for u in common) if prev_day else None,
            "total_ccu_series": [sum(g["playing"] for g in snaps[d]["games"].values()) for d in dates],
            "breakouts": len(analysis.get("breakouts") or []),
            "developers": len(dev_rows),
            "dev_moves_7d": sum(1 for e in events if e["date"] > week_ago),
            "platform_high_7d": sum(1 for i in items if i.get("importance") == "high"
                                    and (i.get("found") or "") > week_ago),
            "platform_7d": sum(1 for i in items if (i.get("found") or "") > week_ago),
        },
        "report": report,
        "health": store.read("health.json", {}).get("sources") or report.get("health") or [],
        "days_since_first": days_between(dates[0], day) if dates else 0,
    })
    return out
