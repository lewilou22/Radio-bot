"""Parse fmstream.org listing pages (embedded ``var data=``) into direct stream URLs.

The site embeds stream endpoints in HTML; see https://fmstream.org/index.php .
Protocol selection matches rsd.js: ``prtcl[row[7] & 7] + '://' + row[0]``.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

# Same order as ``prtcl`` in https://fmstream.org/rsd.js
_PRTCL = ("http", "https", "mms", "mmsh", "rtsp", "rtmp")
# Indices from rsd.js: kc=1, kb=2, k2=7, kr=10
_KC = 1
_KB = 2
_K2 = 7
_KR = 10

USER_AGENT = "Mozilla/5.0 (compatible; RadioMonitor/1.0)"
# Directory search (see https://fmstream.org/index.php?s= ); site requires ≥3 characters.
FMSTREAM_SEARCH_PREFIX = "https://fmstream.org/index.php?s="


def fmstream_search_url(query: str) -> str:
    """Build the GET URL for a search on fmstream.org (``s`` query parameter)."""
    return FMSTREAM_SEARCH_PREFIX + quote(query.strip(), safe="")


@dataclass(frozen=True)
class FmStreamOption:
    """One playable HTTP(S) stream row for a station."""

    url: str
    codec: str
    bitrate: int | None
    label: str


def _js_array_to_python(s: str) -> str:
    s = re.sub(r"\bundefined\b", "None", s)
    while ",," in s:
        s = s.replace(",,", ",None,")
    s = re.sub(r"\[\s*,", "[None,", s)
    s = re.sub(r",\s*\]", ",None]", s)
    return s


def extract_raw_data_array(html: str) -> list[Any]:
    m = re.search(r"var\s+data\s*=\s*", html, re.IGNORECASE)
    if not m:
        raise ValueError(
            "No station data on this page (expected JavaScript var data=). "
            "Use a listing or search results URL from fmstream.org."
        )
    start = m.end()
    if start >= len(html) or html[start] != "[":
        raise ValueError("Malformed var data= assignment.")
    depth = 0
    i = start
    while i < len(html):
        if html[i] == "[":
            depth += 1
        elif html[i] == "]":
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    else:
        raise ValueError("Unclosed var data array.")
    blob = html[start:i]
    try:
        return ast.literal_eval(_js_array_to_python(blob))
    except (SyntaxError, ValueError) as e:
        raise ValueError(f"Could not parse stream data: {e}") from e


def extract_station_names(html: str) -> list[str]:
    raw = re.findall(r'<h3 class="stn">([^<]+)</h3>', html)
    return [re.sub(r"\s+", " ", x.strip()) for x in raw]


def parse_fmstream_page(html: str) -> tuple[list[list[Any]], list[str]]:
    data = extract_raw_data_array(html)
    names = extract_station_names(html)
    if len(names) != len(data):
        names = [f"Station {i + 1}" for i in range(len(data))]
    return data, names


def fetch_fmstream_page(url: str, timeout: float = 60.0) -> str:
    r = requests.get(url.strip(), headers={"User-Agent": USER_AGENT}, timeout=timeout)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def _scheme_for_row(row: list[Any]) -> str:
    if len(row) <= _K2 or row[_K2] is None:
        flag = 0
    else:
        try:
            flag = int(row[_K2])
        except (TypeError, ValueError):
            flag = 0
    idx = flag & 7
    if idx >= len(_PRTCL):
        idx = 0
    return _PRTCL[idx]


def stream_url(row: list[Any]) -> str:
    if not row or not row[0]:
        return ""
    host_path = row[0]
    if not isinstance(host_path, str):
        return ""
    path = host_path.replace("\\/", "/")
    return f"{_scheme_for_row(row)}://{path}"


def _codec_str(row: list[Any]) -> str:
    if len(row) <= _KC or row[_KC] is None:
        return ""
    return str(row[_KC]).strip().lower()


def _bitrate(row: list[Any]) -> int | None:
    if len(row) <= _KB or row[_KB] is None:
        return None
    try:
        return int(row[_KB])
    except (TypeError, ValueError):
        return None


def _region(row: list[Any]) -> str:
    if len(row) <= _KR or row[_KR] is None:
        return ""
    s = str(row[_KR]).strip()
    return s


_EXCLUDE_URL_MARKERS = (".mpd", ".m3u8", "master.m3u8")


def _url_ok_for_monitor(url: str) -> bool:
    if not url.startswith(("http://", "https://")):
        return False
    low = url.lower()
    return not any(m in low for m in _EXCLUDE_URL_MARKERS)


def _codec_ok(codec: str, mp3_only: bool) -> bool:
    if mp3_only:
        return codec == "mp3"
    if not codec:
        return False
    if codec in ("2", "3", "hls", "mpd", "pls", "m3u", "asp", "asx", "ram", "wax", "wvx", "txt", "tex"):
        return False
    if codec == "mp3":
        return True
    if codec in ("1", "aac", "ogg", "opus", "flc", "opu", "vor", "mp4"):
        return True
    if codec.isdigit():
        return int(codec) == 1
    return False


def stream_options_for_station(station_rows: list[Any], mp3_only: bool) -> list[FmStreamOption]:
    """Build deduplicated stream choices for one station (one ``data[i]`` list)."""
    out: list[FmStreamOption] = []
    seen: set[str] = set()
    for row in station_rows:
        if not isinstance(row, list):
            continue
        codec = _codec_str(row)
        if not _codec_ok(codec, mp3_only):
            continue
        url = stream_url(row)
        if not url or not _url_ok_for_monitor(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        br = _bitrate(row)
        reg = _region(row)
        parts = [codec] if codec else ["?"]
        if br is not None:
            parts.append(f"{br} kbps")
        if reg:
            parts.append(reg)
        label = " — ".join(parts)
        out.append(FmStreamOption(url=url, codec=codec or "?", bitrate=br, label=label))
    out.sort(key=lambda o: (-(o.bitrate or 0), o.label))
    return out
