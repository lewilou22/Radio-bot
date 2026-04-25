"""
Detect studio / contest call-in phone numbers and SMS short codes from speech or ICY text.
Heuristic (not perfect); prefers matches near call/text/studio/contest context.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# Words that suggest a nearby number is for listeners (not zip codes, etc.).
_CONTEXT = re.compile(
    r"\b("
    r"call|calling|phone|dial|ring|studio|contest|line|lines|listener|listeners|"
    r"hotline|toll[\s-]*free|switchboard|request|win|prize|"
    r"text(ing| us| the)?|sms|message us|short[\s-]*code|keyword|"
    r"reach us|contact"
    r")\b",
    re.I,
)

# SMS / contest short codes (5–6 digits) when tied to text/sms wording.
_SHORT_SMS = re.compile(
    r"\b(?:text|sms|message)\b(?:\s+\w+){0,6}?\s*(?:to|at|#)?\s*\b([0-9]{5,6})\b",
    re.I,
)
_SHORT_NEAR = re.compile(
    r"(?:\b(?:text|sms|code|keyword)\b[\w\s,'#]{0,40}?\b([0-9]{5,6})\b)|"
    r"(?:\b([0-9]{5,6})\b[\w\s,'#]{0,25}?\b(?:text|sms|keyword)\b)",
    re.I,
)

_TOLL_FREE = re.compile(
    r"(?:\+?1[-.\s]?)?(8[0-9]{2})[-.\s]?([0-9]{3})[-.\s]?([0-9]{4})\b"
)
_US_PHONE = re.compile(
    r"(?:\+?1[-.\s]?)?\(?([2-9][0-9]{2})\)?[-.\s]?([0-9]{3})[-.\s]?([0-9]{4})\b"
)
_COMPACT_10 = re.compile(r"\b(1)?([2-9][0-9]{2})([0-9]{3})([0-9]{4})\b")


class _Hit(NamedTuple):
    key: str
    display: str
    score: float


def _digits_normalize(area: str, mid: str, last: str, leading: str = "") -> tuple[str, str] | None:
    d = re.sub(r"\D", "", f"{leading}{area}{mid}{last}")
    toll3 = ("800", "888", "877", "866", "855", "844", "833", "822")
    if len(d) == 10 and d[:3] in toll3:
        disp = f"1-{d[:3]}-{d[3:6]}-{d[6:]}"
        return "1" + d, disp
    if len(d) == 10 and d[0] in "23456789":
        disp = f"({d[:3]}) {d[3:6]}-{d[6:]}"
        return d, disp
    if len(d) == 11 and d[0] == "1":
        rest = d[1:]
        if rest[:3] in toll3:
            disp = f"1-{rest[:3]}-{rest[3:6]}-{rest[6:]}"
            return d, disp
        if rest[0] in "23456789":
            disp = f"1 ({rest[:3]}) {rest[3:6]}-{rest[6:]}"
            return d, disp
    return None


def display_for_raw_phone_digits(raw: str) -> str | None:
    """Format a digit string (e.g. from a tel: link) for display."""
    d = re.sub(r"\D", "", raw)
    if len(d) == 10:
        r = _digits_normalize(d[:3], d[3:6], d[6:], "")
        return r[1] if r else None
    if len(d) == 11 and d[0] == "1":
        r = _digits_normalize(d[1:4], d[4:7], d[7:], "")
        return r[1] if r else None
    return None


def _context_bonus(text: str, start: int, end: int) -> float:
    lo = max(0, start - 50)
    hi = min(len(text), end + 50)
    return 2.2 if _CONTEXT.search(text[lo:hi]) else 0.9


def _collect_phone_hits(text: str) -> list[_Hit]:
    if not text.strip():
        return []
    hits: list[_Hit] = []
    seen_spans: set[tuple[int, int]] = set()

    def add_hit(start: int, end: int, key: str, display: str) -> None:
        sp = (start, end)
        if sp in seen_spans:
            return
        seen_spans.add(sp)
        sc = _context_bonus(text, start, end)
        if key.startswith("8") and len(key) >= 10:
            sc += 0.5
        hits.append(_Hit(key, display, sc))

    for m in _TOLL_FREE.finditer(text):
        a, b, c = m.group(1), m.group(2), m.group(3)
        norm = _digits_normalize(a, b, c, "")
        if norm:
            add_hit(m.start(), m.end(), norm[0], norm[1])

    for m in _US_PHONE.finditer(text):
        a, b, c = m.group(1), m.group(2), m.group(3)
        norm = _digits_normalize(a, b, c, "")
        if norm:
            add_hit(m.start(), m.end(), norm[0], norm[1])

    for m in _COMPACT_10.finditer(text):
        lead, a, b, c = m.group(1) or "", m.group(2), m.group(3), m.group(4)
        norm = _digits_normalize(a, b, c, lead)
        if norm:
            # Skip if this span is fully inside an existing span
            st, en = m.start(), m.end()
            if any(st >= a0 and en <= b0 for a0, b0 in seen_spans):
                continue
            add_hit(st, en, norm[0], norm[1])

    return hits


def _collect_short_code_hits(text: str) -> list[_Hit]:
    hits: list[_Hit] = []
    for pat in (_SHORT_SMS, _SHORT_NEAR):
        for m in pat.finditer(text):
            g = next((x for x in m.groups() if x), None)
            if not g or not g.isdigit() or len(g) not in (5, 6):
                continue
            if g.startswith("19") or g.startswith("20"):  # years
                continue
            key = f"sms:{g}"
            hits.append(_Hit(key, f"Text {g}", 2.4 + _context_bonus(text, m.start(), m.end())))
    return hits


def best_call_in_display_string(text: str) -> str:
    """Single-line summary of best numbers found in this blob of text."""
    phones = _collect_phone_hits(text)
    shorts = _collect_short_code_hits(text)
    all_hits = phones + shorts
    if not all_hits:
        return ""

    by_key: dict[str, float] = {}
    by_key_disp: dict[str, str] = {}
    for h in all_hits:
        prev = by_key.get(h.key, 0.0)
        if h.score > prev:
            by_key[h.key] = h.score
            by_key_disp[h.key] = h.display

    ranked = sorted(by_key.items(), key=lambda kv: kv[1], reverse=True)
    parts = [by_key_disp[k] for k, _ in ranked[:4]]
    return " · ".join(parts)


def merge_call_in_strings(existing: str, new: str) -> str:
    """Merge two display strings without duplicate numbers/codes."""
    if not new.strip():
        return existing
    if not existing.strip():
        return new.strip()

    def norm_piece(p: str) -> str:
        return re.sub(r"\D", "", p)

    seen: set[str] = set()
    out: list[str] = []
    for block in (existing, new):
        for piece in re.split(r"\s*·\s*", block):
            t = piece.strip()
            if not t:
                continue
            nk = norm_piece(t) if any(c.isdigit() for c in t) else t.lower()
            if nk in seen:
                continue
            seen.add(nk)
            out.append(t)
    return " · ".join(out[:8])
