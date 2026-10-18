"""Summaries and importance ratings for platform posts.

Three engines, best first:
  * local:  a small open model (default qwen2.5:3b) served by Ollama on the GitHub Actions
            runner itself. Free, no account, no API key; the workflow starts it.
  * hosted: optional, any OpenAI-compatible chat API you have a key for (set in settings.yml,
            key in a repo secret). Off unless configured.
  * keywords: a built-in extractive summarizer + keyword rubric, used whenever no AI is
            available. Keyword summaries are upgraded to AI summaries on a later run.

(GitHub Models, the original free option, was retired by GitHub on July 30, 2026.)
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit

import requests

from .http import HttpClient, HttpError
from .util import truncate

LEVELS = ("high", "medium", "low")

SYSTEM_PROMPT = """You brief a solo Roblox developer whose goals are to grow their game's player count and earn more Robux/DevEx.
For each official Roblox post or documentation change you receive, reply with ONLY a JSON object:
{"summary": "...", "importance": "high|medium|low", "why": "...", "tags": ["..."]}

summary: 2-3 plain sentences on what changed and what a developer should do about it. No hype, no markdown.
Only state effects the text actually says; never invent impacts on money, DevEx or discovery.
importance, judged for that solo developer (most posts are medium or low; high is rare):
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


