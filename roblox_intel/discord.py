"""Daily Discord message via webhook (URL comes from the DISCORD_WEBHOOK_URL secret)."""

from __future__ import annotations

import re
from typing import Any

from .http import HttpClient, HttpError
from .report import md
from .util import to_date, truncate

WEBHOOK_RE = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/\d+/[\w-]+$")
ICONS = {"high": "🔴", "medium": "🟠", "low": "⚪"}
EMBED_TOTAL_LIMIT = 6000
DESCRIPTION_LIMIT = 4096


def _embed_size(e: dict) -> int:
    return len(e.get("title", "")) + len(e.get("description", "")) + len((e.get("footer") or {}).get("text", ""))


def build_payload(report: dict, settings: dict) -> dict[str, Any]:
    d = settings["discord"]
    day = to_date(report["date"])
    date_label = f"{day:%a} {day:%b} {day.day}"
    url = report.get("dashboard_url") or ""

    trend_lines = [f"{i}. {h['text']}" for i, h in enumerate(report["highlights"], 1)]
    if not trend_lines:
        trend_lines = ["No trend data today; see source health below."]
    if report.get("history_note"):
        trend_lines.append(f"_{report['history_note']}_")

    moves = report["developer_moves"]
    dev_lines = [f"• {m['text']}" for m in moves] or ["No notable moves by tracked developers today."]
    extra = report.get("developer_moves_total", 0) - len(moves)
    if extra > 0:
        dev_lines.append(f"_+{extra} more on the dashboard_")

    plat = report["platform"]
    plat_lines = [f"{ICONS.get(p['importance'], '•')} **[{md(truncate(p['title'], 120))}]({p['url']})**: "
                  f"{md(truncate(p['summary'], 260))}" for p in plat["featured"]]
    floor = str(d.get("platform_min_importance", "high"))
    if not plat_lines:
        plat_lines = [f"No {floor}-importance changes today."]
    c = plat["counts"]
    footer = f"New posts: {c['high']} high · {c['medium']} medium · {c['low']} low"
    if url:
        footer += " · full log on the dashboard"

    embeds = [
        {"title": f"📈 Roblox trends · {date_label}", "description": "\n".join(trend_lines), "color": 0x3B82F6},
        {"title": "🏢 Developer moves", "description": "\n".join(dev_lines), "color": 0x8B5CF6},
        {"title": "🛠️ Platform changes", "description": "\n".join(plat_lines),
         "color": 0xEF4444 if plat["featured"] else 0x64748B, "footer": {"text": footer}},
    ]
    if url:
        embeds[0]["url"] = url
    problems = [s for s in report.get("health", []) if s["status"] != "ok"]
    if problems:
        lines = [f"• {md(s['label'])}: {md(truncate(s['detail'], 140))}"
                 + (f" → {md(s['fallback'])}" if s.get("fallback") else "") for s in problems[:6]]
        embeds.append({"title": "⚠️ Data source issues", "description": "\n".join(lines), "color": 0xF59E0B})

    for e in embeds:
        e["description"] = truncate(e["description"], DESCRIPTION_LIMIT)
    # Discord rejects messages whose embeds total more than 6000 characters
    while sum(_embed_size(e) for e in embeds) > EMBED_TOTAL_LIMIT:
        longest = max(embeds, key=lambda e: len(e["description"]))
        lines = longest["description"].split("\n")
        if len(lines) <= 1:
            longest["description"] = truncate(longest["description"], len(longest["description"]) // 2)
        else:
            longest["description"] = "\n".join(lines[:-1])
    return {
        "username": d.get("username", "Roblox Intel"),
        "content": f"**Roblox Intel** daily brief for {date_label}" + (f" · <{url}>" if url else ""),
        "embeds": embeds,
        "allowed_mentions": {"parse": []},  # never ping @everyone because of a game name
    }


def send(payload: dict, webhook_url: str, http: HttpClient) -> None:
    webhook_url = (webhook_url or "").strip()
    if not WEBHOOK_RE.match(webhook_url.split("?")[0]):
        raise ValueError("DISCORD_WEBHOOK_URL doesn't look like a Discord webhook URL "
                         "(expected https://discord.com/api/webhooks/<id>/<token>)")
    try:
        http.post_json(webhook_url, payload, label="Discord webhook",
                       retry_statuses={429, 500, 502, 503, 504})
    except HttpError as exc:
        hint = {401: "token is wrong", 403: "webhook not allowed", 404: "webhook was deleted",
                400: "Discord rejected the message format"}.get(exc.status or 0, "")
        raise RuntimeError(f"Discord webhook failed: {exc}" + (f" ({hint})" if hint else "")) from None
