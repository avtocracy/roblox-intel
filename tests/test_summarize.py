import json

from roblox_intel.config import DEFAULTS, deep_merge
from roblox_intel.http import HttpError
from roblox_intel.summarize import Summarizer, extractive_summary, rate


def test_rubric_rates_posts_for_a_solo_developer():
    assert rate("DevEx rate increase for creators", "")[0] == "high"
    assert rate("Updates to Discovery: how Home recommendations work", "")[0] == "high"
    assert rate("New Experience Guidelines and maturity questionnaire", "")[0] == "high"
    assert rate("Release Notes for 700", "Fixes and improvements. Live: faster physics.")[0] == "low"
    assert rate("Introducing a new Studio feature: smarter terrain", "Now available in beta.")[0] == "medium"
    assert rate("Community Spotlight: October game jam winners", "Congrats to everyone!")[0] == "low"
    assert rate("Roblox Reports Third Quarter 2026 Financial Results", "earnings shareholder")[0] == "low"


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


class StubHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.min_interval = {}
        self.payloads = []

    def post_json(self, url, payload, headers=None, retry_statuses=None, label=None):
        self.payloads.append(payload)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Resp:
    def __init__(self, content):
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def settings(**gm):
    return deep_merge(DEFAULTS, {"summarizer": {"provider": "auto", "github_models": gm}})


def test_llm_summary_and_fallbacks():
    http = StubHttp([
        HttpError("m", 404, "HTTP 404: unknown_model"),
        Resp('```json\n{"summary": "Rates go up.", "importance": "HIGH", "why": "money", "tags": ["devex"]}\n```'),
        Resp(json.dumps({"summary": "Odd.", "importance": "urgent"})),
        HttpError("m", 429, "HTTP 429"),
    ])
    s = Summarizer(settings(models=["a/missing", "b/ok"]), http, token="t")
    first = s.summarize("DevEx rate increase", "body", "DevForum")
    assert first == {"summary": "Rates go up.", "importance": "high", "why": "money", "tags": ["devex"],
                     "method": "github-models:b/ok"}
    assert [p["model"] for p in http.payloads] == ["a/missing", "b/ok"]
    second = s.summarize("DevEx rate increase", "body", "DevForum")
    assert second["importance"] == "high", "invalid level falls back to the rubric"
    third = s.summarize("Community spotlight", "Congrats.", "DevForum")
    assert third["method"] == "keywords" and "quota" in s.disabled_reason
    fourth = s.summarize("Another post", "text", "DevForum")
    assert fourth["method"] == "keywords" and len(http.payloads) == 4, "no more calls once the quota is hit"


def test_permission_errors_and_missing_token():
    s = Summarizer(settings(), StubHttp([HttpError("m", 403, "HTTP 403")]), token="t")
    assert s.summarize("x", "y", "z")["method"] == "keywords"
    assert "models: read" in s.disabled_reason
    no_token = Summarizer(settings(), StubHttp([]), token="")
    assert not no_token.llm_enabled and no_token.summarize("x", "y", "z")["method"] == "keywords"
    off = Summarizer(deep_merge(DEFAULTS, {"summarizer": {"provider": "none"}}), StubHttp([]), token="t")
    assert not off.llm_enabled
