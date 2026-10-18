"""Polite HTTP client: per-host throttling, retries with backoff, on-disk cache."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

import requests

log = logging.getLogger(__name__)

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}


def _short_reason(exc: Exception) -> str:
    """'ProxyError (Tunnel connection failed: 403 Forbidden)' instead of a 400-char urllib3 dump."""
    m = re.search(r"Tunnel connection failed: [^'\")]+|Name or service not known|Temporary failure in name "
                  r"resolution|timed out|Connection refused|Connection reset[^'\")]*|certificate verify failed",
                  str(exc))
    return f"{type(exc).__name__} ({m.group(0).strip()})" if m else f"{type(exc).__name__}: {str(exc)[:120]}"


class HttpError(Exception):
    def __init__(self, url: str, status: int | None, message: str):
        super().__init__(f"{message} ({url})")
        self.url = url
        self.status = status
        self.reason = message  # short form for health reports (no URL)


@dataclass
class HostStats:
    requests: int = 0
    cache_hits: int = 0
    retries: int = 0
    errors: int = 0
    last_error: str = ""


@dataclass
class HttpClient:
    user_agent: str = "RobloxIntel/1.0"
    timeout: float = 30
    max_retries: int = 5
    backoff_base: float = 2.0
    max_backoff: float = 90.0
    cache_dir: Path | None = None
    cache_ttl: int = 21600
    min_interval: dict[str, float] = field(default_factory=lambda: {"default": 1.0})
    connect_retries: int = 2      # DNS/refused/proxy errors rarely heal within seconds
    breaker_threshold: int = 3    # after this many failed requests in a row, skip the host this run
    sleep: Callable[[float], None] = time.sleep
    session: requests.Session = field(default_factory=requests.Session)
    stats: dict[str, HostStats] = field(default_factory=dict)
    _last_request: dict[str, float] = field(default_factory=dict)
    _host_failures: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, http: dict[str, Any], root: Path) -> "HttpClient":
        cache_dir = Path(http.get("cache_dir") or ".cache/http")
        if not cache_dir.is_absolute():
            cache_dir = root / cache_dir
        client = cls(
            user_agent=http.get("user_agent", cls.user_agent),
            timeout=float(http.get("timeout_seconds", 30)),
            max_retries=int(http.get("max_retries", 5)),
            backoff_base=float(http.get("backoff_base_seconds", 2)),
            max_backoff=float(http.get("max_backoff_seconds", 90)),
            connect_retries=int(http.get("connect_retries", 2)),
            breaker_threshold=int(http.get("breaker_threshold", 3)),
            cache_dir=cache_dir,
            cache_ttl=int(http.get("cache_ttl_seconds", 21600)),
            min_interval={k: float(v) for k, v in (http.get("min_interval_seconds") or {}).items()},
        )
        client.prune_cache()
        return client

    def __post_init__(self) -> None:
        self.session.headers.update({"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"})

    # ---- public API -------------------------------------------------------

    def get_json(self, url: str, params: dict | None = None, ttl: int | None = None,
                 headers: dict | None = None) -> Any:
        text = self.get_text(url, params=params, ttl=ttl, headers=headers)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise HttpError(self._full_url(url, params), 200, f"invalid JSON: {exc}") from exc

    def get_text(self, url: str, params: dict | None = None, ttl: int | None = None,
                 headers: dict | None = None) -> str:
        ttl = self.cache_ttl if ttl is None else ttl
        if self.cache_ttl <= 0:  # cache_ttl_seconds: 0 turns caching off everywhere
            ttl = 0
        full = self._full_url(url, params)
        cached = self._cache_read(full)
        host = urlsplit(full).netloc
        if cached and ttl > 0 and time.time() - cached["fetched_at"] < ttl:
            self._stats(host).cache_hits += 1
            return cached["body"]
        req_headers = dict(headers or {})
        if cached:  # stale: revalidate cheaply instead of downloading again
            if cached.get("etag"):
                req_headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                req_headers["If-Modified-Since"] = cached["last_modified"]
        resp = self._request("GET", full, headers=req_headers)
        if resp.status_code == 304 and cached:
            self._stats(host).cache_hits += 1
            cached["fetched_at"] = time.time()
            self._cache_write(full, cached)
            return cached["body"]
        body = resp.text
        if ttl > 0:
            self._cache_write(full, {
                "url": full,
                "fetched_at": time.time(),
                "etag": resp.headers.get("ETag"),
                "last_modified": resp.headers.get("Last-Modified"),
                "body": body,
            })
        return body

    def post_json(self, url: str, payload: Any, headers: dict | None = None,
                  retry_statuses: set[int] | None = None, label: str | None = None,
                  allow_redirects: bool = True, timeout: float | None = None) -> requests.Response:
        """POST without caching. `label` replaces the URL in logs and errors
        (use it for URLs that embed a secret, like Discord webhooks)."""
        return self._request("POST", url, json_body=payload, headers=headers or {},
                             retry_statuses=retry_statuses, label=label, allow_redirects=allow_redirects,
                             timeout=timeout)

    def summary(self) -> dict[str, dict[str, Any]]:
        return {host: vars(s).copy() for host, s in sorted(self.stats.items())}

    # ---- internals --------------------------------------------------------

    @staticmethod
    def _full_url(url: str, params: dict | None) -> str:
        if not params:
            return url
        clean = {k: v for k, v in params.items() if v is not None}
        sep = "&" if "?" in url else "?"
        return url + sep + urlencode(sorted(clean.items()), doseq=True)

    def _stats(self, host: str) -> HostStats:
        return self.stats.setdefault(host, HostStats())

    def _throttle(self, host: str) -> None:
        gap = self.min_interval.get(host, self.min_interval.get("default", 1.0))
        last = self._last_request.get(host)
        if last is not None:
            wait = gap - (time.monotonic() - last)
            if wait > 0:
                self.sleep(wait)
        self._last_request[host] = time.monotonic()

    def _backoff(self, attempt: int, resp: requests.Response | None) -> float:
        if resp is not None:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(self.max_backoff, max(0.0, float(retry_after)))
                except ValueError:
                    try:
                        when = parsedate_to_datetime(retry_after).timestamp()
                        return min(self.max_backoff, max(0.0, when - time.time()))
                    except (TypeError, ValueError):
                        pass
        delay = self.backoff_base * (2 ** attempt)
        return min(self.max_backoff, delay + random.uniform(0, delay / 2))

    def _request(self, method: str, url: str, headers: dict, json_body: Any = None,
                 retry_statuses: set[int] | None = None, label: str | None = None,
                 allow_redirects: bool = True, timeout: float | None = None) -> requests.Response:
        host = urlsplit(url).netloc
        shown = label or url
        stats = self._stats(host)
        if self._host_failures.get(host, 0) >= self.breaker_threshold:
            raise HttpError(shown, None, f"skipped: {host} unreachable this run")
        retry_statuses = RETRY_STATUSES if retry_statuses is None else retry_statuses
        last_problem = ""
        attempts = 0
        for attempt in range(self.max_retries + 1):
            self._throttle(host)
            stats.requests += 1
            attempts += 1
            resp: requests.Response | None = None
            try:
                resp = self.session.request(method, url, headers=headers, json=json_body,
                                            timeout=timeout or self.timeout, allow_redirects=allow_redirects)
            except requests.RequestException as exc:
                last_problem = type(exc).__name__ if label else _short_reason(exc)
                unreachable = isinstance(exc, requests.ConnectionError) and not isinstance(exc, requests.Timeout)
                if unreachable and attempt >= self.connect_retries:
                    break
            else:
                if resp.status_code < 400 or resp.status_code == 304:
                    self._host_failures[host] = 0
                    return resp
                last_problem = f"HTTP {resp.status_code}"
                if resp.status_code not in retry_statuses:
                    self._host_failures[host] = 0  # the host answered; this request was just wrong
                    stats.errors += 1
                    stats.last_error = last_problem
                    snippet = " ".join((resp.text or "")[:160].split())
                    raise HttpError(shown, resp.status_code, f"{last_problem}: {snippet}" if snippet else last_problem)
            if attempt < self.max_retries:
                delay = self._backoff(attempt, resp)
                stats.retries += 1
                log.info("retrying %s %s in %.1fs (%s)", method, shown, delay, last_problem)
                self.sleep(delay)
        stats.errors += 1
        stats.last_error = last_problem
        self._host_failures[host] = self._host_failures.get(host, 0) + 1
        status = int(last_problem.split()[1]) if last_problem.startswith("HTTP ") else None
        raise HttpError(shown, status, f"gave up after {attempts} attempts: {last_problem}")

    # ---- cache ------------------------------------------------------------

    def _cache_path(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self.cache_dir / key[:2] / f"{key}.json"

    def _cache_read(self, url: str) -> dict | None:
        path = self._cache_path(url)
        if not path or not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _cache_write(self, url: str, entry: dict) -> None:
        path = self._cache_path(url)
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(entry), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:  # caching is best-effort
            log.debug("cache write failed for %s: %s", url, exc)

    def prune_cache(self, max_age_days: float = 8) -> None:
        if not self.cache_dir or not self.cache_dir.exists():
            return
        cutoff = time.time() - max_age_days * 86400
        for path in self.cache_dir.glob("*/*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass
