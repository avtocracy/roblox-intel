"""Assemble the daily report (highlights, developer moves, platform changes)."""

from __future__ import annotations

import os
import re
from typing import Any

from .context import Context
from .summarize import LEVELS
from .util import fmt_num, fmt_pct, shift_day, to_date

_MD_SPECIAL = re.compile(r"([\\*_~`|>\[\]])")


def md(text: Any) -> str:
    """Escape Discord/markdown control characters in user-generated names."""
    return _MD_SPECIAL.sub(r"\\\1", str(text or "").strip())


def game_link(row: dict) -> str:
    name = f"**{md(row.get('name'))}**"
    pid = row.get("place_id")
    return f"[{name}](https://www.roblox.com/games/{pid})" if pid else name


def dashboard_url(settings: dict) -> str:
    url = (settings.get("report") or {}).get("dashboard_url") or ""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not url and "/" in repo:
        owner, name = repo.split("/", 1)
        url = (f"https://{owner.lower()}.github.io/" if name.lower() == f"{owner.lower()}.github.io"
               else f"https://{owner.lower()}.github.io/{name}/")
    return url


def trend_highlights(analysis: dict, limit: int = 5) -> list[dict]:
    """Pick the most telling trend facts of the day, without repeating a game."""
    if not analysis.get("available"):
        return []
    cands: list[dict] = []

    def add(priority: float, kind: str, text: str, uid: Any = None) -> None:
        cands.append({"priority": priority, "kind": kind, "text": text, "universe_id": uid})

    windows = analysis.get("windows", {})
    for label, base_prio in (("24h", 100), ("7d", 96), ("30d", 92)):
        win = windows.get(label) or {}
        if not win.get("available"):
            continue
        for i, g in enumerate(win.get("gainers", [])[:2]):
            if (g.get("ccu_pct") or 0) <= 0:
                continue
            add(base_prio - i * 30, f"gainer_{label}",
                f"{game_link(g)} {fmt_pct(g['ccu_pct'])} players in {label} "
                f"({fmt_num(g['ccu_then'])} → {fmt_num(g['ccu_now'])}) · {g['genre']}", g["universe_id"])
        top_abs = (win.get("abs_gainers") or [None])[0]
        if top_abs and top_abs["ccu_delta"] > 0:
            add(base_prio - 25, f"abs_gainer_{label}",
                f"{game_link(top_abs)} added the most players in {label}: +{fmt_num(top_abs['ccu_delta'])} "
                f"(now {fmt_num(top_abs['ccu_now'])})", top_abs["universe_id"])
        loser = (win.get("losers") or [None])[0]
        if loser and (loser.get("ccu_pct") or 0) <= -20:
            add(base_prio - 40, f"loser_{label}",
                f"{game_link(loser)} {fmt_pct(loser['ccu_pct'])} players in {label} "
                f"({fmt_num(loser['ccu_then'])} → {fmt_num(loser['ccu_now'])})", loser["universe_id"])

    for i, b in enumerate(analysis.get("breakouts", [])[:3]):
        add(94 - i * 20, "breakout",
            f"New breakout: {game_link(b)} ({b['genre']}, {b['age_days']} days old) · "
            f"{fmt_num(b['ccu_now'])} playing, ~{fmt_num(b['visits_per_day'])} visits/day", b["universe_id"])

    genre_label = next((l for l in ("7d", "24h", "30d") if (analysis.get("genres") or {}).get(l)), None)
    if genre_label:
        rows = [r for r in analysis["genres"][genre_label] if r.get("games", 0) >= 2]  # one game isn't a genre trend
        up = next((r for r in rows if (r.get("pct") or 0) >= 3), None)
        down = next((r for r in reversed(rows) if (r.get("pct") or 0) <= -3), None)
        if up:
            add(88, "genre_up", f"Genre **{md(up['genre'])}** {fmt_pct(up['pct'])} players over {genre_label} "
                f"({fmt_num(up['ccu_then'])} → {fmt_num(up['ccu_now'])} across {up['games']} games)")
        if down:
            add(70, "genre_down", f"Genre **{md(down['genre'])}** {fmt_pct(down['pct'])} players over {genre_label} "
                f"({fmt_num(down['ccu_then'])} → {fmt_num(down['ccu_now'])})")

    if not any(w.get("available") for w in windows.values()):
        # day one: lean on Roblox's own charts until we have history
        for sort in analysis.get("chart_leaders", []):
            name = sort["name"].lower()
            prio = 66 if "up" in name and "coming" in name else 64 if "trending" in name else None
            if prio and sort["games"]:
                g = sort["games"][0]
                add(prio, "chart_leader", f"#1 on {md(sort['name'])}: {game_link(g)}"
                    + (f" · {fmt_num(g['ccu_now'])} playing" if g.get("ccu_now") is not None else ""),
                    g["universe_id"])
        now = analysis.get("genre_now") or []
        if now:
            top = now[0]
            add(62, "genre_share", f"**{md(top['genre'])}** is the biggest genre: {top['share']}% of tracked "
                f"players ({fmt_num(top['ccu'])} across {top['games']} game{'s' if top['games'] != 1 else ''})")
    for g in analysis.get("biggest", [])[:1]:
        add(40, "biggest", f"Biggest game right now: {game_link(g)} · {fmt_num(g['ccu_now'])} playing",
            g["universe_id"])

    picked, seen_games, genre_items = [], set(), 0
    for c in sorted(cands, key=lambda c: -c["priority"]):
        if c["universe_id"] is not None and c["universe_id"] in seen_games:
            continue
        if c["kind"].startswith("genre"):
            if genre_items >= 2:
                continue
            genre_items += 1
        if c["universe_id"] is not None:
            seen_games.add(c["universe_id"])
        picked.append(c)
        if len(picked) >= limit:
            break
    return picked


