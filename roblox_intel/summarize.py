"""Summaries and importance ratings for platform posts.

Two engines:
  * GitHub Models (free tier), called with the workflow's own GITHUB_TOKEN
    (needs `permissions: models: read`). No extra account or key.
  * A built-in extractive summarizer + keyword rubric. Used when no token is
    available, when the free quota runs out, or when provider is "none".
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .http import HttpClient, HttpError
from .util import truncate

LEVELS = ("high", "medium", "low")

SYSTEM_PROMPT = """You brief a solo Roblox developer whose goals are to grow their game's player count and earn more Robux/DevEx.
For each official Roblox post or documentation change you receive, reply with ONLY a JSON object:
{"summary": "...", "importance": "high|medium|low", "why": "...", "tags": ["..."]}

summary: 2-3 plain sentences on what changed and what a developer should do about it. No hype, no markdown.
importance, judged for that solo developer:
  high   = changes money or reach: DevEx rates or eligibility, payouts, revenue share, fees or pricing,
           discovery/recommendation/charts/search changes, policies or ratings that can restrict a game,
           deprecations or deadlines that require action, new ways to earn.
  medium = useful new engine/Studio/Creator Hub features, analytics, ads tools, creator programs, notable fixes.
  low    = routine release notes, minor fixes, community spotlights, events, corporate news, wording tweaks.
