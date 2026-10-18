import json
import re
import subprocess

import yaml

from roblox_intel.config import ROOT, load_settings
from roblox_intel.developers import load_entries, suggestion_yaml
from roblox_intel.platform import FETCHERS
from roblox_intel.roblox import place_id_from
from roblox_intel.storage import Store, dump_json
from roblox_intel.util import fmt_num, fmt_pct, html_to_text, name_matches, parse_iso


def test_repo_settings_are_valid():
    s = load_settings(ROOT / "config" / "settings.yml")
    ids = [src["id"] for src in s["platform"]["sources"]]
    assert len(ids) == len(set(ids))
    assert all(src["type"] in FETCHERS for src in s["platform"]["sources"])
    assert s["summarizer"]["provider"] in ("auto", "github", "none")


def test_default_developer_list_parses():
    entries = load_entries(ROOT / "config" / "developers.yml")
    assert len(entries) >= 20
    for e in entries:
        assert place_id_from(e["game"]) and e.get("expect")


def test_suggestion_yaml_is_valid_yaml():
    rows = [{"kind": "group", "id": 7, "name": 'Studio: "Quotes" #1', "top_game": "G", "top_ccu": 1500}]
    parsed = yaml.safe_load("developers:\n" + suggestion_yaml(rows))
    assert parsed == {"developers": [{"group": 7, "name": 'Studio: "Quotes" #1'}]}


def test_dump_json_one_record_per_line(tmp_path):
    store = Store(tmp_path)
    snapshot = {"date": "2026-10-18", "games": {str(i): {"playing": i} for i in range(5)},
                "sorts": [{"id": f"s{i}"} for i in range(4)]}
    store.save_snapshot("2026-10-18", snapshot)
    text = (tmp_path / "snapshots/2026-10-18.json").read_text()
    assert json.loads(text) == snapshot
    assert '"3":{"playing":3},' in text.splitlines()
    assert store.snapshot_dates() == ["2026-10-18"]
    assert dump_json([]) == "[]\n" and json.loads(dump_json([{"a": 1}, {"b": 2}])) == [{"a": 1}, {"b": 2}]


def test_util_helpers():
    assert parse_iso("2026-10-17T21:43:29.3434567Z").microsecond == 343456
    assert parse_iso("garbage") is None
    assert [fmt_num(x) for x in (999, 1000, 999_999, 1_250_000, -2500)] == ["999", "1k", "1M", "1.3M", "-2.5k"]
    assert fmt_pct(12.345) == "+12%" and fmt_pct(-4.21) == "-4.2%" and fmt_pct(None) == "n/a"
    assert name_matches("[🎃 UPDATE] Grow a Garden 🌱", "Grow a Garden")
    assert name_matches("BedWars", "Bed Wars") and not name_matches("Indoors Tycoon", "Doors")
    assert html_to_text("<p>Hello <b>there</b></p><script>x()</script><p>Bye &amp; ciao</p>") == "Hello there\n\nBye & ciao"


def test_site_never_injects_html():
    js = (ROOT / "site" / "app.js").read_text()
    assert "innerHTML" not in js and "outerHTML" not in js and "insertAdjacentHTML" not in js
    html = (ROOT / "site" / "index.html").read_text()
    for ref in re.findall(r'(?:src|href)="([^"#:]+)"', html):
        assert (ROOT / "site" / ref).exists(), ref


def test_site_javascript_parses():
    node = subprocess.run(["node", "--check", str(ROOT / "site" / "app.js")], capture_output=True, text=True)
    assert node.returncode == 0, node.stderr
