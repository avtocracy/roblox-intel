"""JSON history store kept in the repo (data/).

Layout:
  snapshots/YYYY-MM-DD.json   one file per day: chart placements + stats per game
  games.json                  catalog: latest metadata per universe id
  events.json                 developer moves log
  platform.json               platform change log (summarized posts)
  state/*.json                bookkeeping (resolved developers, seen posts, ...)
  reports/YYYY-MM-DD.{json,md} the daily report as sent
  health.json                 status of every data source on the last run

Big maps and logs are written one record per line so git diffs stay readable
and the repo grows by roughly one small file per day.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_DATE_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_record_collection(value: Any) -> bool:
    if isinstance(value, dict):
        return len(value) > 3 and all(isinstance(v, dict) for v in value.values())
    if isinstance(value, list):
        return len(value) > 3 and all(isinstance(v, dict) for v in value)
    return False


def dump_json(value: Any, _nested: bool = False) -> str:
    """Serialize with one record per line (top level, plus one nested level of
    record collections such as snapshot["games"])."""
    end = "" if _nested else "\n"
    if isinstance(value, list):
        if not value:
            return "[]" + end
        return "[\n" + ",\n".join(_dumps(v) for v in value) + "\n]" + end
    if isinstance(value, dict):
        if not value:
            return "{}" + end
        rows = []
        for key in sorted(value, key=str):
            item = value[key]
            if not _nested and _is_record_collection(item):
                inner = dump_json(item, _nested=True)
            else:
                inner = _dumps(item)
            rows.append(f"{json.dumps(str(key))}:{inner}")
        return "{\n" + ",\n".join(rows) + "\n}" + end
    return _dumps(value) + end


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._snapshot_cache: dict[str, dict | None] = {}

    # ---- generic ------------------------------------------------------------

    def path(self, rel: str) -> Path:
        return self.root / rel

    def read(self, rel: str, default: Any = None) -> Any:
        p = self.path(rel)
        if not p.exists():
            return default
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def write(self, rel: str, value: Any) -> Path:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(dump_json(value), encoding="utf-8")
        os.replace(tmp, p)
        return p

    def write_text(self, rel: str, text: str) -> Path:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    # ---- snapshots ----------------------------------------------------------

    def snapshot_dates(self) -> list[str]:
        d = self.path("snapshots")
        if not d.exists():
            return []
        return sorted(m.group(1) for f in d.iterdir() if (m := _DATE_FILE.match(f.name)))

    def load_snapshot(self, day: str) -> dict | None:
        if day not in self._snapshot_cache:
            self._snapshot_cache[day] = self.read(f"snapshots/{day}.json")
        return self._snapshot_cache[day]

    def save_snapshot(self, day: str, snapshot: dict) -> Path:
        self._snapshot_cache[day] = snapshot
        return self.write(f"snapshots/{day}.json", snapshot)

    # ---- named documents ----------------------------------------------------

    def catalog(self) -> dict[str, dict]:
        return self.read("games.json", {}) or {}

    def save_catalog(self, catalog: dict[str, dict]) -> None:
        self.write("games.json", catalog)

    def events(self) -> list[dict]:
        return self.read("events.json", []) or []

    def save_events(self, events: list[dict]) -> None:
        self.write("events.json", sorted(events, key=lambda e: (e.get("date", ""), e.get("id", ""))))

    def platform_items(self) -> list[dict]:
        return self.read("platform.json", []) or []

    def save_platform_items(self, items: list[dict]) -> None:
        self.write("platform.json", sorted(items, key=lambda i: (i.get("published", ""), i.get("id", ""))))

    def state(self, name: str) -> dict:
        return self.read(f"state/{name}.json", {}) or {}

    def save_state(self, name: str, value: dict) -> None:
        self.write(f"state/{name}.json", value)
