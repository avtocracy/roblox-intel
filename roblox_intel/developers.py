"""Developer tracker: resolve the watch list, detect releases/updates, measure moves."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import load_yaml
from .context import Context
from .http import HttpError
from .roblox import place_id_from
from .trends import find_base_day
from .util import days_between, fmt_num, fmt_pct, name_matches, parse_iso, pct_change, shift_day, to_date

RESOLVE = ("developers-config", "Tracked developer list (config/developers.yml)")
SCAN = ("developer-games", "Developer game lists (games.roblox.com v2)")


@dataclass
class Developer:
    key: str  # "group:123" or "user:456"
    kind: str
    id: int
    name: str
    via: str  # how the entry was written in developers.yml


# ---- config ------------------------------------------------------------------

def load_entries(path: Path) -> list[dict]:
    data = load_yaml(path) or {}
    entries = data.get("developers") if isinstance(data, dict) else data
    out = []
    for i, entry in enumerate(entries or [], 1):
        if isinstance(entry, dict) and any(k in entry for k in ("game", "group", "user")):
            out.append(entry)
        else:
            raise ValueError(f"{path}: entry #{i} needs a 'game', 'group' or 'user' key: {entry!r}")
    return out


def _stale(rec: dict | None, day: str, refresh_days: int) -> bool:
    return not rec or days_between(rec.get("checked", "1970-01-01"), day) >= refresh_days


def _resolve_game(ctx: Context, place_id: int) -> dict:
    uid = ctx.roblox.place_universe(place_id)
    if not uid:
        raise HttpError(f"place {place_id}", None, "place has no universe")
    info = ctx.roblox.games([uid]).get(uid)
    if not info or not info.get("creator"):
        raise HttpError(f"universe {uid}", None, "game details unavailable")
    creator = info["creator"]
    return {"universe_id": uid, "game": info.get("name", ""), "creator_type": str(creator["type"]).lower(),
            "creator_id": int(creator["id"]), "creator_name": creator.get("name", ""), "checked": ctx.day}


def resolve(ctx: Context, entries: list[dict]) -> list[Developer]:
    """Turn developers.yml entries into concrete groups/users (cached in state/resolved.json)."""
    refresh = int(ctx.settings["developers"]["resolve_refresh_days"])
    state = ctx.store.state("resolved")
    devs: dict[str, Developer] = {}
    problems: list[str] = []
    lookup_failures: dict[str, list[str]] = {}  # reason -> entries, so one outage is one line
    for entry in entries:
        if "game" in entry:
            pid = place_id_from(entry["game"])
            if not pid:
                problems.append(f"can't find a place id in {entry['game']!r}")
                continue
            key = f"place:{pid}"
            rec = state.get(key)
            if _stale(rec, ctx.day, refresh):
                try:
                    rec = state[key] = _resolve_game(ctx, pid)
                except HttpError as exc:
                    if not rec:
                        lookup_failures.setdefault(exc.reason, []).append(f"game {pid}")
                        continue  # keep using the stale record otherwise
            expect = entry.get("expect")
            if expect and not name_matches(rec["game"], str(expect)):
                problems.append(f"game {pid} is now {rec['game']!r}, not {expect!r}: skipped "
                                "(fix the link or the 'expect' text)")
                continue
            kind, cid = rec["creator_type"], rec["creator_id"]
            name = entry.get("name") or rec["creator_name"] or f"{kind} {cid}"
            via = f"game {pid} ({rec['game']})"
        else:
            kind = "group" if "group" in entry else "user"
            try:
                cid = int(entry[kind])
            except (TypeError, ValueError):
                problems.append(f"{kind} id {entry[kind]!r} is not a number")
                continue
            key = f"{kind}:{cid}"
            rec = state.get(key)
            if _stale(rec, ctx.day, refresh):
                try:
                    info = ctx.roblox.creator_info(kind, cid)
                    rec = state[key] = {"name": info.get("name") or info.get("displayName") or "",
                                        "checked": ctx.day}
                except HttpError as exc:
                    if not rec:
                        if entry.get("name"):
                            rec = {"name": entry["name"], "checked": "1970-01-01"}
                        else:
                            lookup_failures.setdefault(exc.reason, []).append(f"{kind} {cid}")
                            continue
            name = entry.get("name") or rec["name"] or f"{kind} {cid}"
            via = f"{kind} {cid}"
        dkey = f"{kind}:{cid}"
        devs.setdefault(dkey, Developer(dkey, kind, int(cid), str(name), via))
    ctx.store.save_state("resolved", state)
    for reason, who in lookup_failures.items():
        problems.append(f"{len(who)} lookup{'s' if len(who) > 1 else ''} failed ({reason}): "
                        + ", ".join(who[:3]) + (" ..." if len(who) > 3 else ""))
    detail = f"{len(devs)} developers from {len(entries)} entries"
    if problems:
        ctx.health.warn(*RESOLVE, detail + "; " + "; ".join(problems[:6]) +
                        (f" (+{len(problems) - 6} more)" if len(problems) > 6 else ""))
    else:
        ctx.health.ok(*RESOLVE, detail)
    return list(devs.values())


# ---- scanning ----------------------------------------------------------------

def scan(ctx: Context, devs: list[Developer]) -> dict[str, list[dict]]:
    """List each developer's public games. `track` marks the ones worth a daily stats
    snapshot (enough visits, or brand new); the rest are only remembered."""
    cfg = ctx.settings["developers"]
    min_visits, recent_days = int(cfg["min_visits_to_track"]), int(cfg["recent_release_days"])
    out: dict[str, list[dict]] = {}
    failed: list[str] = []
    for dev in devs:
        try:
            rows = ctx.roblox.creator_games(dev.kind, dev.id, int(cfg["max_games_per_developer"]))
        except HttpError as exc:
            failed.append(f"{dev.name} ({exc.reason})")
            continue
        games = []
        for r in rows:
            created = r.get("created")
            recent = bool(created) and days_between(created, ctx.day) <= recent_days
            games.append({
                "universe_id": int(r["id"]), "name": r.get("name", ""), "created": created,
                "updated": r.get("updated"), "visits": r.get("placeVisits"),
                "root_place_id": (r.get("rootPlace") or {}).get("id"),
                "track": (r.get("placeVisits") or 0) >= min_visits or recent,
            })
        out[dev.key] = games
    total = sum(1 for v in out.values() for g in v if g["track"])
    if devs and len(failed) == len(devs):
        ctx.health.fail(*SCAN, f"all {len(devs)} lookups failed, e.g. {failed[0]}",
                        fallback="developer moves skipped today")
    elif failed:
        ctx.health.warn(*SCAN, f"{total} games; {len(failed)} lookups failed: {', '.join(failed[:4])}")
    elif devs:
        ctx.health.ok(*SCAN, f"{total} games across {len(out)} developers")
    return out


# ---- event detection -----------------------------------------------------------

def _score(base: float, ccu: int | None) -> float:
    return round(base + 4 * math.log10(max(1, ccu or 0) + 1), 1)


def _event(day: str, kind: str, key: str, dev: str, text: str, score: float, **data: Any) -> dict:
    uid = data.get("universe_id", "")
    return {"id": f"{day}:{kind}:{key}:{uid}", "date": day, "type": kind, "developer": dev,
            "developer_key": key, "text": text, "score": score, **data}


def _later(a: str | None, b: str | None) -> bool:
    da, db = parse_iso(a), parse_iso(b)
    return bool(da and db and da > db)


def detect(ctx: Context, devs: list[Developer], scans: dict[str, list[dict]],
           snapshot: dict | None, ownership_changes: list[dict]) -> list[dict]:
    cfg = ctx.settings["developers"]
    day, store = ctx.day, ctx.store
    catalog = store.catalog()
    state = store.state("developers")
    now_games = (snapshot or {}).get("games", {})
    recent_days = int(cfg["recent_release_days"])
    min_update_ccu = int(cfg["report_update_min_ccu"])
    min_new_ccu = int(cfg.get("new_game_min_ccu", 100))
    min_visits = int(cfg["min_visits_to_track"])

    def ccu(uid: str) -> int | None:
        g = now_games.get(uid)
        return g["playing"] if g else None

    def genre(uid: str) -> str:
        return catalog.get(uid, {}).get("genre") or "unclassified"

    events: list[dict] = []
    for dev in devs:
        rows = scans.get(dev.key)
        if rows is None:
            continue  # lookup failed today: keep yesterday's state untouched
        prev = state.get(dev.key)
        prev_games = (prev or {}).get("games", {})
        current = {str(r["universe_id"]): r for r in rows}
        remembered: dict[str, dict] = {}
        for uid, r in current.items():
            players = ccu(uid)
            age = days_between(r["created"], day) if r.get("created") else None
            fresh = age is not None and age <= recent_days
            # empty placeholders and test places aren't news until they draw players
            drawing = (players or 0) >= min_new_ccu or (r.get("visits") or 0) >= min_visits
            old = prev_games.get(uid)
            announced = bool(old) and old.get("announced", True)
            if not announced:
                if drawing and (prev is not None or fresh):
                    if prev is None:
                        text = f"{dev.name} released {r['name']} ({genre(uid)}) {age} days ago"
                    elif fresh:
                        text = f"{dev.name} released a new game: {r['name']} ({genre(uid)})"
                    elif old is None:
                        text = f"{dev.name} made an older game public: {r['name']} ({genre(uid)})"
                    else:
                        text = f"{dev.name}'s older game {r['name']} ({genre(uid)}) is picking up players"
                    if players is not None:
                        text += f" · {fmt_num(players)} playing"
                    kind = "new_game" if fresh else "game_listed"
                    events.append(_event(day, kind, dev.key, dev.name, text,
                                         _score(85 if fresh else 50, players), universe_id=int(uid),
                                         game=r["name"], genre=genre(uid), ccu=players, age_days=age))
                    announced = True
                elif prev is None and not fresh:
                    announced = True  # first look at this developer: their back catalog is the baseline
            if old:
                if old.get("name") and r["name"] != old["name"]:
                    events.append(_event(day, "renamed", dev.key, dev.name,
                                         f"{dev.name} renamed {old['name']} → {r['name']}",
                                         _score(35, players), universe_id=int(uid), game=r["name"],
                                         old_name=old["name"], ccu=players))
                if _later(r.get("updated"), old.get("updated")) and (players or 0) >= min_update_ccu:
                    events.append(_event(day, "game_updated", dev.key, dev.name,
                                         f"{dev.name} updated {r['name']} · {fmt_num(players)} playing",
                                         _score(25, players), universe_id=int(uid), game=r["name"],
                                         ccu=players, updated=r.get("updated")))
            remembered[uid] = {"name": r["name"], "created": r.get("created"), "updated": r.get("updated"),
                               "announced": announced}
        state[dev.key] = {
            "name": dev.name, "kind": dev.kind, "id": dev.id,
            "since": prev.get("since", day) if prev else day,
            "games": remembered,
        }

    history = store.events()
    events += player_moves(ctx, devs, state, history)
    events += update_impacts(ctx, history + events)
    events += ownership_events(ctx, devs, ownership_changes, now_games)

    known = {e["id"] for e in history}
    merged = history + [e for e in events if e["id"] not in known]
    store.save_events(merged)
    store.save_state("developers", state)
    # report only today's events (a same-day re-run returns the same set)
    todays = [e for e in merged if e["date"] == day]
    return sorted(todays, key=lambda e: -e["score"])


def _total_ccu(snapshot: dict | None, uids: set[str]) -> tuple[int, int] | None:
    if not snapshot:
        return None
    vals = [snapshot["games"][u]["playing"] for u in uids if u in snapshot["games"]]
    return (sum(vals), len(vals)) if vals else None


def player_moves(ctx: Context, devs: list[Developer], state: dict, history: list[dict]) -> list[dict]:
    """Developer-level player change over 7 days (same games on both days)."""
    cfg = ctx.settings["developers"]
    threshold, min_ccu = float(cfg["big_move_pct"]), int(cfg["big_move_min_ccu"])
    store, day = ctx.store, ctx.day
    base_day = find_base_day(store.snapshot_dates(), day, 7, 1)
    if not base_day:
        return []
    now_snap, base_snap = store.load_snapshot(day), store.load_snapshot(base_day)
    recent = {(e["developer_key"], e["type"]) for e in history
              if e["type"] in ("player_surge", "player_drop") and days_between(e["date"], day) < 7}
    out = []
    for dev in devs:
        uids = set(state.get(dev.key, {}).get("games", {}))
        common = {u for u in uids if u in (now_snap or {}).get("games", {}) and u in base_snap["games"]}
        now_t, then_t = _total_ccu(now_snap, common), _total_ccu(base_snap, common)
        if not now_t or not then_t:
            continue
        pct = pct_change(now_t[0], then_t[0])
        if pct is None or abs(pct) < threshold or max(now_t[0], then_t[0]) < min_ccu:
            continue
        kind = "player_surge" if pct > 0 else "player_drop"
        if (dev.key, kind) in recent:
            continue
        verb = "up" if pct > 0 else "down"
        text = (f"{dev.name}: players {verb} {fmt_pct(abs(pct), signed=False)} in a week "
                f"({fmt_num(then_t[0])} → {fmt_num(now_t[0])} across {now_t[1]} game{'s' if now_t[1] != 1 else ''})")
        out.append(_event(day, kind, dev.key, dev.name, text, _score(55 + min(25, abs(pct) / 4), now_t[0]),
                          ccu=now_t[0], ccu_then=then_t[0], pct=round(pct, 1), base_day=base_day))
    return out


def update_impacts(ctx: Context, history: list[dict]) -> list[dict]:
    """A week after an update, report how the game's players moved since then."""
    cfg = ctx.settings["developers"]
    threshold, min_ccu = float(cfg["big_move_pct"]), int(cfg["big_move_min_ccu"])
    store, day = ctx.store, ctx.day
    now_snap = store.load_snapshot(day)
    if not now_snap:
        return []
    target = shift_day(day, -7)
    recent_impacts = {e.get("universe_id") for e in history
                      if e["type"] == "update_impact" and days_between(e["date"], day) < 7}
    out, seen = [], set()
    for e in history:
        if e["type"] != "game_updated" or e["date"] != target:
            continue
        uid = e.get("universe_id")
        if uid in recent_impacts or uid in seen:
            continue
        seen.add(uid)
        before = e.get("ccu")
        now = now_snap["games"].get(str(uid), {}).get("playing")
        pct = pct_change(now, before)
        if pct is None or abs(pct) < threshold or max(now or 0, before or 0) < min_ccu:
            continue
        when = to_date(e["date"]).strftime("%b %d").replace(" 0", " ")
        verb = "rose" if pct > 0 else "fell"
        text = (f"{e['developer']} updated {e.get('game')} on {when}; players {verb} "
                f"{fmt_pct(abs(pct), signed=False)} in a week ({fmt_num(before)} → {fmt_num(now)})")
        out.append(_event(day, "update_impact", e["developer_key"], e["developer"], text,
                          _score(70 + min(20, abs(pct) / 5), now), universe_id=uid, game=e.get("game"),
                          ccu=now, ccu_then=before, pct=round(pct, 1), update_day=e["date"]))
    return out


