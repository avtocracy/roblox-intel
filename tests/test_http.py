import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from roblox_intel.http import HttpClient, HttpError


class Recorder(BaseHTTPRequestHandler):
    hits: list = []
    script: dict = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.hits.append((self.path, dict(self.headers)))
        plan = self.script.get(self.path.split("?")[0], [])
        status, headers, body = plan.pop(0) if len(plan) > 1 else (plan[0] if plan else (404, {}, "nope"))
        raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_POST = do_GET


@pytest.fixture
def server():
    handler = type("H", (Recorder,), {"hits": [], "script": {}})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield handler, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def client(tmp_path, **kw):
    sleeps = []
    c = HttpClient(cache_dir=tmp_path / "cache", min_interval={"default": 0}, sleep=sleeps.append,
                   backoff_base=1, max_backoff=8, **kw)
    return c, sleeps


def test_retries_429_with_retry_after_then_succeeds(server, tmp_path):
    handler, base = server
    handler.script["/x"] = [(429, {"Retry-After": "3"}, "slow down"), (503, {}, "busy"), (200, {}, {"ok": 1})]
    c, sleeps = client(tmp_path, max_retries=3)
    assert c.get_json(base + "/x", ttl=0) == {"ok": 1}
    assert sleeps[0] == 3.0, "Retry-After is honoured"
    assert 2 <= sleeps[1] <= 3, "then exponential backoff with jitter"
    stats = c.summary()[base.split("//")[1]]
    assert stats["retries"] == 2 and stats["requests"] == 3


def test_gives_up_after_max_retries(server, tmp_path):
    handler, base = server
    handler.script["/down"] = [(503, {}, "down")]
    c, sleeps = client(tmp_path, max_retries=2)
    with pytest.raises(HttpError) as err:
        c.get_text(base + "/down", ttl=0)
    assert err.value.status == 503 and len(sleeps) == 2


def test_client_errors_are_not_retried(server, tmp_path):
    handler, base = server
    handler.script["/missing"] = [(404, {}, "no such universe")]
    c, sleeps = client(tmp_path)
    with pytest.raises(HttpError) as err:
        c.get_json(base + "/missing")
    assert err.value.status == 404 and "no such universe" in str(err.value) and sleeps == []


def test_cache_hit_and_etag_revalidation(server, tmp_path):
    handler, base = server
    handler.script["/feed"] = [(200, {"ETag": '"v1"'}, "hello"), (304, {}, "")]
    c, _ = client(tmp_path)
    assert c.get_text(base + "/feed", ttl=3600) == "hello"
    assert c.get_text(base + "/feed", ttl=3600) == "hello"
    assert len(handler.hits) == 1, "second call served from the disk cache"
    assert c.get_text(base + "/feed", ttl=1e-9) == "hello"  # stale -> conditional request -> 304
    assert handler.hits[-1][1].get("If-None-Match") == '"v1"'


def test_cache_disabled_globally(server, tmp_path):
    handler, base = server
    handler.script["/a"] = [(200, {}, "one")]
    c, _ = client(tmp_path, cache_ttl=0)
    c.get_text(base + "/a", ttl=86400)
    c.get_text(base + "/a", ttl=86400)
    assert len(handler.hits) == 2


def test_label_hides_secret_urls(server, tmp_path):
    handler, base = server
    handler.script["/api/webhooks/1/SECRET"] = [(401, {}, "bad token")]
    c, _ = client(tmp_path)
    with pytest.raises(HttpError) as err:
        c.post_json(base + "/api/webhooks/1/SECRET", {"content": "x"}, label="Discord webhook")
    assert "SECRET" not in str(err.value) and "Discord webhook" in str(err.value)


def test_throttle_spaces_requests_per_host(server, tmp_path):
    handler, base = server
    handler.script["/t"] = [(200, {}, "ok")]
    c, sleeps = client(tmp_path)
    c.min_interval = {"default": 5.0}
    c.get_text(base + "/t", ttl=0)
    c.get_text(base + "/t", ttl=0)
    assert sleeps and 4.0 < sleeps[0] <= 5.0


def test_unreachable_host_fails_fast_and_trips_the_breaker(tmp_path):
    c, sleeps = client(tmp_path, max_retries=5, connect_retries=1, breaker_threshold=2)
    dead = "http://127.0.0.1:9/nothing-listens-here"
    for _ in range(2):
        with pytest.raises(HttpError) as err:
            c.get_text(dead, ttl=0)
        assert "attempts" in str(err.value) and "ConnectionError" in str(err.value)
    assert len(sleeps) == 2, "connection errors get connect_retries, not max_retries"
    with pytest.raises(HttpError) as err:
        c.get_text(dead + "/again", ttl=0)
    assert "skipped" in str(err.value) and "unreachable" in str(err.value) and len(sleeps) == 2


def test_breaker_resets_after_success(server, tmp_path):
    handler, base = server
    handler.script["/flaky"] = [(503, {}, "x"), (200, {}, "ok")]
    c, _ = client(tmp_path, max_retries=0, breaker_threshold=2)
    with pytest.raises(HttpError):
        c.get_text(base + "/flaky", ttl=0)
    assert c.get_text(base + "/flaky", ttl=0) == "ok"
    assert c._host_failures[base.split("//")[1]] == 0
