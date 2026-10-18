"""Command line: python -m roblox_intel <command>."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback
from dataclasses import replace
from pathlib import Path

from . import dashboard, developers, discord, platform, report, trends
from .config import ROOT, cache_root, developers_path, load_settings
from .context import Context, make_context
from .http import HttpClient, HttpError
from .roblox import Roblox
from .summarize import Summarizer

log = logging.getLogger("roblox_intel")


def _stage(ctx: Context, key: str, label: str, fn, default=None):
    """Run one pipeline stage; a crash in one stage never stops the others."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - reported in source health, run continues
        log.error("%s crashed:\n%s", label, traceback.format_exc())
        ctx.health.fail(key, label, f"crashed: {type(exc).__name__}: {exc}")
        return default


def cmd_run(args: argparse.Namespace) -> int:
    ctx = make_context(day=args.date)
    log.info("Roblox Intel run for %s", ctx.day)

    devs: list = []
    scans: dict = {}
    if not args.skip_developers:
        entries = _stage(ctx, "developers-config", "Tracked developer list",
                         lambda: developers.load_entries(developers_path()), [])
        devs = _stage(ctx, "developers-config", "Tracked developer list",
                      lambda: developers.resolve(ctx, entries), [])
        scans = _stage(ctx, "developer-games", "Developer game lists", lambda: developers.scan(ctx, devs), {})
    extra = {g["universe_id"] for rows in scans.values() for g in rows if g.get("track", True)}

    snapshot, ownership = _stage(ctx, "games-api", "Trend snapshot",
                                 lambda: trends.collect(ctx, extra), (None, []))
    dev_events = _stage(ctx, "developer-events", "Developer move detection",
                        lambda: developers.detect(ctx, devs, scans, snapshot, ownership), []) if devs else []
    analysis = _stage(ctx, "trend-analysis", "Trend analysis", lambda: trends.analyze(ctx),
                      {"available": False})
    platform_new = [] if args.skip_platform else _stage(
        ctx, "platform", "Platform monitor", lambda: platform.run(ctx), [])

    daily = report.build(ctx, analysis, dev_events, platform_new)
    payload = discord.build_payload(daily, ctx.settings)
    preview = cache_root(ctx.settings) / "discord-preview.json"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if args.no_discord:
        log.info("Discord skipped (--no-discord); preview in %s", preview)
    elif not webhook:
        ctx.health.warn("discord", "Discord webhook", "DISCORD_WEBHOOK_URL is not set; message not sent",
                        fallback=f"preview saved to {preview}")
    else:
        try:
            discord.send(payload, webhook, ctx.http)
            ctx.health.ok("discord", "Discord webhook", "daily message sent")
        except (ValueError, RuntimeError) as exc:
            ctx.health.fail("discord", "Discord webhook", str(exc))

    # final health (now including Discord) goes into the stored report and health.json
    daily["health"] = ctx.health.as_list()
    ctx.store.write(f"reports/{ctx.day}.json", daily)
    ctx.store.write_text(f"reports/{ctx.day}.md", report.to_markdown(daily))
    ctx.store.write("health.json", {"date": ctx.day, "generated_at": ctx.now.isoformat(timespec="seconds"),
                                    "sources": ctx.health.as_list(), "requests": ctx.http.summary()})
    if not args.no_dashboard:
        out = _stage(ctx, "dashboard", "Dashboard build", lambda: dashboard.build(ctx, devs, analysis))
        if out:
            log.info("dashboard written to %s", out)

    print("\n" + report.to_markdown(daily))
    requests_made = sum(s["requests"] for s in ctx.http.summary().values())
    log.info("HTTP: %d requests, %d cache hits", requests_made,
             sum(s["cache_hits"] for s in ctx.http.summary().values()))
    if snapshot is None:
        log.error("No trend snapshot today (game stats API unreachable). See source health above.")
        return 2
    return 0


