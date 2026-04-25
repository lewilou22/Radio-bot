"""
Fetch a station web page and extract call-in / studio phone numbers and SMS-style codes.
Uses tel:/sms: links plus the same heuristics as speech on visible page text.
"""

from __future__ import annotations

import os
import re
from html import unescape
from urllib.parse import urlparse, unquote as url_unquote

import requests

from call_in_numbers import (
    best_call_in_display_string,
    display_for_raw_phone_digits,
    merge_call_in_strings,
)

USER_AGENT = os.getenv(
    "CALL_IN_SCRAPE_USER_AGENT",
    "RadioMonitor/1.0 (call-in lookup; personal radio monitor)",
)
MAX_BYTES = int(os.getenv("CALL_IN_SCRAPE_MAX_BYTES", "750000"))


def _valid_http_url(url: str) -> bool:
    try:
        p = urlparse(url.strip())
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def _strip_html_to_text(html: str) -> str:
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"(?is)<noscript[^>]*>.*?</noscript>", " ", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _extract_tel_sms_hrefs(html: str) -> str:
    merged = ""
    for m in re.finditer(r'(?i)href\s*=\s*["\']([^"\']+)["\']', html):
        href = url_unquote(m.group(1).strip())
        low = href.lower()
        if not (low.startswith("tel:") or low.startswith("sms:")):
            continue
        body = href.split(":", 1)[-1] if ":" in href else href
        body = body.split(";")[0].split(",")[0].strip()
        if low.startswith("tel:"):
            disp = display_for_raw_phone_digits(body)
            if disp:
                merged = merge_call_in_strings(merged, disp)
        elif low.startswith("sms:"):
            digits = re.sub(r"\D", "", body)
            if len(digits) in (5, 6):
                merged = merge_call_in_strings(merged, f"Text {digits}")
    return merged


def scrape_station_call_ins(page_url: str, timeout: float = 18.0) -> tuple[str, str]:
    """
    Returns (display_string, error_message).
    error_message is empty when the HTTP request succeeded (even if nothing was found).
    """
    u = (page_url or "").strip()
    if not u:
        return "", ""
    if not _valid_http_url(u):
        return "", "URL must start with http:// or https://"

    try:
        r = requests.get(
            u,
            timeout=timeout,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            },
            allow_redirects=True,
        )
        r.raise_for_status()
        raw = r.content[:MAX_BYTES]
        enc = r.encoding or "utf-8"
        html = raw.decode(enc, errors="replace")
    except requests.RequestException as exc:
        return "", str(exc)[:140]
    except Exception as exc:
        return "", str(exc)[:140]

    from_href = _extract_tel_sms_hrefs(html)
    visible = _strip_html_to_text(html)
    chunk = visible[:250000] if visible else ""
    from_text = best_call_in_display_string(chunk) if chunk else ""
    out = merge_call_in_strings(from_href, from_text)
    return out.strip(), ""