def ownership_events(ctx: Context, devs: list[Developer], changes: list[dict], now_games: dict) -> list[dict]:
    tracked = {d.key for d in devs}
    min_ccu = int(ctx.settings["developers"]["big_move_min_ccu"])
    out = []
    for ch in changes:
        keys = {f"{ch['from']['type']}:{ch['from']['id']}", f"{ch['to']['type']}:{ch['to']['id']}"}
        players = now_games.get(str(ch["universe_id"]), {}).get("playing")
        if not (keys & tracked) and (players or 0) < min_ccu:
            continue
        new_owner = ch["to"].get("name") or f"{ch['to']['type']} {ch['to']['id']}"
        old_owner = ch["from"].get("name") or f"{ch['from']['type']} {ch['from']['id']}"
        text = f"{ch['game']} changed owner: {old_owner} → {new_owner}"
        if players is not None:
            text += f" · {fmt_num(players)} playing"
        out.append(_event(ctx.day, "ownership_change", f"{ch['to']['type']}:{ch['to']['id']}", new_owner,
                          text, _score(75, players), universe_id=ch["universe_id"], game=ch["game"],
                          ccu=players, previous_owner=old_owner))
    return out


# ---- suggestions -----------------------------------------------------------------

def suggestions(ctx: Context, devs: list[Developer], limit: int = 15) -> list[dict]:
    """Owners of today's biggest games that aren't on the watch list yet."""
    snap = ctx.store.load_snapshot(ctx.day) or {}
    dates = ctx.store.snapshot_dates()
    if not snap and dates:
        snap = ctx.store.load_snapshot(dates[-1]) or {}
    catalog = ctx.store.catalog()
    tracked = {d.key for d in devs}
    best: dict[str, dict] = {}
    for uid, g in (snap.get("games") or {}).items():
        cat = catalog.get(uid, {})
        if not cat.get("creator_id"):
            continue
        key = f"{cat['creator_type']}:{cat['creator_id']}"
        if key in tracked:
            continue
        cur = best.setdefault(key, {"key": key, "kind": cat["creator_type"], "id": cat["creator_id"],
                                    "name": cat.get("creator_name") or key, "ccu": 0, "top_game": None,
                                    "top_ccu": -1})
        cur["ccu"] += g["playing"]
        if g["playing"] > cur["top_ccu"]:
            cur["top_game"], cur["top_ccu"] = cat.get("name"), g["playing"]
    return sorted(best.values(), key=lambda r: -r["ccu"])[:limit]


def suggestion_yaml(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        lines.append(f"  - {r['kind']}: {r['id']}")
        lines.append(f"    name: {json.dumps(str(r['name']), ensure_ascii=False)}"
                     f"  # top game: {r['top_game']} ({fmt_num(r['top_ccu'])} playing)")
    return "\n".join(lines)
