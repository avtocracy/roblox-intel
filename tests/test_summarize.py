import json

import pytest

from roblox_intel.config import DEFAULTS, deep_merge
from roblox_intel.http import HttpError
from roblox_intel.summarize import Summarizer, _same_model, extractive_summary, rate


def test_rubric_rates_posts_for_a_solo_developer():
    assert rate("DevEx rate increase for creators", "")[0] == "high"
    assert rate("Updates to Discovery: how Home recommendations work", "")[0] == "high"
    assert rate("New Experience Guidelines and maturity questionnaire", "")[0] == "high"
    assert rate("Release Notes for 700", "Fixes and improvements. Live: faster physics.")[0] == "low"
    assert rate("Introducing a new Studio feature: smarter terrain", "Now available in beta.")[0] == "medium"
    assert rate("Community Spotlight: October game jam winners", "Congrats to everyone!")[0] == "low"
    assert rate("Roblox Reports Third Quarter 2026 Financial Results", "earnings shareholder")[0] == "low"
    assert rate("Weekly Recap: October 12 - 18, 2026", "policy " * 5 + "discovery " * 5 + "payouts " * 5)[0] == "medium"


def test_rubric_for_docs_diffs_needs_a_concrete_change():
    substantive = "Page: production/monetization/developer-exchange.md\n- The rate is $0.0035.\n" \
                  "+ The DevEx rate is $0.0038 per Robux and payouts require 30,000 earned Robux."
    assert rate("Monetization & DevEx docs updated: Developer Exchange", substantive, "docs")[0] == "high"
    wording = "Page: production/promotion/ads.md\n- Click Save.\n+ Select Save to apply your ad campaign settings."
    level, why, tags = rate("Monetization & DevEx docs updated: Ads", wording, "docs")
    assert level == "medium" and "ads" in tags, "area names in the title don't count as evidence"
    assert rate("Publishing & policy docs updated", "Page: x.md\n+ Fixed a typo in the heading text.", "docs")[0] == "low"


def test_extractive_summary_skips_greetings_and_signoffs():
    text = ("Hi Creators,\n\nToday we are launching regional pricing for passes in more countries. "
            "Prices adjust automatically to local purchasing power. You can opt out in the Creator Dashboard.\n\n"
            "Thanks,\nThe Roblox Team")
    summary = extractive_summary("Regional pricing expands", text)
    assert summary.startswith("Today we are launching regional pricing")
    assert "Hi Creators" not in summary and "Roblox Team" not in summary and len(summary) <= 420


# ---- test doubles ------------------------------------------------------------------

class FakeSession:
    def __init__(self, models):
        self.models = models

    def get(self, url, timeout=None):
        if self.models is None:
            raise __import__("requests").ConnectionError("refused")

        class R:
            def json(inner):
                return {"models": [{"name": m} for m in self.models]}
        return R()


class StubHttp:
    def __init__(self, responses, local_models=None):
        self.responses = list(responses)
        self.min_interval = {}
        self.payloads, self.urls, self.timeouts = [], [], []
        self.session = FakeSession(local_models)

    def post_json(self, url, payload, headers=None, retry_statuses=None, label=None, allow_redirects=True,
                  timeout=None):
        self.urls.append(url)
        self.payloads.append(payload)
        self.timeouts.append(timeout)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def reply(content, finish="stop", status=200, headers=None, text=""):
    class R:
        status_code = status

        def __init__(self):
            self.headers = headers or {"Content-Type": "application/json"}
            self.text = text

        def json(self):
            if content is None:
                raise ValueError("not json")
            return {"choices": [{"finish_reason": finish, "message": {"content": content}}]}
    return R()


def ollama(content, model="qwen2.5:3b"):
    class R:
        status_code, headers, text = 200, {"Content-Type": "application/json"}, ""

        def json(self):
            return {"model": model, "message": {"role": "assistant", "content": content}, "done_reason": "stop"}
    return R()


GOOD = json.dumps({"summary": "Payouts change next month.", "importance": "high", "why": "money", "tags": []})


def hosted(**kw):
    cfg = {"provider": "hosted", "hosted": {"endpoint": "https://ai.example/v1/chat/completions",
                                            "models": ["m"], "api_key_env": "K", **kw}}
    return deep_merge(DEFAULTS, {"summarizer": cfg})


def local(**kw):
    return deep_merge(DEFAULTS, {"summarizer": {"provider": "auto", "local": kw}})


# ---- local (Ollama on the runner) --------------------------------------------------

def test_local_model_is_used_when_running():
    http = StubHttp([ollama(GOOD)], local_models=["llama3.2:3b", "qwen2.5:3b"])
    s = Summarizer(local(), http, env={})
    result = s.summarize("DevEx", "x", "src")
    assert result["method"] == "local:qwen2.5:3b" and result["importance"] == "high"
    payload = http.payloads[0]
    assert http.urls[0].endswith("/api/chat") and payload["format"] == "json" and payload["stream"] is False
    assert payload["options"]["num_ctx"] >= 8192 and http.timeouts[0] >= 120
    assert s.label == "local AI on the runner (qwen2.5:3b)"


def test_no_local_model_and_no_key_means_keywords_with_a_reason():
    s = Summarizer(local(), StubHttp([], local_models=None), env={})
    assert s.summarize("DevEx", "x", "src")["method"] == "keywords"
    assert "no local model running" in s.disabled_reason and "hosted AI not configured" in s.disabled_reason
    empty = Summarizer(local(), StubHttp([], local_models=[]), env={})
    empty.summarize("x", "y", "z")
    assert "no model pulled" in empty.disabled_reason


