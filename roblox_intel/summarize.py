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
    is_release_notes = kind == "release_notes" or re.match(r"release notes", title or "", re.I)
    corporate = "corporate" in hits_title
    if high_title and not corporate:
        level, why = "high", f"title is about {', '.join(sorted(high_title))}"
    elif len(high_body) >= 2 and not is_release_notes:
        level, why = "high", f"discusses {', '.join(sorted(high_body))}"
    elif high_body or any(level_of[t] == "medium" for t in hits_title):
        level, why = "medium", f"touches on {', '.join(sorted(high_body or hits_title))}"
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
    def __init__(self, settings: dict[str, Any], http: HttpClient, token: str | None = None):
        cfg = settings["summarizer"]
        gm = cfg.get("github_models") or {}
        self.http = http
        self.provider = str(cfg.get("provider", "auto")).lower()
        self.endpoint = gm.get("endpoint", "https://models.github.ai/inference/chat/completions")
        self.models = list(gm.get("models") or ["openai/gpt-4.1-mini"])
        self.calls_left = int(gm.get("max_calls_per_run", 25))
        self.token = token if token is not None else (os.environ.get("GITHUB_MODELS_TOKEN")
                                                      or os.environ.get("GITHUB_TOKEN"))
        self.stats = {"llm": 0, "heuristic": 0}
        self.disabled_reason = ""
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
        if self.llm_enabled:
            try:
                result = self._llm(title, text, source, kind)
                if result:
                    self.stats["llm"] += 1
                    return result
            except LLMUnavailable as exc:
                self.disabled_reason = str(exc)
        self.stats["heuristic"] += 1
        level, why, tags = rate(title, text, kind)
        return {"summary": extractive_summary(title, text, kind), "importance": level, "why": why,
                "tags": tags, "method": "keywords"}

    def _llm(self, title: str, text: str, source: str, kind: str) -> dict[str, Any] | None:
        what = {"docs": "Official documentation change (diff of added/removed lines)",
                "page_change": "Change to an official help page (diff of added/removed lines)",
                "release_notes": "Release notes post"}.get(kind, "Post")
        prompt = (f"Source: {source}\nType: {what}\nTitle: {title}\n\nContent:\n"
                  f"{truncate(text or '(no body text available)', 12000)}")
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        while self.models:
            model = self.models[0]
            payload = {"model": model, "temperature": 0.2, "max_tokens": 400, "messages": [
                {"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]}
            self.calls_left -= 1
            try:
                resp = self.http.post_json(self.endpoint, payload, headers=headers,
                                           retry_statuses={500, 502, 503, 504})
            except HttpError as exc:
                message = str(exc).lower()
                if exc.status == 404 or (exc.status in (400, 422) and "model" in message and any(
                        w in message for w in ("unknown", "not found", "unavailable", "invalid", "unsupported"))):
                    self.models.pop(0)  # model not in the catalog -> try the next one
                    continue
                if exc.status in (400, 413, 422):
                    return None  # this post only (e.g. content filter); keyword summary instead
                if exc.status in (401, 403):
                    raise LLMUnavailable("GitHub Models refused the token (workflow needs "
                                         "`permissions: models: read`)") from exc
                if exc.status == 429:
                    raise LLMUnavailable("GitHub Models free quota reached for now") from exc
                raise LLMUnavailable(f"GitHub Models unavailable: {exc.reason}") from exc
            try:
                content = resp.json()["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError):
                return None
            data = _extract_json(content or "")
            if not data or not str(data.get("summary", "")).strip():
                return None
            level = str(data.get("importance", "")).lower().strip()
            if level not in LEVELS:
                level = rate(title, text, kind)[0]
            tags = [str(t).lower() for t in (data.get("tags") or []) if isinstance(t, str)][:4]
            return {"summary": truncate(" ".join(str(data["summary"]).split()), 600), "importance": level,
                    "why": truncate(str(data.get("why", "")).strip(), 160), "tags": tags,
                    "method": f"github-models:{model}"}
        raise LLMUnavailable("none of the configured GitHub Models are available")
