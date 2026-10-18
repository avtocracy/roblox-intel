"""Small shared helpers: dates, number formatting, text cleanup."""

from __future__ import annotations

import html
import math
import re
from datetime import date, datetime, timedelta, timezone

_ISO_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$"
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str | None) -> datetime | None:
    """Parse Roblox/Discourse timestamps, including 7-digit fractions and 'Z'."""
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    m = _ISO_RE.match(value)
    if not m:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    day, clock, frac, tz = m.groups()
    if len(clock) == 5:
        clock += ":00"
    text = f"{day}T{clock}"
    if frac:
        text += "." + frac[:6].ljust(6, "0")
    if not tz or tz == "Z":
        tz = "+00:00"
    elif ":" not in tz:
        tz = tz[:3] + ":" + tz[3:]
    return datetime.fromisoformat(text + tz)


def to_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value[:10])


def days_between(a: str | date | datetime, b: str | date | datetime) -> int:
    return (to_date(b) - to_date(a)).days


def shift_day(day: str, delta: int) -> str:
    return (to_date(day) + timedelta(days=delta)).isoformat()


def fmt_num(n: float | int | None) -> str:
    """1234 -> 1.2k, 4500000 -> 4.5M."""
    if n is None:
        return "?"
    n = float(n)
    sign = "-" if n < 0 else ""
    n = abs(n)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= limit * 0.9995:  # 999,999 -> "1M", not "1000k"
            value = n / limit  # round half up, like the dashboard's JavaScript
            text = f"{math.floor(value * 10 + 0.5) / 10:.1f}" if value < 99.95 else f"{math.floor(value + 0.5)}"
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            return sign + text + suffix
    return sign + f"{n:.0f}"


def fmt_pct(p: float | None, signed: bool = True) -> str:
    if p is None:
        return "n/a"
    if abs(p) >= 1000:
        text = f"{p:,.0f}%"
    elif abs(p) >= 10:
        text = f"{p:.0f}%"
    else:
        text = f"{p:.1f}%"
    if signed and p > 0:
        text = "+" + text
    return text


def pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old <= 0:
        return None
    return (new - old) / old * 100.0


def normalize_name(text: str) -> str:
    """Lowercase, drop emoji/punctuation: '[🍂FALL] Grow a Garden 🌶️' -> 'fall grow a garden'."""
    text = re.sub(r"[^0-9a-z]+", " ", (text or "").lower())
    return " ".join(text.split())


def name_matches(actual: str, expected: str) -> bool:
    """True if the expected words appear as whole words in actual, ignoring spacing
    ('BedWars' ~ 'Bed Wars'), but 'Indoors Tycoon' does not match 'Doors'."""
    words, target = normalize_name(actual).split(), normalize_name(expected).replace(" ", "")
    if not target:
        return True
    for i in range(len(words)):
        joined = ""
        for word in words[i:]:
            joined += word
            if joined == target:
                return True
            if len(joined) >= len(target):
                break
    return False


_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|table|blockquote|pre)[^>]*>", re.I)
_DROP_RE = re.compile(
    r"<(script|style|aside|svg)[^>]*>.*?</\1>|<div[^>]*class=\"[^\"]*(lightbox|meta)[^\"]*\"[^>]*>.*?</div>",
    re.I | re.S,
)


def html_to_text(raw: str) -> str:
    """Rough HTML -> plain text that keeps paragraph breaks."""
    if not raw:
        return ""
    text = _DROP_RE.sub(" ", raw)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out, blank = [], False
    for line in lines:
        if line:
            out.append(line)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    cut = text[: max(0, limit - 1)].rstrip()
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,.;:-") + "…"
