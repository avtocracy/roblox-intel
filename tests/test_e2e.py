"""End-to-end: run the real CLI pipeline for 10 simulated days against local mock services.

All numbers come from tests/mock_services.py (synthetic). The test checks that every
stage works together: snapshots, growth windows, developer events, platform monitoring
with AI + keyword summaries, Discord delivery, failure fallbacks and the dashboard.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from roblox_intel import cli, discord
from tests.mock_services import MockServices, day_str

DOC = "content/en-us/production/monetization/developer-exchange.md"


def git(repo: Path, *args: str, when: str | None = None) -> None:
    env = dict(os.environ)
    if when:
        env.update(GIT_AUTHOR_DATE=f"{when}T12:00:00Z", GIT_COMMITTER_DATE=f"{when}T12:00:00Z")
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def commit_doc(repo: Path, body: str, when: str) -> None:
    path = repo / DOC
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: Developer Exchange\n---\n\n{body}\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "update Open Source Docs", when=when)


@pytest.fixture
def env(tmp_path, monkeypatch):
    with MockServices() as mock:
        base = mock.base
        repo = tmp_path / "creator-docs"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "docs@example.test")
        git(repo, "config", "user.name", "docs bot")
        commit_doc(repo, "Initial page.", day_str(-30))
        commit_doc(repo, "The DevEx rate is $0.0035 per Robux for eligible creators.", day_str(-16))
        commit_doc(repo, "The DevEx rate is now $0.0038 per Robux. Creators must have 30,000 earned Robux "
                         "to be eligible for DevEx payouts.", day_str(-2))

        settings = {
            "report": {"dashboard_url": "https://example.github.io/roblox-intel/"},
            "http": {"user_agent": "test", "timeout_seconds": 5, "max_retries": 1, "backoff_base_seconds": 0.01,
                     "max_backoff_seconds": 0.05, "cache_dir": str(tmp_path / "cache" / "http"),
                     "cache_ttl_seconds": 0, "min_interval_seconds": {"default": 0}},
            "endpoints": {"games": f"{base}/games", "apis": f"{base}/apis", "thumbnails": f"{base}/thumbnails",
                          "users": f"{base}/users", "groups": f"{base}/groups", "www": base},
            "platform": {"first_run_lookback_days": 14, "max_new_items_per_source": 25, "sources": [
                {"id": "devforum-announcements", "label": "DevForum · Announcements", "type": "discourse",
                 "base_url": f"{base}/devforum", "category": "updates/announcements", "category_id": 36},
                {"id": "devforum-release-notes", "label": "DevForum · Release Notes", "type": "discourse",
                 "base_url": f"{base}/devforum", "category": "updates/release-notes",
                 "fallback_category": "updates", "title_filter": "^Release Notes"},
                {"id": "roblox-newsroom", "label": "Roblox Newsroom", "type": "newsroom",
                 "base_url": f"{base}/news", "path": "/newsroom"},
                {"id": "creator-docs", "label": "Creator Docs", "type": "git_docs", "repo_url": repo.as_uri(),
                 "areas": {"Monetization & DevEx": ["content/en-us/production/monetization"]}},
                {"id": "devex-faq", "label": "DevEx FAQ", "type": "zendesk_article",
                 "base_url": f"{base}/help", "article_id": 203314100},
            ]},
            "summarizer": {"provider": "github", "github_models": {
                "endpoint": f"{base}/models/inference/chat/completions",
                "models": ["openai/missing-model", "openai/gpt-4.1-mini"], "max_calls_per_run": 25,
                "min_interval_seconds": 0}},
            "dashboard": {"output_dir": str(tmp_path / "site"), "top_games": 20, "history_days": 180},
        }
        (tmp_path / "settings.yml").write_text(yaml.safe_dump(settings, allow_unicode=True), encoding="utf-8")
        (tmp_path / "developers.yml").write_text(yaml.safe_dump({"developers": [
            {"game": "https://www.roblox.com/games/2001/Pet-Tycoon", "expect": "Pet Tycoon"},
            {"game": "https://www.roblox.com/games/2005", "expect": "Roleplay Town"},
            {"user": 601, "name": "SoloDev"},
            {"game": "https://www.roblox.com/games/2003", "expect": "Wrong Name"},  # must be skipped
        ]}), encoding="utf-8")
        monkeypatch.setenv("RI_SETTINGS", str(tmp_path / "settings.yml"))
        monkeypatch.setenv("RI_DEVELOPERS", str(tmp_path / "developers.yml"))
        monkeypatch.setenv("RI_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")
        monkeypatch.delenv("GITHUB_MODELS_TOKEN", raising=False)
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", f"{base}/discord/api/webhooks/123/abc")
        monkeypatch.setattr(discord, "WEBHOOK_RE", re.compile(r"^http://127\.0\.0\.1:\d+/discord/api/webhooks/\d+/\w+$"))
        yield {"mock": mock, "data": tmp_path / "data", "site": tmp_path / "site", "repo": repo}


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def run_day(env, k: int) -> int:
    env["mock"].scenario.k = k
    return cli.main(["run", "--date", day_str(k)])


def events_of(env, kind: str) -> list[dict]:
    return [e for e in load(env["data"] / "events.json") if e["type"] == kind]


def test_ten_days_end_to_end(env):
    mock, data = env["mock"], env["data"]
    sc = mock.scenario

    # ---- day 0: first run, no history yet
    assert run_day(env, 0) == 0
    snap = load(data / f"snapshots/{day_str(0)}.json")
    assert "1007" not in snap["games"], "sponsored chart slots must be ignored"
    assert {"1001", "1002", "1003", "1005"} <= set(snap["games"])
    assert "1006" not in snap["games"], "tiny test places are not tracked"
    assert [s["id"] for s in snap["sorts"]] == ["top-trending", "up-and-coming", "top-playing-now"]
    assert snap["sorts"][0]["universe_ids"][-1] == 1003, "second page of a sort is followed"
    catalog = load(data / "games.json")
    assert catalog["1005"]["genre"] == "Roleplay & Avatar Sim"
    assert catalog["1002"]["icon"].startswith("https://tr.rbxcdn.com/")
    report = load(data / f"reports/{day_str(0)}.json")
    assert any(h["kind"] == "chart_leader" for h in report["highlights"])
    assert "24h from" in report["history_note"]
    health = {h["id"]: h for h in load(data / "health.json")["sources"]}
    assert health["developers-config"]["status"] == "warn" and "Wrong Name" in health["developers-config"]["detail"]
    assert health["charts"]["status"] == "ok"
    assert health["discord"]["status"] == "ok"
    resolved = load(data / "state/resolved.json")
    assert resolved["place:2001"]["creator_id"] == 502
    platform = {p["id"]: p for p in load(data / "platform.json")}
    assert "devforum-announcements:9001" in platform, "recent announcement summarized"
    assert "devforum-announcements:9002" not in platform, "posts older than the first-run window are skipped"
    assert "devforum-announcements:9000" not in platform, "category 'About' topics are skipped"
    assert "devforum-release-notes:9101" in platform, "release-notes category resolved via categories.json"
    assert "roblox-newsroom:creator-earnings-update" in platform
    assert platform["roblox-newsroom:creator-earnings-update"]["title"] == "Creator earnings reach a new high"
    docs = [p for p in platform.values() if p["source"] == "creator-docs"]
    assert len(docs) == 1 and docs[0]["kind"] == "docs" and "Developer Exchange" in docs[0]["title"]
    assert not [p for p in platform.values() if p["source"] == "devex-faq"], "first run only saves a baseline"
    assert platform["devforum-announcements:9001"]["importance"] == "high"
    assert platform["devforum-announcements:9001"]["method"] == "github-models:openai/gpt-4.1-mini", \
        "unknown model skipped, next model used"
    assert len(sc.discord_payloads) == 1
    first = sc.discord_payloads[0]
    assert first["allowed_mentions"] == {"parse": []}
    assert sum(len(e.get("description", "")) + len(e.get("title", "")) for e in first["embeds"]) <= 6000
    assert any("Platform changes" in e["title"] for e in first["embeds"])

    # ---- day 1: 24h growth + Roleplay Town update
    assert run_day(env, 1) == 0
    analysis = load(env["site"] / "data/movers.json")
    win = analysis["windows"]["24h"]
    assert win["available"] and win["gainers"][0]["name"] == "Pet Tycoon"
    assert round(win["gainers"][0]["ccu_pct"]) == 50
    assert win["losers"][0]["name"] == "Battle Arena"
    updates = events_of(env, "game_updated")
    assert [e["game"] for e in updates] == ["Roleplay Town"] and updates[0]["date"] == day_str(1)
    report = load(data / f"reports/{day_str(1)}.json")
    assert report["highlights"][0]["kind"] == "gainer_24h"

    # ---- day 2
    assert run_day(env, 2) == 0

    # ---- day 3: Tycoon Bros releases Fresh Hit; DevEx FAQ text changes
    assert run_day(env, 3) == 0
    new_games = events_of(env, "new_game")
    assert [e["game"] for e in new_games] == ["Fresh Hit"], "quiet placeholder (Secret Project) not announced yet"
    assert new_games[0]["developer"] == "Tycoon Bros" and new_games[0]["date"] == day_str(3)
    analysis = load(env["site"] / "data/movers.json")
    assert [b["name"] for b in analysis["breakouts"]] == ["Fresh Hit"]
    faq = [p for p in load(data / "platform.json") if p["source"] == "devex-faq"]
    assert len(faq) == 1 and faq[0]["kind"] == "page_change" and faq[0]["importance"] == "high"
    assert "Developer Exchange" in faq[0]["title"]

    # ---- day 4: a new announcement appears
    assert run_day(env, 4) == 0
    assert any(p["id"] == "devforum-announcements:9003" for p in load(data / "platform.json"))

    # ---- day 5: formatting-only docs commit is ignored
    commit_doc(env["repo"], "The DevEx rate is now $0.0038 per Robux.  Creators must have 30,000 earned Robux "
                            "to be eligible for DevEx payouts.", day_str(5))
    assert run_day(env, 5) == 0
    assert len([p for p in load(data / "platform.json") if p["source"] == "creator-docs"]) == 1

    # ---- day 6: Charts API down -> fallback to tracked games, reported
    sc.fail_paths.add("/apis/explore-api/v1/get-sorts")
    payloads_before = len(sc.discord_payloads)
    assert run_day(env, 6) == 0
    sc.fail_paths.clear()
    secret = [e for e in events_of(env, "new_game") if e["game"] == "Secret Project"]
    assert len(secret) == 1 and secret[0]["date"] == day_str(6), \
        "a placeholder listed on day 2 is announced only once it draws players"
    assert secret[0]["ccu"] == 2000
    snap6 = load(data / f"snapshots/{day_str(6)}.json")
    assert snap6["sorts"] == [] and {"1002", "1005"} <= set(snap6["games"]), "kept tracking known games"
    health = {h["id"]: h for h in load(data / "health.json")["sources"]}
    assert health["charts"]["status"] == "fail" and health["charts"]["fallback"]
    issues = [e for e in sc.discord_payloads[payloads_before]["embeds"] if "issues" in e["title"]]
    assert issues and "Charts" in issues[0]["description"]

    # ---- day 7: 7-day window opens
    assert run_day(env, 7) == 0
    analysis = load(env["site"] / "data/movers.json")
    assert analysis["windows"]["7d"]["available"] and analysis["windows"]["7d"]["base_day"] == day_str(0)

    # ---- day 8: a week after the update, players rose 60%
    assert run_day(env, 8) == 0
    impact = events_of(env, "update_impact")
    assert len(impact) == 1 and impact[0]["game"] == "Roleplay Town"
    assert round(impact[0]["pct"]) == 60 and "rose 60% in a week" in impact[0]["text"]
    surges = {e["developer"] for e in events_of(env, "player_surge")}
    assert {"RP Inc", "Tycoon Bros"} <= surges
    report = load(data / f"reports/{day_str(8)}.json")
    assert any(m["type"] == "update_impact" for m in report["developer_moves"])

    # same-day re-run must not duplicate anything
    counts = (len(load(data / "events.json")), len(load(data / "platform.json")))
    assert run_day(env, 8) == 0
    assert (len(load(data / "events.json")), len(load(data / "platform.json"))) == counts

    # ---- day 9: game stats API down -> no snapshot, exit code 2, still reported
    sc.fail_paths.add("/games/v1/games")
    assert run_day(env, 9) == 2
    assert not (data / f"snapshots/{day_str(9)}.json").exists()
    assert "No trend data" in sc.discord_payloads[-1]["embeds"][0]["description"]

    # ---- dashboard output
    site = env["site"]
    for name in ("summary", "games", "genres", "movers", "developers", "platform"):
        assert isinstance(load(site / f"data/{name}.json"), dict)
    games = load(site / "data/games.json")
    assert len(games["dates"]) == 9 and games["dates"][-1] == day_str(8)
    pet = next(g for g in games["games"] if g["name"] == "Pet Tycoon")
    assert len(pet["ccu"]) == 9 and pet["ccu"][0] == 50000 and pet["url"].endswith("/2001")
    devs = load(site / "data/developers.json")
    names = {d["name"] for d in devs["developers"]}
    assert {"Tycoon Bros", "RP Inc", "SoloDev"} <= names and "Battle Arena" not in names
    assert any(s["name"] == "Obby Studio" for s in devs["suggestions"]), "untracked owner of a top game suggested"
    for f in ("index.html", "app.js", "style.css"):
        assert (site / f).exists()
    assert sc.llm_calls > 0
