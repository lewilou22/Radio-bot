"""
Web-search + optional Groq check to tell studio call-in / SMS lines from advertiser numbers.
"""

from __future__ import annotations

import json
import os
import re
from html import unescape
from typing import Callable
from urllib.parse import urlparse

import requests

from call_in_verified_db import (
    add_rejected_ad,
    get_verified_pair,
    is_rejected_ad,
    norm_key_from_segment,
    upsert_verified,
)

USER_AGENT = os.getenv(
    "CALL_IN_VERIFY_SEARCH_UA",
    "Mozilla/5.0 (compatible; RadioMonitor/1.1; personal call-in verification)",
)
DDG_URL = "https://html.duckduckgo.com/html/"
GROQ_MODEL = os.getenv("CALL_IN_VERIFY_GROQ_MODEL", os.getenv("GROQ_CONTEST_MODEL", "llama-3.1-8b-instant"))


def _domain_from_url(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower().removeprefix("www.")
    except Exception:
        return ""


def duckduckgo_snippets(query: str, timeout: float = 14.0) -> list[str]:
    """Best-effort HTML scrape; may return empty if blocked."""
    if not (query or "").strip():
        return []
    try:
        r = requests.post(
            DDG_URL,
            data={"q": query.strip(), "b": ""},
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://duckduckgo.com/",
            },
            timeout=timeout,
        )
        r.raise_for_status()
        html = r.text
    except requests.RequestException:
        return []

    snippets: list[str] = []
    for m in re.finditer(
        r'class="result__snippet"[^>]*>(.*?)</a>',
        html,
        flags=re.I | re.DOTALL,
    ):
        raw = re.sub(r"<[^>]+>", " ", m.group(1))
        t = unescape(re.sub(r"\s+", " ", raw).strip())
        if len(t) > 15:
            snippets.append(t[:400])
    if snippets:
        return snippets[:6]
    # Fallback: any paragraph-ish chunk
    for m in re.finditer(r">([^<]{40,240})<", html):
        t = unescape(re.sub(r"\s+", " ", m.group(1)).strip())
        if "http" not in t.lower() and len(t) > 30:
            snippets.append(t[:400])
        if len(snippets) >= 4:
            break
    return snippets[:6]


# Snippets that mention the searched phone number almost always contain the digits — that must NOT
# count as evidence the line is the radio studio (e.g. a law firm’s site listing the same number).

_UNRELATED_BUSINESS = (
    "attorney",
    "attorneys",
    "lawyer",
    "lawyers",
    "law firm",
    "legal services",
    "personal injury",
    "injury attorney",
    "accident attorney",
    "criminal defense",
    "divorce attorney",
    "esq.",
    "pllc",
    "dentist",
    "orthodont",
    "dental office",
    "realtor",
    "real estate agent",
    "plumber",
    "hvac",
)


def _station_relevant(blob: str, station_name: str) -> bool:
    st = (station_name or "").strip().lower()
    if len(st) >= 3 and st in blob:
        return True
    for t in re.split(r"[\s\-–—]+", st):
        if len(t) >= 4 and t in blob:
            return True
    return False


def _radio_context(blob: str) -> bool:
    return any(
        x in blob
        for x in (
            "radio station",
            "radio",
            "listener",
            "contest line",
            "studio line",
            "request line",
            "call-in",
            "call in to",
            "on-air",
            "on air",
            "broadcast",
            "switchboard",
        )
    )


def _studio_radio_corroborated(blob: str, station_name: str) -> bool:
    """True when snippets tie the station brand to radio-ish / call-in context (not just any page)."""
    if not _station_relevant(blob, station_name):
        return False
    return _radio_context(blob)


def _reject_studio_as_unrelated_business(snippets: list[str], station_name: str) -> bool:
    """
    True → treat as not the studio line (law office, dentist, etc. in search results without
    a clear station + radio call-in tie-in). Used to override weak heuristics and Groq mistakes.
    """
    blob = " ".join(snippets).lower()
    if len(blob.strip()) < 24:
        return False
    if not any(w in blob for w in _UNRELATED_BUSINESS):
        return False
    if _studio_radio_corroborated(blob, station_name):
        return False
    studioish = sum(
        1
        for w in (
            "studio line",
            "call the studio",
            "studio phone",
            "contest line",
            "request line",
            "listener line",
        )
        if w in blob
    )
    if studioish >= 2 and _radio_context(blob):
        return False
    return True