def cmd_check_sources(args: argparse.Namespace) -> int:
    """Probe every data source live (no cache) and print a status table."""
    settings = load_settings()
    http = HttpClient.from_settings({**settings["http"], "max_retries": 2}, ROOT)
    http.cache_dir = None
    rb = Roblox(http, settings["endpoints"])
    rows: list[tuple[str, str, str]] = []

    def probe(name: str, fn) -> object:
        try:
            detail, value = fn()
            rows.append(("OK", name, detail))
            return value
        except Exception as exc:  # noqa: BLE001 - every failure is reported, not raised
            rows.append(("FAIL", name, f"{type(exc).__name__}: {exc}"[:220]))
            return None

    def charts():
        sorts = rb.chart_sorts(max_sorts=4, max_pages=1)
        if not sorts:
            raise HttpError("explore-api", None, "no sorts returned")
        first = sorts[0]
        return (f"{len(sorts)} sorts, e.g. '{first.name}' with {len(first.entries)} games",
                [e["universe_id"] for s in sorts for e in s.entries])

    chart_uids = probe("Charts / Discover (explore-api)", charts) or []
    uid = chart_uids[0] if chart_uids else 994732206  # fall back to Blox Fruits' universe

    def games():
        g = rb.games([uid]).get(uid)
        if not g:
            raise HttpError("games", None, f"universe {uid} not returned")
        return (f"{g['name']!r}: {g.get('playing')} playing, {g.get('visits')} visits, "
                f"genre={g.get('genre_l1') or g.get('genre')!r}", g)

    game = probe("Game stats (games/v1/games)", games)
    probe("Likes/dislikes (games/v1/games/votes)", lambda: (f"{rb.votes([uid]).get(uid)}", None))
    probe("Game icons (thumbnails)", lambda: (f"{len(rb.icons([uid]))} icon(s)", None))
    probe("Place -> universe (apis/universes)",
          lambda: (f"place 2753915549 -> universe {rb.place_universe(2753915549)}", None))
    if game and game.get("creator"):
        kind = str(game["creator"]["type"]).lower()
        cid = int(game["creator"]["id"])
        probe(f"Creator games ({kind} {cid})",
              lambda: (f"{len(rb.creator_games(kind, cid, 10))} public games", None))
        probe(f"Creator info ({kind} {cid})", lambda: (repr(rb.creator_info(kind, cid).get('name')), None))

    for src in settings["platform"]["sources"]:
        def fetch(src=src):
            ctx = make_context(settings=settings, data_root=cache_root(settings) / "check-sources")
            ctx = replace(ctx, http=http)
            items, note = platform.FETCHERS[src["type"]](ctx, src, {})
            if src["type"] == "zendesk_article":
                return f"{note}", None
            if not items:
                raise platform.SourceError(f"no items ({note})")
            newest = max(items, key=lambda i: i.published or "")
            newest.load()
            return (f"{len(items)} items; newest {newest.published[:10]} {newest.title[:70]!r} "
                    f"({len(newest.text)} chars)" + (f"; {note}" if note else ""), None)
        probe(f"Platform: {src.get('label', src['id'])}", fetch)

    summarizer = Summarizer(settings, http)
    summarizer._choose_backend()
    if summarizer.backend == "local":
        rows.append(("OK", "Summaries: local AI (Ollama)", f"model {summarizer.model} ready on the runner"))
    elif summarizer.backend == "hosted":
        def hosted():
            method = summarizer.summarize("Test", "Roblox announced a test post for creators.", "check-sources")["method"]
            if method == "keywords":
                raise RuntimeError(f"fell back to keywords: {summarizer.disabled_reason or summarizer.problem}")
            return method, None
        probe("Summaries: hosted AI", hosted)
    else:
        rows.append(("SKIP", "Summaries: AI", f"keyword summaries only ({summarizer.disabled_reason})"))
    hook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if hook:
        ok = bool(discord.WEBHOOK_RE.match(hook.split("?")[0]))
        rows.append(("OK" if ok else "FAIL", "Discord webhook secret",
                     "looks valid (not sent; use discord-test)" if ok else "not a Discord webhook URL"))
    else:
        rows.append(("SKIP", "Discord webhook secret", "DISCORD_WEBHOOK_URL not set"))

    width = max(len(r[1]) for r in rows)
    print(f"\n{'STATUS':6}  {'SOURCE':{width}}  DETAIL")
    for status, name, detail in rows:
        print(f"{status:6}  {name:{width}}  {detail}")
    failed = sum(1 for r in rows if r[0] == "FAIL")
    print(f"\n{len(rows) - failed} of {len(rows)} checks OK" + (f", {failed} FAILED" if failed else ""))
    return 1 if failed else 0