def test_local_time_budget_leaves_the_rest_for_later():
    s = Summarizer(local(max_seconds_per_run=0), StubHttp([], local_models=["qwen2.5:3b"]), env={})
    assert s.summarize("DevEx", "x", "src")["method"] == "keywords"
    assert "time budget" in s.disabled_reason


def test_model_name_matching():
    assert _same_model("llama3.2", "llama3.2:latest") and _same_model("qwen2.5:3b", "qwen2.5:3b")
    assert not _same_model("qwen2.5:3b", "qwen2.5:7b")


# ---- hosted (optional, OpenAI-compatible) ---------------------------------------------

def test_hosted_needs_a_key_and_falls_back_cleanly():
    s = Summarizer(hosted(), StubHttp([]), env={})
    assert s.summarize("x", "y", "z")["method"] == "keywords" and "key not set" in s.disabled_reason


def test_hosted_summary_unknown_model_and_quota():
    http = StubHttp([
        HttpError("m", 404, "HTTP 404: unknown_model"),
        reply('```json\n{"summary": "Rates go up.", "importance": "HIGH", "why": "money", "tags": ["devex"]}\n```'),
        reply(json.dumps({"summary": "Odd.", "importance": "urgent"})),
        HttpError("m", 429, "HTTP 429"),
    ])
    s = Summarizer(hosted(models=["a/missing", "b/ok"]), http, env={"K": "secret"})
    first = s.summarize("DevEx rate increase", "body", "DevForum")
    assert first == {"summary": "Rates go up.", "importance": "high", "why": "money", "tags": ["devex"],
                     "method": "hosted:b/ok"}
    assert [p["model"] for p in http.payloads] == ["a/missing", "b/ok"]
    assert s.summarize("DevEx rate increase", "body", "DevForum")["importance"] == "high", "bad level -> rubric"
    third = s.summarize("Community spotlight", "Congrats.", "DevForum")
    assert third["method"] == "keywords" and "quota" in s.disabled_reason
    assert s.summarize("Another post", "text", "DevForum")["method"] == "keywords" and len(http.payloads) == 4


@pytest.mark.parametrize("status", [401, 403])
def test_hosted_bad_key(status):
    s = Summarizer(hosted(), StubHttp([HttpError("m", status, f"HTTP {status}")]), env={"K": "secret"})
    assert s.summarize("x", "y", "z")["method"] == "keywords" and "refused the API key" in s.disabled_reason


def test_hosted_reasoning_model_out_of_tokens_gets_one_bigger_retry():
    http = StubHttp([reply("", finish="length"), reply(GOOD)])
    s = Summarizer(hosted(max_tokens=1500), http, env={"K": "secret"})
    assert s.summarize("Payouts", "x", "src")["method"] == "hosted:m"
    assert [p["max_tokens"] for p in http.payloads] == [1500, 4500]


def test_hosted_rejected_options_are_dropped_and_retried():
    http = StubHttp([HttpError("m", 400, "HTTP 400: Unsupported value: 'temperature' does not support 0.2"),
                     HttpError("m", 400, "HTTP 400: 'response_format' is not supported with this model"),
                     HttpError("m", 400, "HTTP 400: Unsupported parameter: 'max_tokens'; use max_completion_tokens"),
                     reply(GOOD)])
    s = Summarizer(hosted(), http, env={"K": "secret"})
    assert s.summarize("Payouts", "x", "src")["method"] == "hosted:m"
    last = http.payloads[-1]
    assert "temperature" not in last and "response_format" not in last and last["max_completion_tokens"] == 1500


def test_prose_reply_counts_and_unusable_replies_are_diagnosed():
    prose = "Roblox is raising DevEx rates for eligible creators next month. Importance: high. Check eligibility."
    http = StubHttp([reply(prose), reply(""), reply(""), reply(""), reply(GOOD)])
    s = Summarizer(hosted(), http, env={"K": "secret"})
    first = s.summarize("DevEx", "x", "src")
    assert first["method"] == "hosted:m" and first["importance"] == "high"
    assert "Importance" not in first["summary"] and first["summary"].startswith("Roblox is raising")
    for _ in range(3):
        assert s.summarize("Post", "x", "src")["method"] == "keywords"
    assert "stopped after 3 unusable replies" in s.disabled_reason and "finish_reason='stop'" in s.problem
    assert s.summarize("Post", "x", "src")["method"] == "keywords" and len(http.payloads) == 4


def test_non_json_reply_and_redirects_are_reported_not_followed():
    moved = reply(None, status=302, text="OK\r\n",
                  headers={"Content-Type": "text/plain", "Location": "https://elsewhere.test/x"})
    http = StubHttp([moved] * 3)
    s = Summarizer(hosted(), http, env={"K": "secret"})
    for _ in range(3):
        assert s.summarize("Post", "x", "src")["method"] == "keywords"
    assert "raw='OK\\r\\n'" in s.problem and "HTTP 302" in s.problem and "elsewhere.test" in s.problem
    assert set(http.urls) == {"https://ai.example/v1/chat/completions"}, "the API key is never sent elsewhere"


def test_local_high_ratings_need_evidence_in_the_post():
    hype = json.dumps({"summary": "Adds debug tints in Studio.", "importance": "high",
                       "why": "affects DevEx rates", "tags": ["devex", "monetization"]})
    http = StubHttp([ollama(hype), ollama(GOOD)], local_models=["qwen2.5:3b"])
    s = Summarizer(local(), http, env={})
    tint = s.summarize("SLIM debug tints in Studio", "Enable tints via the Developer Console to verify SLIM.", "src")
    assert tint["importance"] == "medium" and "DevEx" not in tint["why"] and "devex" not in tint["tags"]
    devex = s.summarize("DevEx rate increase", "The DevEx rate rises to $0.0040 per Robux for eligible creators.", "src")
    assert devex["importance"] == "high", "kept when the post really is about money"