why: one short clause explaining the rating.
tags: 1-4 of devex, monetization, discovery, policy, engine, studio, analytics, ads, deprecation, events, corporate."""

# keyword rubric for the offline summarizer: (tag, level, pattern)
RUBRIC: list[tuple[str, str, re.Pattern]] = [
    ("devex", "high", re.compile(r"\bdev ?ex\b|developer exchange|exchange rate|cash ?out", re.I)),
    ("monetization", "high", re.compile(
        r"\bpayouts?\b|revenue share|rev[- ]share|engagement[- ]based|creator rewards|premium payouts|"
        r"\b(fees?|commissions?)\b|regional pricing|price (floor|optimi[sz]ation)|\bpricing\b|"
        r"earn(ed)? robux|new ways to (earn|monetize)|subscriptions?\b", re.I)),
    ("discovery", "high", re.compile(
        r"\bdiscovery\b|recommend(ation|ations|ed for you)|\balgorithm|home ?(page|feed)|\bcharts?\b|"
        r"\bsorts?\b|search (results|ranking)|\branking\b", re.I)),
    ("policy", "high", re.compile(
        r"\bpolic(y|ies)\b|guidelines|terms of (use|service)|community standards|compliance|enforcement|"
        r"maturity|age (checks?|verification|ratings?|estimation|guidelines)|content labels?|questionnaire", re.I)),
    ("deprecation", "high", re.compile(
        r"deprecat|sunset|end of life|no longer (be )?(supported|available)|will be removed|"
        r"breaking change|migrat(e|ion) (by|before)|deadline", re.I)),
    ("ads", "medium", re.compile(r"\bads?\b|advertis|sponsored|rewarded video|immersive ads", re.I)),
    ("analytics", "medium", re.compile(r"analytics|dashboard|insights|retention|funnel|benchmarks?", re.I)),
    ("studio", "medium", re.compile(r"\bstudio\b|creator hub|assistant|open cloud|plugins?", re.I)),
    ("engine", "medium", re.compile(
        r"\bengine\b|\bAPIs?\b|luau|now available|introducing|\blaunch|\bbeta\b|physics|rendering|"
        r"avatar|animation|performance", re.I)),
    ("events", "low", re.compile(r"spotlight|contest|game jam|hackathon|\bevents?\b|meetup|\brdc\b|recap", re.I)),
    ("corporate", "low", re.compile(
        r"earnings|shareholder|financial results|quarter(ly)? (results|report)|board of directors|investor", re.I)),
]
_GREETING = re.compile(r"^(hi|hello|hey|greetings)\b.{0,40}[,!]?$|^(dear )?(creators|developers|everyone)[,!]$", re.I)
_SIGNOFF = re.compile(r"^(thanks|thank you|cheers|happy (building|creating)|best,?|- ?the .* team|the roblox .* team)", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(])")


class LLMUnavailable(Exception):
    pass


def _clean_lines(text: str) -> list[str]:
    out = []
    for raw in (text or "").splitlines():
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", raw)            # markdown images
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)           # markdown links -> text
        line = re.sub(r"<[^>]+>|^\s*[#>*\-+]+\s*|\*\*|__|`", " ", line)  # headings, bullets, emphasis
        line = " ".join(line.split())
        if not line or _GREETING.match(line) or _SIGNOFF.match(line):
            continue
        if re.fullmatch(r"https?://\S+|\d+x\d+\s*\S*|image|click to expand\.*", line, re.I):
            continue
        out.append(line)
    return out


def extractive_summary(title: str, text: str, kind: str = "post", max_chars: int = 420) -> str:
    if kind in ("docs", "page_change"):
        raw = (text or "").splitlines()
        added = [l[2:] for l in raw if l.startswith("+ ")]
        removed = [l[2:] for l in raw if l.startswith("- ")]
        body = " ".join(_clean_lines("\n".join(added or removed)))
        label = "New or changed text" if added else "Removed text"
        return truncate(f"{label}: {body}" if body else f"{title}.", max_chars)
    lines = _clean_lines(text)
    sentences: list[str] = []
    for line in lines:
        for sentence in _SENTENCE.split(line):
            sentence = sentence.strip()
            if len(sentence.split()) >= 6 and sentence not in sentences:
                sentences.append(sentence)
        if len(sentences) >= 3:
            break
    if not sentences:
        return truncate(title, max_chars)
    summary = ""
    for sentence in sentences[:3]:
        candidate = f"{summary} {sentence}".strip()
        if len(candidate) > max_chars and summary:
            break
        summary = candidate
    return truncate(summary, max_chars)


_SUBSTANTIVE = re.compile(
    r"\d+(\.\d+)?\s?%|\$\s?\d|\b\d[\d,]*\s?robux\b|\bmust\b|\brequired?\b|no longer|eligib|deadline|"
    r"\beffective\b|starting (on|in|from)|will (now|no longer|be removed)", re.I)


def rate(title: str, text: str, kind: str = "post") -> tuple[str, str, list[str]]:
    """Keyword rubric -> (importance, why, tags)."""
    if kind in ("docs", "page_change"):
        return _rate_change(text)
    hits_title = {tag for tag, _, rx in RUBRIC if rx.search(title or "")}
    body = (text or "")[:20000]
    hits_body: dict[str, int] = {}
    for tag, _, rx in RUBRIC:
        n = len(rx.findall(body))
        if n:
            hits_body[tag] = n
    level_of = {tag: level for tag, level, _ in RUBRIC}
    tags = sorted(hits_title | set(hits_body), key=lambda t: (LEVELS.index(level_of[t]), -hits_body.get(t, 0)))
    high_title = [t for t in hits_title if level_of[t] == "high"]
    high_body = [t for t, n in hits_body.items() if level_of[t] == "high" and n >= 2]
    strong_body = [t for t, n in hits_body.items() if level_of[t] == "high" and n >= 3]
    is_release_notes = kind == "release_notes" or re.match(r"release notes", title or "", re.I)
    corporate = "corporate" in hits_title
    roundup = re.search(r"\brecap\b|\broundup\b|this week", title or "", re.I)  # mentions everything
    if high_title and not corporate and not roundup:
        level, why = "high", f"title is about {', '.join(sorted(high_title))}"
    elif (len(strong_body) >= 2 and set(strong_body) & {"devex", "monetization", "discovery"}
          and not is_release_notes and not roundup):
        level, why = "high", f"discusses {', '.join(sorted(strong_body))}"
    elif high_body or high_title or any(level_of[t] == "medium" for t in hits_title):
        level, why = "medium", f"touches on {', '.join(sorted(high_body or high_title or hits_title))}"
    elif is_release_notes:
        level, why = "low", "routine release notes"
    elif any(level_of[t] == "medium" for t in hits_body):
        level, why = "medium", "new features or tools"
    else:
        level, why = "low", "no money, reach or policy impact spotted"
    return level, why, tags[:4]


def _rate_change(diff: str) -> tuple[str, str, list[str]]:
    """Docs/help-page diffs: judge only the changed lines, and require a concrete
    rule change (numbers, 'must', eligibility, dates...) before calling it high."""
    changed = "\n".join(l[2:] for l in (diff or "").splitlines() if l[:2] in ("+ ", "- "))
    level_of = {tag: level for tag, level, _ in RUBRIC}
    hits = {tag: len(rx.findall(changed)) for tag, _, rx in RUBRIC}
    hits = {t: n for t, n in hits.items() if n}
    tags = sorted(hits, key=lambda t: (LEVELS.index(level_of[t]), -hits[t]))[:4]
    high = sorted(t for t, n in hits.items() if level_of[t] == "high")
    substantive = _SUBSTANTIVE.search(changed)
    if high and substantive and sum(hits[t] for t in high) >= 2:
        return "high", f"changes {', '.join(high)} rules ('{substantive.group(0).strip()}')", tags
    if high:
        return "medium", f"edits {', '.join(high)} docs", tags
    if any(level_of[t] == "medium" for t in hits):
        return "medium", "documents new or changed features", tags
    return "low", "wording or minor documentation edits", tags


def _extract_json(content: str) -> dict | None:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(json)?|```$", "", content, flags=re.M).strip()
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(content[start:end + 1])
    except json.JSONDecodeError:
        return None


class Summarizer:
    MAX_BAD_REPLIES = 3  # stop calling the model after this many unusable replies in a row

    def __init__(self, settings: dict[str, Any], http: HttpClient, token: str | None = None):
        cfg = settings["summarizer"]
        gm = cfg.get("github_models") or {}
        self.http = http
        self.provider = str(cfg.get("provider", "auto")).lower()
        self.endpoint = gm.get("endpoint", "https://models.github.ai/inference/chat/completions")
        self.catalog_url = gm.get("catalog", "https://models.github.ai/catalog/models")
        self.models = list(gm.get("models") or ["openai/gpt-4.1-mini"])
        self.max_tokens = int(gm.get("max_tokens", 1500))
        self.calls_left = int(gm.get("max_calls_per_run", 25))
        self.token = token if token is not None else (os.environ.get("GITHUB_MODELS_TOKEN")
                                                      or os.environ.get("GITHUB_TOKEN"))
        self.stats = {"llm": 0, "heuristic": 0}
        self.disabled_reason = ""
        self.problem = ""          # last unusable reply, shown in the run's source health
        self.bad_replies = 0
        self._catalog_checked = False
        self._params: dict[str, dict[str, Any]] = {}  # per-model request options, adapted on 400s
        if self.provider == "none":
            self.disabled_reason = "provider set to none"
        elif not self.token:
            self.disabled_reason = "no GITHUB_TOKEN (runs outside GitHub Actions use the keyword summarizer)"
        host = re.sub(r"^https?://([^/]+).*$", r"\1", self.endpoint)
        # the free tier allows ~15 requests/minute
        http.min_interval.setdefault(host, float(gm.get("min_interval_seconds", 4.5)))

    @property
    def llm_enabled(self) -> bool:
        return not self.disabled_reason and self.calls_left > 0

    def summarize(self, title: str, text: str, source: str, kind: str = "post") -> dict[str, Any]:
        result = self.summarize_ai(title, text, source, kind)
        if result:
            return result
        self.stats["heuristic"] += 1
        level, why, tags = rate(title, text, kind)
        return {"summary": extractive_summary(title, text, kind), "importance": level, "why": why,
                "tags": tags, "method": "keywords"}

    def summarize_ai(self, title: str, text: str, source: str, kind: str = "post") -> dict[str, Any] | None:
        """AI summary, or None when AI isn't available for this item."""
        if not self.llm_enabled:
            return None
        try:
            result = self._llm(title, text, source, kind)
        except LLMUnavailable as exc:
            self.disabled_reason = str(exc)
            return None
        if result:
            self.stats["llm"] += 1
        return result

    # ---- GitHub Models ------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def _resolve_models(self) -> None:
        """Keep the configured models GitHub currently offers. If none are offered any more
        (models get retired), fall back to the catalog's most generous free-tier chat models."""
        if self._catalog_checked:
            return
        self._catalog_checked = True
        try:
            catalog = self.http.get_json(self.catalog_url, headers=self._headers(), ttl=86400)
        except HttpError as exc:
            if exc.status in (401, 403):
                raise LLMUnavailable("GitHub Models refused the token (workflow needs "
                                     "`permissions: models: read`)") from exc
            return  # catalog unreachable: try the configured list as is
        rows = catalog if isinstance(catalog, list) else (catalog.get("models") or catalog.get("data") or [])
        offered = {str(m["id"]).lower(): m for m in rows if isinstance(m, dict) and m.get("id")}
        if not offered:
            return
        keep = [m for m in self.models if m.lower() in offered]
        if not keep:
            def chat_model(m: dict) -> bool:
                outputs = [str(x).lower() for x in (m.get("supported_output_modalities") or ["text"])]
                return "text" in outputs and "embed" not in str(m.get("id", "")).lower()
            tier = {"low": 0, "high": 1}
            ranked = sorted((m for m in offered.values() if chat_model(m)),
                            key=lambda m: (tier.get(str(m.get("rate_limit_tier", "")).lower(), 2), str(m["id"])))
            keep = [str(m["id"]) for m in ranked[:3]]
        if keep:
            self.models = keep

    def _complete(self, model: str, messages: list[dict]) -> dict:
        """One chat completion. If the model rejects an option (temperature, max_tokens,
        response_format), drop or rename it and retry once per option."""
        params = self._params.setdefault(model, {"temperature": 0.2, "max_tokens": self.max_tokens,
                                                 "response_format": {"type": "json_object"}})
        for _ in range(4):
            if self.calls_left <= 0:
                raise LLMUnavailable("per-run AI call budget used up")
            self.calls_left -= 1
            try:
                resp = self.http.post_json(self.endpoint, {"model": model, "messages": messages, **params},
                                           headers=self._headers(), retry_statuses={500, 502, 503, 504})
            except HttpError as exc:
                msg = str(exc).lower()
                if exc.status in (400, 422):
                    if "response_format" in msg and "response_format" in params:
                        params.pop("response_format")
                        continue
                    if "temperature" in msg and "temperature" in params:
                        params.pop("temperature")
                        continue
                    if "max_tokens" in msg and "max_tokens" in params:
                        params["max_completion_tokens"] = params.pop("max_tokens")
                        continue
                raise
            try:
                body = resp.json()
            except ValueError:
                body = {"_raw": resp.text[:200]}
            return body if isinstance(body, dict) else {"_raw": str(body)[:200]}
        raise LLMUnavailable(f"{model} rejected every request variant")

    def _llm(self, title: str, text: str, source: str, kind: str) -> dict[str, Any] | None:
        self._resolve_models()
        what = {"docs": "Official documentation change (diff of added/removed lines)",
                "page_change": "Change to an official help page (diff of added/removed lines)",
                "release_notes": "Release notes post"}.get(kind, "Post")
        prompt = (f"Source: {source}\nType: {what}\nTitle: {title}\n\nContent:\n"
                  f"{truncate(text or '(no body text available)', 12000)}")
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
        while self.models:
            model = self.models[0]
            try:
                body = self._complete(model, messages)
            except HttpError as exc:
                message = str(exc).lower()
                if exc.status == 404 or (exc.status in (400, 422) and "model" in message and any(
                        w in message for w in ("unknown", "not found", "unavailable", "invalid", "unsupported"))):
                    self.models.pop(0)  # model not in the catalog -> try the next one
                    continue
                if exc.status in (400, 413, 422):
                    self.problem = f"{model} rejected one post ({exc.reason})"
                    return None  # this post only (e.g. content filter); keyword summary instead
                if exc.status in (401, 403):
                    raise LLMUnavailable("GitHub Models refused the token (workflow needs "
                                         "`permissions: models: read`)") from exc
                if exc.status == 429:
                    raise LLMUnavailable("GitHub Models free quota reached for now") from exc
                raise LLMUnavailable(f"GitHub Models unavailable: {exc.reason}") from exc
            result = self._parse(body, model, title, text, kind)
            if result is None and self._ran_out_of_tokens(body, model):
                continue  # retried below with a bigger output budget
            return result
        raise LLMUnavailable("none of the configured GitHub Models are available")

    def _ran_out_of_tokens(self, body: dict, model: str) -> bool:
        """Reasoning models can spend the whole output budget thinking: allow one bigger retry."""
        choice = (body.get("choices") or [{}])[0] or {}
        params = self._params.get(model, {})
        key = "max_tokens" if "max_tokens" in params else "max_completion_tokens"
        if choice.get("finish_reason") == "length" and params.get(key, 0) < 6000 and not self._content(body):
            params[key] = min(6000, int(params.get(key, self.max_tokens)) * 3)
            self.bad_replies = max(0, self.bad_replies - 1)
            return True
        return False

    @staticmethod
    def _content(body: dict) -> str:
        choices = body.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        content = (choice.get("message") or {}).get("content")
        if isinstance(content, list):  # content parts
            content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        return str(content or "").strip()

    def _parse(self, body: dict, model: str, title: str, text: str, kind: str) -> dict[str, Any] | None:
        content = self._content(body)
        method = f"github-models:{model}"
        data = _extract_json(content) if content else None
        if data and str(data.get("summary", "")).strip():
            self.bad_replies = 0
            level = str(data.get("importance", "")).lower().strip()
            if level not in LEVELS:
                level = rate(title, text, kind)[0]
            tags = [str(t).lower() for t in (data.get("tags") or []) if isinstance(t, str)][:4]
            return {"summary": truncate(" ".join(str(data["summary"]).split()), 600), "importance": level,
                    "why": truncate(str(data.get("why", "")).strip(), 160), "tags": tags, "method": method}
        if len(content) >= 40 and not content.lstrip().startswith("{"):
            self.bad_replies = 0  # a prose answer instead of JSON is still a real summary
            m = re.search(r"importance\W{0,5}(high|medium|low)", content, re.I)
            level = m.group(1).lower() if m else rate(title, text, kind)[0]
            prose = re.sub(r"(?im)^\W*(importance|why|tags)\W.*$", "", content)
            prose = re.sub(r"(?i)\bimportance\W{0,5}(high|medium|low)\b\W*", "", prose)
            return {"summary": truncate(" ".join(re.sub(r"[#*_`>]+", " ", prose).split()), 600),
                    "importance": level, "why": "AI summary (free-form reply)", "tags": [], "method": method}
        choices = body.get("choices") or [{}]
        finish = (choices[0] or {}).get("finish_reason") if isinstance(choices[0], dict) else None
        detail = (f"finish_reason={finish!r}, served by {body.get('model') or model!r}, "
                  f"content={content[:80]!r}")
        if body.get("_raw"):
            detail += f", raw={body['_raw'][:80]!r}"
        usage = body.get("usage") or {}
        if usage:
            detail += f", tokens out={usage.get('completion_tokens')}"
        self.bad_replies += 1
        self.problem = f"{model} replied without a usable summary ({detail})"
        if self.bad_replies >= self.MAX_BAD_REPLIES:
            self.disabled_reason = f"stopped after {self.bad_replies} unusable replies; last: {self.problem}"
        return None