def history_note(analysis: dict) -> str:
    windows = analysis.get("windows") or {}
    missing = [l for l in ("24h", "7d", "30d") if not (windows.get(l) or {}).get("available")]
    if not missing or not analysis.get("available"):
        return ""
    first = analysis.get("first_day") or analysis.get("date")
    when = {"24h": 1, "7d": 7, "30d": 30}
    parts = []
    for label in missing:
        day = to_date(shift_day(first, when[label]))
        parts.append(f"{label} from {day.strftime('%b')} {day.day}")
    return "Growth comparisons unlock as history builds: " + ", ".join(parts) + "."


def build(ctx: Context, analysis: dict, dev_events: list[dict], platform_new: list[dict]) -> dict:
    d = ctx.settings["discord"]
    min_level = LEVELS.index(str(d.get("platform_min_importance", "high")))
    counts = {lvl: sum(1 for p in platform_new if p["importance"] == lvl) for lvl in LEVELS}
    report = {
        "date": ctx.day,
        "generated_at": ctx.now.isoformat(timespec="seconds"),
        "dashboard_url": dashboard_url(ctx.settings),
        "history_days": analysis.get("history_days", 0),
        "history_note": history_note(analysis),
        "highlights": trend_highlights(analysis, int(d["max_trend_items"])),
        "developer_moves": [{k: e.get(k) for k in ("text", "type", "developer", "score", "universe_id")}
                            for e in dev_events[: int(d["max_developer_items"])]],
        "developer_moves_total": len(dev_events),
        "platform": {
            "counts": counts,
            "featured": [{k: p.get(k) for k in ("title", "url", "summary", "importance", "why", "source_label")}
                         for p in platform_new if LEVELS.index(p["importance"]) <= min_level
                         ][: int(d["max_platform_items"])],
        },
        "health": ctx.health.as_list(),
    }
    ctx.store.write(f"reports/{ctx.day}.json", report)
    ctx.store.write_text(f"reports/{ctx.day}.md", to_markdown(report))
    return report


def to_markdown(report: dict) -> str:
    lines = [f"# Roblox Intel · {report['date']}", "", "## Trend highlights"]
    lines += [f"{i}. {h['text']}" for i, h in enumerate(report["highlights"], 1)] or ["- No trend data today."]
    if report.get("history_note"):
        lines += ["", f"_{report['history_note']}_"]
    lines += ["", "## Developer moves"]
    lines += [f"- {m['text']}" for m in report["developer_moves"]] or ["- No notable moves today."]
    lines += ["", "## Platform changes"]
    feat = report["platform"]["featured"]
    lines += [f"- **[{p['importance'].upper()}]** [{p['title']}]({p['url']}): {p['summary']}" for p in feat] \
        or ["- No high-importance changes today."]
    c = report["platform"]["counts"]
    lines += ["", f"New posts today: {c['high']} high, {c['medium']} medium, {c['low']} low.", "", "## Source health"]
    lines += [f"- {s['status'].upper()}: {s['label']}: {s['detail']}"
              + (f" (fallback: {s['fallback']})" if s.get("fallback") else "") for s in report["health"]]
    return "\n".join(lines) + "\n"
