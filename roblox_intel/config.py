"""Settings loading: built-in defaults deep-merged with config/settings.yml."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

# Base URLs for every external service. Tests point these at a local mock server.
ENDPOINTS = {
    "games": "https://games.roblox.com",
    "apis": "https://apis.roblox.com",
    "thumbnails": "https://thumbnails.roblox.com",
    "users": "https://users.roblox.com",
    "groups": "https://groups.roblox.com",
    "www": "https://www.roblox.com",
}

DEFAULTS: dict[str, Any] = {
    "report": {"dashboard_url": ""},
    "http": {
        "user_agent": "RobloxIntel/1.0 (personal market-intel tool; daily, low volume)",
        "timeout_seconds": 30,
        "max_retries": 5,
        "connect_retries": 2,
        "backoff_base_seconds": 2,
        "max_backoff_seconds": 90,
        "breaker_threshold": 3,
        "cache_dir": ".cache/http",
        "cache_ttl_seconds": 21600,
        "min_interval_seconds": {"default": 1.0},
    },
    "endpoints": ENDPOINTS,
    "trends": {
        "charts": {
            "device": "computer",
            "country": "all",
            "max_sorts": 40,
            "max_pages_per_sort": 2,
            "extra_params": {},
        },
        "keep_tracking_days": 30,
        "min_ccu_for_growth": 1000,
        "min_base_ccu": 100,
        "new_game_days": 60,
        "breakout_min_ccu": 1000,
        "top_n": 10,
        "exclude_sponsored": True,
    },
    "developers": {
        "max_games_per_developer": 100,
        "min_visits_to_track": 1000,
        "report_update_min_ccu": 500,
        "new_game_min_ccu": 100,
        "big_move_pct": 25,
        "big_move_min_ccu": 1000,
        "recent_release_days": 30,
        "resolve_refresh_days": 7,
    },
    "platform": {
        "first_run_lookback_days": 14,
        "max_new_items_per_source": 25,
        "sources": [],
    },
    "summarizer": {
        "provider": "auto",
        "github_models": {
            "endpoint": "https://models.github.ai/inference/chat/completions",
            "models": ["openai/gpt-4.1-mini", "openai/gpt-4o-mini"],
            "max_calls_per_run": 25,
            "min_interval_seconds": 4.5,
        },
    },
    "discord": {
        "username": "Roblox Intel",
        "max_trend_items": 5,
        "max_developer_items": 5,
        "max_platform_items": 5,
        "platform_min_importance": "high",
    },
    "dashboard": {"output_dir": "_site", "top_games": 20, "history_days": 180},
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_yaml(path: Path) -> Any:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_settings(path: Path | None = None) -> dict[str, Any]:
    """Load settings; RI_SETTINGS can point at an alternative file."""
    path = path or Path(os.environ.get("RI_SETTINGS", ROOT / "config" / "settings.yml"))
    user = load_yaml(Path(path)) or {}
    if not isinstance(user, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return deep_merge(DEFAULTS, user)


def developers_path() -> Path:
    return Path(os.environ.get("RI_DEVELOPERS", ROOT / "config" / "developers.yml"))


def data_dir() -> Path:
    return Path(os.environ.get("RI_DATA_DIR", ROOT / "data"))


def site_src_dir() -> Path:
    return ROOT / "site"


def cache_root(settings: dict[str, Any]) -> Path:
    """Scratch space for non-committed files (HTTP cache, docs repo clone, Discord preview)."""
    cache_dir = Path(settings["http"].get("cache_dir") or ".cache/http")
    return (cache_dir if cache_dir.is_absolute() else ROOT / cache_dir).parent