def calibrate(level: str, why: str, title: str, text: str, kind: str = "post") -> tuple[str, str, list[str]]:
    """Small local models call almost everything "high" and invent reasons. Keep their rating
    only when the post itself shows money/reach/policy evidence; tags come from the rubric."""
    evidence, evidence_why, tags = rate(title, text, kind)
    if level == "high" and evidence != "high":
        return "medium", f"no money, reach or policy change stated ({evidence_why})", tags
    return level, why, tags


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

    def __init__(self, settings: dict[str, Any], http: HttpClient, env: Any = None):
        cfg = settings["summarizer"]
        env = os.environ if env is None else env
        self.http = http
        self.provider = str(cfg.get("provider", "auto")).lower()  # auto | local | hosted | none
        local = cfg.get("local") or {}
        self.local_url = str(local.get("url", "http://127.0.0.1:11434")).rstrip("/")
        self.local_models = [str(m) for m in (local.get("models") or ["qwen2.5:3b", "llama3.2:3b"])]
        self.local_timeout = float(local.get("request_timeout_seconds", 300))
        self.time_budget = float(local.get("max_seconds_per_run", 900))
        hosted = cfg.get("hosted") or {}
        self.hosted_endpoint = str(hosted.get("endpoint") or "")
        self.hosted_models = [str(m) for m in (hosted.get("models") or [])]
        self.hosted_key = str(env.get(str(hosted.get("api_key_env") or "AI_API_KEY"), "") or "")
        self.max_tokens = int(hosted.get("max_tokens", 1500))
        self.calls_left = int(cfg.get("max_calls_per_run", 40))
        self.backend = ""          # "local" or "hosted", picked on first use
        self.model = ""
        self.stats = {"llm": 0, "heuristic": 0}
        self.disabled_reason = "provider set to none" if self.provider == "none" else ""
        self.problem = ""          # last unusable reply, shown in the run's source health
        self.setup_note = ""       # why the local model couldn't be used, if it couldn't
        self.bad_replies = 0
        self._started = time.monotonic()
        self._params: dict[str, dict[str, Any]] = {}  # per-model request options, adapted on 400s
        if self.hosted_endpoint:
            host = urlsplit(self.hosted_endpoint).netloc
            http.min_interval.setdefault(host, float(hosted.get("min_interval_seconds", 4)))

    @property
    def llm_enabled(self) -> bool:
        return not self.disabled_reason and self.calls_left > 0

    @property
    def label(self) -> str:
        if self.backend == "local":
            return f"local AI on the runner ({self.model})"
        if self.backend == "hosted":
            return f"hosted AI ({self.model})"
        return "AI"

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
        self._choose_backend()
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

    # ---- picking a backend --------------------------------------------------------

    def _choose_backend(self) -> None:
        if self.backend or self.disabled_reason:
            return
        if self.provider in ("auto", "local"):
            available = self._local_available()
            if available:
                wanted = [m for m in self.local_models if any(_same_model(m, a) for a in available)]
                self.model = wanted[0] if wanted else available[0]
                self.backend = "local"
                return
        if self.provider in ("auto", "hosted") and self.hosted_endpoint and self.hosted_key and self.hosted_models:
            self.backend, self.model = "hosted", self.hosted_models[0]
            return
        reasons = []
        if self.provider in ("auto", "local"):
            reasons.append(f"no local model running ({self.setup_note or 'not started'})")
        if self.provider in ("auto", "hosted"):
            reasons.append("hosted AI not configured" if not self.hosted_endpoint else "hosted AI key not set")
        self.disabled_reason = "; ".join(reasons)

    def _local_available(self) -> list[str]:
        try:
            resp = self.http.session.get(f"{self.local_url}/api/tags", timeout=5)
            names = [m.get("name") or m.get("model") for m in (resp.json().get("models") or [])]
        except (requests.RequestException, ValueError, AttributeError) as exc:
            self.setup_note = f"{self.local_url} unreachable: {type(exc).__name__}"
            return []
        names = [str(n) for n in names if n]
        if not names:
            self.setup_note = "Ollama is running but has no model pulled"
        return names

    # ---- calls --------------------------------------------------------------------

    def _llm(self, title: str, text: str, source: str, kind: str) -> dict[str, Any] | None:
        if self.backend == "local" and time.monotonic() - self._started > self.time_budget:
            raise LLMUnavailable(f"local AI time budget ({self.time_budget:.0f}s) used up for this run; "
                                 "the rest get AI summaries on a later run")
        what = {"docs": "Official documentation change (diff of added/removed lines)",
                "page_change": "Change to an official help page (diff of added/removed lines)",
                "release_notes": "Release notes post"}.get(kind, "Post")
        limit = 5000 if self.backend == "local" else 12000  # small CPU model: keep prompts short
        prompt = (f"Source: {source}\nType: {what}\nTitle: {title}\n\nContent:\n"
                  f"{truncate(text or '(no body text available)', limit)}")
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
        if self.backend == "local":
            return self._parse(self._complete_local(messages), self.model, title, text, kind)
        while self.hosted_models:
            model = self.hosted_models[0]
            try:
                body = self._complete_hosted(model, messages)
            except HttpError as exc:
                message = str(exc).lower()
                if exc.status == 404 or (exc.status in (400, 422) and "model" in message and any(
                        w in message for w in ("unknown", "not found", "unavailable", "invalid", "unsupported"))):
                    self.hosted_models.pop(0)  # model not offered -> try the next one
                    continue
                if exc.status in (400, 413, 422):
                    self.problem = f"{model} rejected one post ({exc.reason})"
                    return None  # this post only (e.g. content filter); keyword summary instead
                if exc.status in (401, 403):
                    raise LLMUnavailable(f"hosted AI refused the API key ({exc.reason})") from exc
                if exc.status == 429:
                    raise LLMUnavailable("hosted AI free quota reached for now") from exc
                raise LLMUnavailable(f"hosted AI unavailable: {exc.reason}") from exc
            self.model = model
            result = self._parse(body, model, title, text, kind)
            if result is None and self._ran_out_of_tokens(body, model):
                continue  # retried with a bigger output budget
            return result
        raise LLMUnavailable("none of the configured hosted models are available")

    def _complete_local(self, messages: list[dict]) -> dict:
        """Ollama's native chat API: JSON-constrained output, bigger context than the default."""
        if self.calls_left <= 0:
            raise LLMUnavailable("per-run AI call budget used up")
        self.calls_left -= 1
        payload = {"model": self.model, "messages": messages, "stream": False, "format": "json",
                   "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": 600}}
        try:
            resp = self.http.post_json(f"{self.local_url}/api/chat", payload, retry_statuses={500, 502, 503},
                                       timeout=self.local_timeout)
        except HttpError as exc:
            raise LLMUnavailable(f"local model failed: {exc.reason}") from exc
        try:
            data = resp.json()
        except ValueError:
            return {"_raw": resp.text[:200], "_http": f"HTTP {resp.status_code}"}
        message = data.get("message") or {}
        return {"model": data.get("model"), "choices": [
            {"message": {"content": message.get("content", "")}, "finish_reason": data.get("done_reason")}]}

    def _complete_hosted(self, model: str, messages: list[dict]) -> dict:
        """One OpenAI-compatible chat completion. If the model rejects an option (temperature,
        max_tokens, response_format), drop or rename it and retry once per option."""
        params = self._params.setdefault(model, {"temperature": 0.2, "max_tokens": self.max_tokens,
                                                 "response_format": {"type": "json_object"}})
        headers = {"Authorization": f"Bearer {self.hosted_key}", "Accept": "application/json"}
        for _ in range(4):
            if self.calls_left <= 0:
                raise LLMUnavailable("per-run AI call budget used up")
            self.calls_left -= 1
            try:
                # no silent redirects: a redirected POST turns into a GET and returns the wrong thing
                resp = self.http.post_json(self.hosted_endpoint, {"model": model, "messages": messages, **params},
                                           headers=headers, retry_statuses={500, 502, 503, 504},
                                           allow_redirects=False)
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
            http = f"HTTP {resp.status_code}, content-type {resp.headers.get('Content-Type')!r}"
            if 300 <= resp.status_code < 400:
                http += f", redirect to {resp.headers.get('Location')!r}"
            try:
                body = resp.json()
            except ValueError:
                return {"_raw": resp.text[:200], "_http": http}
            return body if isinstance(body, dict) else {"_raw": str(body)[:200], "_http": http}
        raise LLMUnavailable(f"{model} rejected every request variant")

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
        method = f"{self.backend}:{model}"
        data = _extract_json(content) if content else None
        if data and str(data.get("summary", "")).strip():
            self.bad_replies = 0
            level = str(data.get("importance", "")).lower().strip()
            why = truncate(str(data.get("why", "")).strip(), 160)
            tags = [str(t).lower() for t in (data.get("tags") or []) if isinstance(t, str)][:4]
            if level not in LEVELS:
                level, why, tags = rate(title, text, kind)
            elif self.backend == "local":
                level, why, tags = calibrate(level, why, title, text, kind)
            return {"summary": truncate(" ".join(str(data["summary"]).split()), 600), "importance": level,
                    "why": why, "tags": tags, "method": method}
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
        if "_raw" in body:
            detail += f", raw={body['_raw'][:80]!r}, {body.get('_http', '')}"
        self.bad_replies += 1
        self.problem = f"{model} replied without a usable summary ({detail})"
        if self.bad_replies >= self.MAX_BAD_REPLIES:
            self.disabled_reason = f"stopped after {self.bad_replies} unusable replies; last: {self.problem}"
        return None


def _same_model(wanted: str, available: str) -> bool:
    """'llama3.2' matches 'llama3.2:latest'; 'qwen2.5:3b' matches only itself."""
    if ":" in wanted:
        return wanted == available
    return available.split(":")[0] == wanted
