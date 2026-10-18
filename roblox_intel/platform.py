"""Platform change monitor: official Roblox sources -> summarized, rated items.

Source types (configured in settings.yml -> platform.sources):
  discourse        DevForum categories (JSON API, RSS fallback, parent-category fallback)
  newsroom         Roblox's official newsroom/blog (feed autodiscovery, page links, sitemap)
  git_docs         Roblox/creator-docs on GitHub: diffs of monetization/discovery/policy pages
  zendesk_article  a Help Center article (e.g. DevEx FAQ): reports when its text changes
"""

from __future__ import annotations

import difflib
import hashlib
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

from .config import cache_root
from .context import Context
from .http import HttpError
from .summarize import LEVELS, Summarizer
from .util import html_to_text, parse_iso, shift_day, truncate

SEEN_LIMIT = 400


class SourceError(Exception):
    pass


@dataclass
class Item:
    id: str
    title: str
    url: str
    published: str = ""  # ISO timestamp (may be approximate until loaded)
    text: str = ""
    kind: str = "post"   # post | release_notes | docs | page_change
    loader: Callable[[], dict] | None = None  # fetches text (and exact title/date) lazily

    def load(self) -> None:
        if self.loader and not self.text:
            extra = self.loader() or {}
            self.text = extra.get("text") or self.text
            self.title = extra.get("title") or self.title
            self.published = extra.get("published") or self.published
            self.loader = None


def _iso(value: Any) -> str:
    if not value:
        return ""
    dt = parse_iso(str(value))
    if not dt:
        try:
            dt = parsedate_to_datetime(str(value))
        except (TypeError, ValueError):
            return ""
    return dt.isoformat(timespec="seconds")


# ---- feeds ---------------------------------------------------------------------

