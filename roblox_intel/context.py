"""Run context shared by every stage, plus per-source health tracking."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ROOT, data_dir, load_settings
from .http import HttpClient
from .roblox import Roblox
from .storage import Store
from .util import utcnow

log = logging.getLogger("roblox_intel")


@dataclass
class Health:
    """Records whether each data source worked on this run, and what we fell back to."""

    sources: dict[str, dict[str, Any]] = field(default_factory=dict)

    def _set(self, key: str, label: str, status: str, detail: str, fallback: str = "") -> None:
        self.sources[key] = {"id": key, "label": label, "status": status, "detail": detail,
                             "fallback": fallback}
        line = f"[{status.upper():4}] {label}: {detail}" + (f" -> fallback: {fallback}" if fallback else "")
        (log.info if status == "ok" else log.warning)(line)

    def ok(self, key: str, label: str, detail: str = "") -> None:
        self._set(key, label, "ok", detail)

    def warn(self, key: str, label: str, detail: str, fallback: str = "") -> None:
        self._set(key, label, "warn", detail, fallback)

    def fail(self, key: str, label: str, detail: str, fallback: str = "") -> None:
        self._set(key, label, "fail", detail, fallback)

    def problems(self) -> list[dict[str, Any]]:
        return [s for s in self.sources.values() if s["status"] != "ok"]

    def as_list(self) -> list[dict[str, Any]]:
        return list(self.sources.values())


@dataclass
class Context:
    settings: dict[str, Any]
    store: Store
    http: HttpClient
    roblox: Roblox
    day: str
    now: datetime
    root: Path = ROOT
    health: Health = field(default_factory=Health)


def make_context(day: str | None = None, settings: dict | None = None,
                 data_root: Path | None = None) -> Context:
    settings = settings or load_settings()
    now = utcnow()
    http = HttpClient.from_settings(settings["http"], ROOT)
    return Context(
        settings=settings,
        store=Store(data_root or data_dir()),
        http=http,
        roblox=Roblox(http, settings["endpoints"]),
        day=day or now.date().isoformat(),
        now=now,
    )