def _heuristic_label(snippets: list[str], candidate: str, station_name: str) -> str:
    blob = " ".join(snippets).lower()
    c = (candidate or "").lower()
    digits = re.sub(r"\D", "", candidate)

    commercial_hits = sum(
        1
        for w in (
            "sponsor",
            "advertisement",
            "advertiser",
            "order now",
            "call now to order",
            "shop now",
            "not affiliated with",
            "paid promotion",
            "toll-free order",
            "deal ends",
            "limited time offer",
            "insurance quote",
            "car dealer",
            "mattress sale",
        )
        if w in blob
    )
    studio_hits = sum(
        1
        for w in (
            "studio line",
            "call the studio",
            "studio phone",
            "contest line",
            "request line",
            "switchboard",
            "listener line",
            "call in to win",
            "be the caller",
            "radio station",
            "on-air",
        )
        if w in blob
    )
    sms_hits = sum(
        1
        for w in (
            "text to",
            "text your",
            "sms",
            "short code",
            "keyword to",
            "message and data rates",
        )
        if w in blob
    )
    prof_hits = sum(1 for w in _UNRELATED_BUSINESS if w in blob)

    if re.match(r"(?i)^text\s+", candidate) or (len(digits) in (5, 6) and "text" in c):
        sms_hits += 2

    # Unrelated storefront / professional service: reject unless clearly the station’s line in-context.
    if prof_hits >= 1 and not _studio_radio_corroborated(blob, station_name):
        return "commercial_ad"

    if commercial_hits >= 2 and commercial_hits > studio_hits:
        return "commercial_ad"
    if commercial_hits >= 1 and studio_hits == 0:
        return "commercial_ad"
    if sms_hits >= 2 and len(digits) in (5, 6):
        return "sms_shortcode"
    if len(digits) in (5, 6) and sms_hits >= 1 and commercial_hits == 0 and prof_hits == 0:
        return "sms_shortcode"

    # Studio: need real call-in language, not merely “this digit string appears in a search hit”.
    strong_studio = studio_hits >= 2
    weak_studio = (
        studio_hits >= 1
        and _station_relevant(blob, station_name)
        and _radio_context(blob)
    )
    if (strong_studio or weak_studio) and commercial_hits == 0:
        return "studio_phone"
    if commercial_hits >= 1 and studio_hits >= 1:
        return "unknown"
    return "unknown"