def parse_feed(xml_text: str) -> list[dict]:
    """RSS 2.0 or Atom -> [{title, link, published, html, guid}]."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError as exc:
        raise SourceError(f"feed is not valid XML: {exc}") from exc
    out = []
    atom = "{http://www.w3.org/2005/Atom}"
    content_ns = "{http://purl.org/rss/1.0/modules/content/}encoded"
    for item in root.iter("item"):
        out.append({
            "title": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "published": _iso(item.findtext("pubDate")),
            "html": item.findtext(content_ns) or item.findtext("description") or "",
            "guid": (item.findtext("guid") or item.findtext("link") or "").strip(),
        })
    for entry in root.iter(f"{atom}entry"):
        link = entry.find(f"{atom}link[@rel='alternate']")
        if link is None:
            link = entry.find(f"{atom}link")
        href = link.get("href", "") if link is not None else ""
        out.append({
            "title": (entry.findtext(f"{atom}title") or "").strip(),
            "link": href,
            "published": _iso(entry.findtext(f"{atom}published") or entry.findtext(f"{atom}updated")),
            "html": entry.findtext(f"{atom}content") or entry.findtext(f"{atom}summary") or "",
            "guid": (entry.findtext(f"{atom}id") or href).strip(),
        })
    return out


# ---- DevForum (Discourse) ------------------------------------------------------------

def _clean_markdown(text: str) -> str:
    text = re.sub(r"\[quote[^\]]*\].*?\[/quote\]", " ", text or "", flags=re.S | re.I)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)|<img[^>]*>|\[details=[^\]]*\]|\[/details\]", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return text.strip()


def _discourse_text(ctx: Context, base: str, topic_id: int) -> dict:
    try:
        raw = ctx.http.get_text(f"{base}/raw/{topic_id}/1", ttl=86400)
        if raw.strip():
            return {"text": _clean_markdown(raw)}
    except HttpError:
        pass
    data = ctx.http.get_json(f"{base}/t/{topic_id}.json", ttl=86400)
    posts = (data.get("post_stream") or {}).get("posts") or []
    return {"text": html_to_text(posts[0].get("cooked", "")) if posts else ""}


def _resolve_category(ctx: Context, base: str, path: str) -> int | None:
    """Find a category id from its slug path via /categories.json."""
    slugs = [s for s in path.strip("/").split("/") if s]
    data = ctx.http.get_json(f"{base}/categories.json", params={"include_subcategories": "true"}, ttl=86400)
    level = (data.get("category_list") or {}).get("categories") or []
    found = None
    for slug in slugs:
        found = next((c for c in level if c.get("slug") == slug), None)
        if not found:
            return None
        level = found.get("subcategory_list") or []
    return int(found["id"]) if found else None


def _category_topics(ctx: Context, base: str, path: str, cat_id: int | None) -> list[dict]:
    url = f"{base}/c/{path}/{cat_id}.json" if cat_id else f"{base}/c/{path}.json"
    data = ctx.http.get_json(url, ttl=3600)
    topics = (data.get("topic_list") or {}).get("topics")
    if topics is None:
        raise SourceError(f"{url}: no topic_list in response")
    return topics


def discourse(ctx: Context, src: dict, sstate: dict) -> tuple[list[Item], str]:
    base = src["base_url"].rstrip("/")
    path = src["category"].strip("/")
    title_filter = re.compile(src["title_filter"], re.I) if src.get("title_filter") else None
    kind = "release_notes" if "release" in path else "post"
    notes: list[str] = []
    topics = None
    cat_id = sstate.get("category_id") or src.get("category_id")
    try:
        topics = _category_topics(ctx, base, path, cat_id)
    except (HttpError, SourceError) as exc:
        notes.append(f"category JSON failed ({_brief(exc)})")
        try:
            resolved = _resolve_category(ctx, base, path)
            if resolved and resolved != cat_id:
                topics = _category_topics(ctx, base, path, resolved)
                sstate["category_id"] = resolved
                notes.append(f"resolved category id {resolved}")
        except (HttpError, SourceError) as exc2:
            notes.append(f"category lookup failed ({_brief(exc2)})")
    if topics is not None:
        items = []
        for t in topics:
            title = t.get("title") or t.get("fancy_title") or ""
            if t.get("pinned") and title.lower().startswith("about the"):
                continue
            tid = t["id"]
            items.append(Item(id=f"{src['id']}:{tid}", title=title, kind=kind,
                              url=f"{base}/t/{t.get('slug', 'topic')}/{tid}", published=_iso(t.get("created_at")),
                              loader=lambda tid=tid: _discourse_text(ctx, base, tid)))
        return items, "; ".join(notes)

    # RSS fallback for the same category
    rss_url = f"{base}/c/{path}/{cat_id}.rss" if cat_id else f"{base}/c/{path}.rss"
    try:
        entries = parse_feed(ctx.http.get_text(rss_url, ttl=3600))
        notes.append("used RSS feed")
        return [_feed_item(src, e, kind) for e in entries], "; ".join(notes)
    except (HttpError, SourceError) as exc:
        notes.append(f"RSS failed ({_brief(exc)})")

    # last resort: parent category filtered by title (e.g. "Release Notes for ...")
    parent = src.get("fallback_category")
    if parent and title_filter:
        try:
            topics = _category_topics(ctx, base, parent.strip("/"), None)
        except (HttpError, SourceError) as exc:
            notes.append(f"parent category '{parent}' failed ({_brief(exc)})")
            raise SourceError("; ".join(notes)) from None
        notes.append(f"used parent category '{parent}' filtered by title")
        items = [Item(id=f"{src['id']}:{t['id']}", title=t.get("title", ""), kind=kind,
                      url=f"{base}/t/{t.get('slug', 'topic')}/{t['id']}", published=_iso(t.get("created_at")),
                      loader=lambda tid=t["id"]: _discourse_text(ctx, base, tid))
                 for t in topics if title_filter.search(t.get("title", ""))]
        return items, "; ".join(notes)
    raise SourceError("; ".join(notes))


def _brief(exc: Exception) -> str:
    if isinstance(exc, HttpError):
        return f"HTTP {exc.status}" if exc.status else re.sub(r"^gave up after \d+ attempts: ", "", exc.reason)[:120]
    return str(exc)[:120]


def _feed_item(src: dict, entry: dict, kind: str = "post") -> Item:
    key = hashlib.sha1((entry.get("guid") or entry.get("link") or entry["title"]).encode()).hexdigest()[:12]
    m = re.search(r"/t/[^/]+/(\d+)", entry.get("link", ""))
    return Item(id=f"{src['id']}:{m.group(1) if m else key}", title=entry["title"], url=entry["link"],
                published=entry["published"], text=html_to_text(entry["html"]), kind=kind)


# ---- Newsroom ---------------------------------------------------------------------------

_ARTICLE_RE = re.compile(r"/newsroom/(\d{4})/(\d{2})/([a-z0-9][a-z0-9-]*[a-z0-9])")


def _meta(html: str, *names: str) -> str:
    for name in names:
        m = re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(name)}["\'][^>]*content=["\']([^"\']*)', html, re.I) \
            or re.search(rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{re.escape(name)}["\']', html, re.I)
        if m and m.group(1).strip():
            return html_to_text(m.group(1))
    return ""


def _article(ctx: Context, url: str) -> dict:
    page = ctx.http.get_text(url, ttl=86400)
    title = _meta(page, "og:title", "twitter:title")
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
        title = html_to_text(m.group(1)) if m else ""
    published = _meta(page, "article:published_time", "og:published_time", "date")
    if not published:
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', page) or re.search(r'<time[^>]+datetime="([^"]+)"', page)
        published = m.group(1) if m else ""
    m = re.search(r"<article[^>]*>(.*?)</article>", page, re.S | re.I)
    body = m.group(1) if m else " ".join(re.findall(r"<p[^>]*>(.*?)</p>", page, re.S | re.I))
    text = html_to_text(body) or _meta(page, "og:description", "description")
    return {"title": re.sub(r"\s*[|–-]\s*Roblox( Newsroom| Corporation| Blog)?\s*$", "", title).strip(), "published": _iso(published), "text": text}


def newsroom(ctx: Context, src: dict, sstate: dict) -> tuple[list[Item], str]:
    base = src["base_url"].rstrip("/")
    index_url = base + src.get("path", "/newsroom")
    page = ctx.http.get_text(index_url, ttl=3600)
    feed = re.search(r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*href=["\']([^"\']+)', page, re.I)
    if feed:
        href = feed.group(1)
        href = href if href.startswith("http") else base + href
        try:
            entries = parse_feed(ctx.http.get_text(href, ttl=3600))
            return [_feed_item(src, e) for e in entries], "used RSS/Atom feed"
        except (HttpError, SourceError):
            pass
    # article paths anywhere in the page (links or embedded JSON); locale copies such as
    # /en-au/newsroom/... share the slug and collapse into the canonical article
    links: dict[str, tuple[str, str, str]] = {}
    for m in _ARTICLE_RE.finditer(page):
        links.setdefault(m.group(3), (m.group(0), m.group(1), m.group(2)))
    note = f"{len(links)} articles linked from the newsroom page"
    if not links:
        try:  # client-rendered page: fall back to the sitemap
            sitemap = ctx.http.get_text(f"{base}/sitemap.xml", ttl=3600)
            for m in _ARTICLE_RE.finditer(sitemap):
                links.setdefault(m.group(3), (m.group(0), m.group(1), m.group(2)))
            note = f"{len(links)} articles from sitemap.xml"
        except HttpError as exc:
            raise SourceError(f"no article links on {index_url} and sitemap failed ({exc})") from exc
    if not links:
        raise SourceError(f"no article links found on {index_url} or in the sitemap")
    items = []
    for slug, (path, year, month) in links.items():
        url = base + path
        items.append(Item(id=f"{src['id']}:{slug}", title=slug.replace("-", " ").capitalize(), url=url,
                          published=f"{year}-{month}-01T00:00:00+00:00",  # refined when the page loads
                          loader=lambda url=url: _article(ctx, url)))
    return items, note


# ---- Creator Docs (git) ------------------------------------------------------------------------

def _git(repo: Path, *args: str, timeout: int = 180) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise SourceError(f"git {args[0]} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def _sync_repo(repo: Path, url: str, since: str) -> str:
    """Blobless, shallow clone covering `since` onward (a few MB). Returns the head sha."""
    if (repo / ".git").exists():
        try:
            _git(repo, "fetch", "--quiet", f"--shallow-since={since}", "--filter=blob:none", "origin", "HEAD")
            return _git(repo, "rev-parse", "FETCH_HEAD").strip()
        except SourceError:
            shutil.rmtree(repo, ignore_errors=True)  # broken cache: start over
    repo.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["git", "clone", "--quiet", "--no-checkout", "--filter=blob:none",
                           f"--shallow-since={since}", url, str(repo)],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise SourceError(f"git clone failed: {proc.stderr.strip()[:300]}")
    return _git(repo, "rev-parse", "HEAD").strip()


def _has_commit(repo: Path, rev: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
                          capture_output=True).returncode == 0


def _doc_title(repo: Path, rev: str, path: str) -> str:
    try:
        head = _git(repo, "show", f"{rev}:{path}")[:600]
        m = re.search(r"^title:\s*(.+)$", head, re.M)
        if m:
            return m.group(1).strip().strip("'\"")
    except SourceError:
        pass
    return Path(path).stem.replace("-", " ").capitalize()


def _diff_text(repo: Path, base: str, head: str, files: list[str], limit: int = 6000) -> tuple[str, int]:
    """Readable +/- lines for the changed pages, and how many words really changed."""
    diff = _git(repo, "diff", "--unified=0", "--no-color", "--no-ext-diff", base, head, "--", *files)
    out_lines: list[str] = []
    added_words: Counter = Counter()
    removed_words: Counter = Counter()
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path = line.split(" b/", 1)[-1]
            out_lines.append(f"Page: {path.removeprefix('content/en-us/')}")
            continue
        if line.startswith(("+++", "---")) or line[:1] not in ("+", "-"):
            continue
        content = " ".join(re.sub(r"<[^>]+>|!\[[^\]]*\]\([^)]*\)", " ", line[1:]).split())
        content = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", content)
        if len(re.findall(r"[A-Za-z]{2,}", content)) < 2:
            continue  # whitespace, table pipes, image-only lines
        (added_words if line[0] == "+" else removed_words).update(re.findall(r"[a-z0-9$%.]+", content.lower()))
        out_lines.append(f"{line[0]} {content}")
    # drop page headers that ended up with no meaningful lines
    lines = [l for i, l in enumerate(out_lines)
             if not l.startswith("Page: ") or (i + 1 < len(out_lines) and not out_lines[i + 1].startswith("Page: "))]
    changed = sum(((added_words - removed_words) + (removed_words - added_words)).values())
    return truncate("\n".join(lines), limit), changed


def git_docs(ctx: Context, src: dict, sstate: dict) -> tuple[list[Item], str]:
    url = src["repo_url"].rstrip("/")
    areas: dict[str, list[str]] = src.get("areas") or {}
    lookback = int(ctx.settings["platform"]["first_run_lookback_days"])
    since_day = sstate.get("last_commit_day") or shift_day(ctx.day, -lookback)
    repo = cache_root(ctx.settings) / "repos" / re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-")
    head = _sync_repo(repo, url, shift_day(since_day, -3))
    last = sstate.get("last_sha")
    if last and _has_commit(repo, last):
        rev_args = [f"{last}..{head}"]
    else:
        rev_args = [f"--since={since_day}T00:00:00Z", head]
    log = _git(repo, "log", "--reverse", "--format=%H %cI", *rev_args).splitlines()
    commits = [line.split(" ", 1) for line in log if line.strip()]
    paths = [p for plist in areas.values() for p in plist]

    def area_of(path: str) -> str | None:
        return next((a for a, plist in areas.items()
                     if any(path == p or path.startswith(p.rstrip("/") + "/") for p in plist)), None)

    groups: dict[tuple[str, str], dict] = defaultdict(lambda: {"commits": [], "files": []})
    for sha, when in commits:
        if not _has_commit(repo, f"{sha}^"):
            continue  # shallow boundary: parent not fetched, can't diff
        for path in _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha, "--", *paths).split():
            area = area_of(path)
            if not area:
                continue
            g = groups[(when[:10], area)]
            g.setdefault("when", when)
            if sha not in g["commits"]:
                g["commits"].append(sha)
            if path not in g["files"]:
                g["files"].append(path)

    items: list[Item] = []
    skipped = 0
    repo_web = url.removesuffix(".git")
    for (day, area), g in sorted(groups.items()):
        base = _git(repo, "rev-parse", f"{g['commits'][0]}^").strip()
        last_sha = g["commits"][-1]
        text, changed_words = _diff_text(repo, base, last_sha, g["files"])
        if changed_words < 8:
            skipped += 1  # formatting/link-only edits
            continue
        titles = [_doc_title(repo, last_sha, f) for f in g["files"][:3]]
        more = f" +{len(g['files']) - 3} more" if len(g["files"]) > 3 else ""
        slug = re.sub(r"[^a-z0-9]+", "-", area.lower()).strip("-")
        items.append(Item(id=f"{src['id']}:{day}:{slug}:{last_sha[:10]}",
                          title=f"{area} docs updated: {', '.join(titles)}{more}",
                          url=f"{repo_web}/compare/{base[:12]}...{last_sha[:12]}", published=g["when"],
                          text=text, kind="docs"))
    sstate["last_sha"] = head
    if commits:
        sstate["last_commit_day"] = commits[-1][1][:10]
    return items, f"{len(commits)} new commits, {len(items)} meaningful changes" + (
        f", {skipped} formatting-only edits ignored" if skipped else "")


# ---- Help Center article (Zendesk) -------------------------------------------------------------

def zendesk_article(ctx: Context, src: dict, sstate: dict) -> tuple[list[Item], str]:
    base = src["base_url"].rstrip("/")
    aid = src["article_id"]
    try:
        art = ctx.http.get_json(f"{base}/api/v2/help_center/en-us/articles/{aid}.json", ttl=3600)["article"]
        title, body, url = art.get("title", ""), art.get("body", ""), art.get("html_url") or ""
        updated = art.get("edited_at") or art.get("updated_at") or ""
        note = "help center API"
    except (HttpError, KeyError, TypeError):
        page = ctx.http.get_text(f"{base}/hc/en-us/articles/{aid}", ttl=3600)
        m = re.search(r'<div[^>]+class="[^"]*article-body[^"]*"[^>]*>(.*?)</div>\s*(?:<div|</section|</article)',
                      page, re.S | re.I)
        if not m:
            raise SourceError("article body not found in page HTML")
        body, url = m.group(1), f"{base}/hc/en-us/articles/{aid}"
        tm = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S | re.I)
        title, updated, note = html_to_text(tm.group(1)) if tm else f"Article {aid}", "", "page HTML"
    text = html_to_text(body)
    if len(text) < 200:
        raise SourceError("article text looks empty")
    digest = hashlib.sha256(text.encode()).hexdigest()
    previous, prev_text = sstate.get("digest"), sstate.get("text", "")
    sstate.update({"digest": digest, "text": text[:30000], "title": title})
    if not previous:
        return [], f"{note}; baseline saved ({len(text)} chars)"
    if previous == digest:
        return [], f"{note}; unchanged"
    lines = []
    for line in difflib.unified_diff(prev_text.splitlines(), text.splitlines(), lineterm="", n=0):
        if line.startswith(("+++", "---", "@@")):
            continue
        if line[:1] in "+-" and line[1:].strip():
            lines.append(f"{line[0]} {line[1:].strip()}")
    item = Item(id=f"{src['id']}:{digest[:12]}", title=f"Help page changed: {title}", url=url,
                published=_iso(updated) or ctx.now.isoformat(timespec="seconds"),
                text=truncate("\n".join(lines), 6000), kind="page_change")
    return [item], f"{note}; text changed"


KEEP_TEXT_CHARS = 4000
UPGRADE_DAYS = 14


def _refetch_text(ctx: Context, src: dict | None, rec: dict) -> str:
    """Body text of an already-logged post, for sources that can be re-read by id or URL."""
    if not src:
        return ""
    try:
        if src.get("type") == "discourse":
            tid = rec["id"].rsplit(":", 1)[-1]
            if tid.isdigit():
                return _discourse_text(ctx, src["base_url"].rstrip("/"), int(tid)).get("text", "")
        elif src.get("type") == "newsroom" and rec.get("url"):
            return _article(ctx, rec["url"]).get("text", "")
    except (HttpError, SourceError):
        return ""
    return ""


def upgrade_summaries(ctx: Context, summarizer: Summarizer, records: list[dict]) -> int:
    """Replace recent keyword summaries with AI summaries once the AI is available again
    (e.g. the free quota ran out yesterday). Raw text is only kept while it may be needed."""
    cutoff = shift_day(ctx.day, -UPGRADE_DAYS)
    sources = {s["id"]: s for s in ctx.settings["platform"].get("sources") or []}
    upgraded = 0
    for rec in sorted(records, key=lambda r: r.get("published", ""), reverse=True):
        if not summarizer.llm_enabled:
            break
        if rec.get("method") != "keywords" or (rec.get("found") or "") < cutoff:
            continue
        text = rec.get("text") or _refetch_text(ctx, sources.get(rec.get("source")), rec)
        if not text:
            continue
        result = summarizer.summarize_ai(rec["title"], text, rec.get("source_label", ""), rec.get("kind", "post"))
        if result:
            rec.update(result)
            upgraded += 1
    for rec in records:
        if "text" in rec and (rec.get("method") != "keywords" or (rec.get("found") or "") < cutoff):
            del rec["text"]
    return upgraded


FETCHERS: dict[str, Callable[[Context, dict, dict], tuple[list[Item], str]]] = {
    "discourse": discourse,
    "newsroom": newsroom,
    "git_docs": git_docs,
    "zendesk_article": zendesk_article,
}


# ---- orchestration ---------------------------------------------------------------------------------

def run(ctx: Context, summarizer: Summarizer | None = None) -> list[dict]:
    """Fetch every source, summarize new items, append to data/platform.json."""
    cfg = ctx.settings["platform"]
    store = ctx.store
    state = store.state("platform")
    log = store.platform_items()
    known = {i["id"] for i in log}
    summarizer = summarizer or Summarizer(ctx.settings, ctx.http)
    lookback = int(cfg["first_run_lookback_days"])
    max_new = int(cfg["max_new_items_per_source"])
    new_records: list[dict] = []
    for src in cfg.get("sources") or []:
        if src.get("enabled") is False:
            continue
        sid, label = src["id"], src.get("label") or src["id"]
        fetcher = FETCHERS.get(src.get("type"))
        if not fetcher:
            ctx.health.fail(f"platform:{sid}", label, f"unknown source type {src.get('type')!r}")
            continue
        sstate = state.setdefault(sid, {})
        first_run = not sstate.get("initialized")
        try:
            items, note = fetcher(ctx, src, sstate)
        except (HttpError, SourceError, subprocess.TimeoutExpired, OSError) as exc:
            reason = exc.reason if isinstance(exc, HttpError) else str(exc)
            ctx.health.fail(f"platform:{sid}", label, reason, fallback="skipped this run; retried tomorrow")
            continue
        # first run: only recent posts; later runs: anything new from the last month
        cutoff = shift_day(ctx.day, -lookback if first_run else -max(lookback, 30))
        seen = list(sstate.get("seen") or [])
        seen_set = set(seen)
        done: list[str] = []
        failures: list[str] = []
        added = 0
        candidates = sorted((i for i in items if i.id not in seen_set and i.id not in known),
                            key=lambda i: i.published or "", reverse=True)
        for item in candidates:
            if item.published and item.published[:7] < cutoff[:7]:
                done.append(item.id)  # older than the window (month precision is enough here)
                continue
            if added >= max_new:
                continue  # stays unseen; picked up on the next run
            try:
                item.load()
            except (HttpError, SourceError) as exc:
                failures.append(f"{item.url} ({exc})")
                continue  # stays unseen; retried on the next run
            if item.published and item.published[:10] < cutoff:
                done.append(item.id)
                continue
            result = summarizer.summarize(item.title, item.text, label, item.kind)
            record = {
                "id": item.id, "source": sid, "source_label": label, "title": item.title, "url": item.url,
                "published": item.published or ctx.now.isoformat(timespec="seconds"), "found": ctx.day,
                "kind": item.kind, **result,
            }
            if result["method"] == "keywords":  # kept so a later run can upgrade it to an AI summary
                record["text"] = truncate(item.text or "", KEEP_TEXT_CHARS)
            new_records.append(record)
            known.add(item.id)
            done.append(item.id)
            added += 1
        sstate["seen"] = list(dict.fromkeys(done + seen))[:SEEN_LIMIT]
        sstate["initialized"] = sstate.get("initialized") or ctx.day
        detail = f"{note + '; ' if note else ''}{added} new"
        if failures:
            ctx.health.warn(f"platform:{sid}", label, f"{detail}; {len(failures)} posts failed to load, "
                            f"retrying next run: {failures[0]}")
        else:
            ctx.health.ok(f"platform:{sid}", label, detail)
    upgraded = upgrade_summaries(ctx, summarizer, log + new_records)
    store.save_platform_items(log + new_records)
    store.save_state("platform", state)
    stats = summarizer.stats
    note = "; ".join(x for x in (summarizer.disabled_reason or summarizer.problem, summarizer.catalog_note) if x)
    if stats["llm"]:
        ctx.health.ok("summarizer", "Summaries (GitHub Models, free tier)",
                      f"{stats['llm']} AI summaries ({upgraded} upgraded from earlier keyword summaries), "
                      f"{stats['heuristic']} keyword summaries" + (f"; note: {note}" if note else ""))
    elif stats["heuristic"] or note:
        ctx.health.warn("summarizer", "Summaries",
                        f"keyword summarizer used: {note or 'AI summaries unavailable'}")
    new_records.sort(key=lambda r: r["published"], reverse=True)
    new_records.sort(key=lambda r: LEVELS.index(r["importance"]))
    return new_records