def cmd_suggest(args: argparse.Namespace) -> int:
    ctx = make_context()
    dates = ctx.store.snapshot_dates()
    if not dates:
        print("No snapshots yet: run `python -m roblox_intel run` first.")
        return 1
    ctx.day = dates[-1]
    devs = []
    state = ctx.store.state("resolved")
    for entry in developers.load_entries(developers_path()):
        if "game" in entry:
            rec = state.get(f"place:{developers.place_id_from(entry['game'])}")
            if rec:
                devs.append(developers.Developer(f"{rec['creator_type']}:{rec['creator_id']}", "", 0, "", ""))
        else:
            kind = "group" if "group" in entry else "user"
            devs.append(developers.Developer(f"{kind}:{entry[kind]}", "", 0, "", ""))
    rows = developers.suggestions(ctx, devs, limit=args.limit)
    if not rows:
        print("Every owner of today's top games is already tracked.")
        return 0
    print(f"# Owners of the biggest games on {ctx.day} that you don't track yet.")
    print("# Paste any of these under `developers:` in config/developers.yml\n")
    print(developers.suggestion_yaml(rows))
    return 0


def cmd_build_dashboard(args: argparse.Namespace) -> int:
    ctx = make_context()
    dates = ctx.store.snapshot_dates()
    if dates:
        ctx.day = dates[-1]
    out = dashboard.build(ctx, out_dir=Path(args.out) if args.out else None)
    print(f"dashboard written to {out}")
    return 0


def cmd_discord_test(args: argparse.Namespace) -> int:
    hook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not hook:
        print("Set DISCORD_WEBHOOK_URL first.")
        return 1
    settings = load_settings()
    http = HttpClient.from_settings(settings["http"], ROOT)
    payload = {"username": settings["discord"]["username"], "allowed_mentions": {"parse": []},
               "content": "✅ Roblox Intel is connected. Daily briefs will arrive here."}
    try:
        discord.send(payload, hook, http)
    except (ValueError, RuntimeError) as exc:
        print(exc)
        return 1
    print("Test message sent.")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    ctx = make_context()
    day = args.date or (sorted(p.stem for p in ctx.store.path("reports").glob("*.json"))[-1:] or [None])[0]
    data = ctx.store.read(f"reports/{day}.json") if day else None
    if not data:
        print("No report found.")
        return 1
    print(report.to_markdown(data))
    if args.discord_json:
        print(json.dumps(discord.build_payload(data, ctx.settings), indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="roblox_intel", description="Roblox market intelligence, daily.")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="collect everything, send the Discord brief, build the dashboard")
    p.add_argument("--date", help="treat this UTC date (YYYY-MM-DD) as today")
    p.add_argument("--no-discord", action="store_true", help="don't send; just write the preview")
    p.add_argument("--no-dashboard", action="store_true")
    p.add_argument("--skip-platform", action="store_true")
    p.add_argument("--skip-developers", action="store_true")
    p.set_defaults(fn=cmd_run)

    sub.add_parser("check-sources", help="test every data source live").set_defaults(fn=cmd_check_sources)
    p = sub.add_parser("suggest-developers", help="owners of top games you don't track yet")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(fn=cmd_suggest)
    p = sub.add_parser("build-dashboard", help="rebuild the static site from data/")
    p.add_argument("--out", help="output directory (default: _site)")
    p.set_defaults(fn=cmd_build_dashboard)
    sub.add_parser("discord-test", help="send a test message to the webhook").set_defaults(fn=cmd_discord_test)
    p = sub.add_parser("report", help="print a stored daily report")
    p.add_argument("--date")
    p.add_argument("--discord-json", action="store_true", help="also print the Discord payload")
    p.set_defaults(fn=cmd_report)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stdout)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return int(args.fn(args) or 0)