def _groq_classify(station_name: str, candidate: str, snippets: list[str]) -> str | None:
    key = (os.getenv("GROQ_API_KEY") or "").strip()
    if not key:
        return None
    ctx = "\n".join(f"- {s[:350]}" for s in snippets[:8])
    user = (
        f'Radio station (call sign / brand): "{station_name}"\n'
        f"Candidate line heard or scraped: {candidate}\n\n"
        f"Web search snippets:\n{ctx or '(none)'}\n\n"
        'Reply with exactly one JSON object, no markdown: '
        '{"label":"studio_phone"|"sms_shortcode"|"commercial_ad"|"unknown"}'
    )
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": "You classify whether a phone or SMS code belongs to the NAMED radio station "
                        "(studio / contest / request line / listener line) vs a commercial ad or unrelated business. "
                        "If web snippets describe a law firm, medical office, realtor, or other local business using "
                        "this number with no clear tie to that radio station’s studio or contest line, use "
                        "commercial_ad or unknown — not studio_phone. Output JSON only.",
                    },
                    {"role": "user", "content": user},
                ],
                "max_tokens": 40,
                "temperature": 0,
            },
            timeout=22,
        )
        r.raise_for_status()
        msg = (r.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""
        m = re.search(r"\{[^}]+\}", msg)
        if not m:
            return None
        d = json.loads(m.group(0))
        lab = str(d.get("label", "")).strip()
        if lab in ("studio_phone", "sms_shortcode", "commercial_ad", "unknown"):
            return lab
    except Exception:
        return None
    return None


def _search_queries(station_name: str, candidate: str, page_domain: str) -> list[str]:
    d = re.sub(r"\D", "", candidate)
    q: list[str] = []
    if len(d) >= 10:
        q.append(f'"{station_name}" radio station phone {d}')
        q.append(f'"{station_name}" contest call in number {d}')
    elif len(d) in (5, 6) or re.match(r"(?i)^text\s+", candidate):
        tail = d if d else re.sub(r"(?i)^text\s+", "", candidate).strip()
        q.append(f'"{station_name}" radio text contest {tail}')
    else:
        q.append(f'"{station_name}" radio studio phone number')
    if page_domain:
        q.append(f"site:{page_domain} call studio OR contact OR contest")
    return q[:3]


def _normalize_display_for_role(segment: str, role: str) -> str:
    t = (segment or "").strip()
    if role == "sms_shortcode":
        d = re.sub(r"\D", "", t)
        if len(d) in (5, 6):
            return f"Text {d}"
        if re.match(r"(?i)^text\s+", t):
            return t
        return t
    return t


def verify_segment(
    station_name: str,
    segment: str,
    page_url: str,
) -> tuple[str | None, str]:
    """
    Returns (role_stored_as_db or None, label_debug).
    role_stored: 'studio' | 'sms' for upsert; None if rejected or unknown.
    """
    seg = (segment or "").strip()
    if len(seg) < 5:
        return None, "skip-short"
    nk = norm_key_from_segment(seg)
    if is_rejected_ad(station_name, nk):
        return None, "skip-rejected-ad"

    studio_cur, sms_cur = get_verified_pair(station_name)
    if studio_cur and nk == norm_key_from_segment(studio_cur):
        return None, "skip-known-studio"
    if sms_cur and nk == norm_key_from_segment(sms_cur):
        return None, "skip-known-sms"

    domain = _domain_from_url(page_url)
    snippets: list[str] = []
    for q in _search_queries(station_name, seg, domain):
        snippets.extend(duckduckgo_snippets(q))
        if len(snippets) >= 10:
            break
    snippets = snippets[:10]

    label = _groq_classify(station_name, seg, snippets)
    if label is None:
        label = _heuristic_label(snippets, seg, station_name)

    if label == "studio_phone" and _reject_studio_as_unrelated_business(snippets, station_name):
        label = "commercial_ad"

    if label == "commercial_ad":
        add_rejected_ad(station_name, nk)
        return None, "rejected-ad"

    if label == "unknown":
        return None, "unknown"

    if label == "sms_shortcode":
        disp = _normalize_display_for_role(seg, label)
        upsert_verified(station_name, "sms", disp, nk)
        return "sms", label

    if label == "studio_phone":
        disp = _normalize_display_for_role(seg, label)
        upsert_verified(station_name, "studio", disp, nk)
        return "studio", label

    return None, label


def split_candidate_blob(blob: str) -> list[str]:
    if not (blob or "").strip():
        return []
    parts = [p.strip() for p in re.split(r"\s*·\s*", blob) if p.strip()]
    return parts[:12]


def process_station_candidates(
    station_name: str,
    heard_blob: str,
    scrape_blob: str,
    page_url: str,
    log: Callable[[str], None] | None = None,
) -> None:
    """Verify new segments from heard + scrape merged list."""
    merged: list[str] = []
    seen: set[str] = set()
    for block in (heard_blob, scrape_blob):
        for p in split_candidate_blob(block):
            nk = norm_key_from_segment(p)
            if nk in seen:
                continue
            seen.add(nk)
            merged.append(p)

    for seg in merged:
        try:
            role, dbg = verify_segment(station_name, seg, page_url)
            if log:
                log(f"call-in verify {station_name!r} {seg!r} -> {role} ({dbg})")
        except Exception as exc:
            if log:
                log(f"call-in verify error {station_name!r}: {exc}")
