"""Core radio monitor engine (recorders, transcribers, keyword logic)."""

from __future__ import annotations

from collections.abc import Callable
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from queue import Empty, Queue
import re

import requests
from fuzzysearch import find_near_matches
from zoneinfo import ZoneInfo

import config
from call_in_numbers import best_call_in_display_string, merge_call_in_strings
from config import (
    ALERT_DEDUP_SECONDS,
    COMMERCIAL_AI_MIN_INTERVAL_SEC,
    COMMERCIAL_DETECT_MODEL,
    CONTEST_AI_MIN_INTERVAL_SEC,
    DELAY_SMOOTHING_ALPHA,
    MAX_DELAY_SYNC_SECONDS,
    MODEL_SIZE,
    GROQ_CONTEST_MODEL,
    StationConfig,
    VAD_FILTER,
    WHISPER_COMPUTE_TYPE,
    WHISPER_DEVICE,
    load_stations,
)
from transcribers import FasterWhisperTranscriber, Transcriber
import commercial_memory
import song_play_history
from contest_memory import fetch_few_shot_messages, record_positive as record_contest_learn_positive
from transcript_db import append_transcript

_COMMERCIAL_CLASSIFY_SYSTEM = (
    "You classify short radio transcriptions. Reply with exactly YES only if this is "
    "clearly a paid advertisement or sponsored spot: selling a product/service, "
    "direct-response ad, dealer/auto/furniture/mattress read, insurance/legal/medical ad, "
    "or a third-party brand promo with a buy/call/visit CTA.\n"
    "Reply exactly NO for: station contest or giveaway instructions, call-in shows, "
    "DJ/host chatter, introducing songs, news/weather/traffic, sports talk, station IDs, "
    "PSAs, charity drives without a product pitch, song lyrics, interviews, or "
    "programming promos for the station's own shows (unless the clip is mostly a "
    "third-party ad read)."
)


def _commercial_classify_messages(text: str, station_name: str) -> list[dict]:
    system = (
        _COMMERCIAL_CLASSIFY_SYSTEM
        + "\n\nLabeled examples follow (assistant answered YES or NO). "
        "Apply the same rule to the final excerpt."
    )
    few = commercial_memory.fetch_few_shot_messages(station_name)
    messages: list[dict] = [{"role": "system", "content": system}]
    messages.extend(few)
    messages.append(
        {
            "role": "user",
            "content": "Classify this radio transcript excerpt. Reply YES or NO only.\n\n"
            + (text or "").strip()[:1500],
        }
    )
    return messages

def setup_log() -> logging.Logger:
    logger = logging.getLogger("radio-monitor")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    # Read paths at call time so Kivy mobile can set_mobile_base_dir() before MonitorController().
    log_dir = config.LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    fh = logging.FileHandler(log_dir / f"gui_monitor_{datetime.utcnow().date().isoformat()}.log")
    fh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.addHandler(fh)
    return logger


# Words of transcript around a keyword match (blacklist + Discord + alert snippet).
KEYWORD_CONTEXT_WORDS_EACH_SIDE = 40
# Short first chunks (or sparse speech): buffer transcripts until keyword scan has enough text.
MIN_TRANSCRIPT_WORDS_BEFORE_KEYWORD_SCAN = 20
MAX_SHORT_CHUNKS_TO_ACCUMULATE = 12

_COMMERCIAL_AI_LOCK = threading.Lock()
_COMMERCIAL_AI_LAST_TS: dict[str, float] = {}
_COMMERCIAL_GROQ_LOCK = threading.Lock()
_COMMERCIAL_GROQ_LAST_TS: dict[str, float] = {}


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", text.strip()))


@dataclass
class MatchEvent:
    station: str
    term: str
    context_text: str
    is_live: bool
    fuzzy_max_distance: int = 1


class TranscriptMergeState:
    """Order chunks by sequence number and merge short transcripts before keyword matching."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending_by_seq: dict[str, dict[int, str]] = {}
        self._next_seq: dict[str, int] = {}
        self._carry: dict[str, str] = {}
        self._carry_chunk_count: dict[str, int] = {}

    def reset(self) -> None:
        with self._lock:
            self._pending_by_seq.clear()
            self._next_seq.clear()
            self._carry.clear()
            self._carry_chunk_count.clear()

    def feed(self, station_name: str, chunk_seq: int, whisper_text: str) -> str | None:
        """Return text to run keyword matching on, or None while waiting for more chunks."""
        text = whisper_text.strip()
        with self._lock:
            pending = self._pending_by_seq.setdefault(station_name, {})
            pending[chunk_seq] = text
            expected = self._next_seq.get(station_name, 0)
            newly_drained = 0
            while expected in pending:
                fragment = pending.pop(expected).strip()
                expected += 1
                newly_drained += 1
                if fragment:
                    prev = self._carry.get(station_name, "").strip()
                    self._carry[station_name] = (prev + " " + fragment).strip() if prev else fragment
            self._next_seq[station_name] = expected
            if newly_drained == 0:
                return None
            cc = self._carry_chunk_count.get(station_name, 0) + newly_drained
            self._carry_chunk_count[station_name] = cc
            carry = self._carry.get(station_name, "").strip()
            if not carry:
                if cc >= MAX_SHORT_CHUNKS_TO_ACCUMULATE:
                    self._carry_chunk_count[station_name] = 0
                return None
            wc = _word_count(carry)
            force = cc >= MAX_SHORT_CHUNKS_TO_ACCUMULATE
            if wc < MIN_TRANSCRIPT_WORDS_BEFORE_KEYWORD_SCAN and not force:
                return None
            self._carry[station_name] = ""
            self._carry_chunk_count[station_name] = 0
            return carry


def parse_icy_stream_title(metadata_text: str) -> str:
    match = re.search(r"StreamTitle='([^']*)';", metadata_text)
    if not match:
        return ""
    return match.group(1).strip()


def looks_like_song_title(title: str) -> bool:
    if not title:
        return False
    lowered = title.lower()
    if lowered in ("-", "unknown", "advertisement", "adswizz"):
        return False
    if any(
        x in lowered
        for x in (
            "advertisement",
            "commercial break",
            "commercial",
            "comercial",
            "sponsor",
            "ad break",
            "traffic and weather",
            "news at",
            "breaking news",
        )
    ):
        return False
    # Common music title patterns from radio metadata.
    return " - " in title or " by " in lowered


# Words too common in DJ patter to use for "does this sound like the song title?"
_MUSIC_OVERLAP_STOP = frozenset(
    """
    the a an and or but in on at to for of is it as if so than then that this these those
    you we i me my your our they them their he she his her its was were are am be been being
    have has had do does did done will would could should can may might must not no yes just
    like get got go going went come came take took make made way back out up down here there
    when what who how why where which some any all each every both few more most other such
    very much too also only own same so than too very just don doesnt didnt wont cant im youre
    were gonna wanna gonna cause cause cos bout about into over off again once twice ever never
    uh um oh hey hi hello yeah yep nah okay ok well right left big small long uh-huh mm-hmm
    listen live local now next right here coming station radio show morning afternoon evening
    """.split()
)


def _meaningful_tokens(text: str) -> list[str]:
    return [
        w
        for w in re.findall(r"[a-z0-9']+", text.lower())
        if len(w) >= 2 and w not in _MUSIC_OVERLAP_STOP
    ]


def _icy_title_token_set(icy_title: str) -> set[str]:
    parts = re.split(r"\s+-\s+|\s+by\s+", icy_title, maxsplit=3, flags=re.IGNORECASE)
    out: set[str] = set()
    for p in parts:
        out.update(_meaningful_tokens(p))
    return out


def icy_and_transcript_suggest_music_lyrics(transcript: str, icy_title: str) -> bool:
    """True if ICY looks like a track *and* the transcribed words fit that track.

    Stations often leave the last song title in metadata during DJ breaks; this compares
    Whisper text to artist/title tokens so announcer chatter is not styled as lyrics.
    """
    icy = icy_title.strip()
    if not icy or not looks_like_song_title(icy):
        return False
    t = transcript.strip()
    if not t:
        return True
    tw = _meaningful_tokens(t)
    wc = len(re.findall(r"\S+", t))
    if wc < 8 or len(tw) < 4:
        return True
    title_toks = _icy_title_token_set(icy)
    if len(title_toks) < 2:
        return True
    hits = sum(1 for w in tw if w in title_toks)
    ratio = hits / len(tw)
    # Long patter with almost no words from the supposed title → DJ talk, stale ICY.
    if wc >= 14 and ratio < 0.09:
        return False
    if wc >= 20 and ratio < 0.12:
        return False
    if wc >= 12 and hits == 0 and len(title_toks) >= 4:
        return False
    return True


def icy_suggests_commercial(title: str) -> bool:
    """Stream metadata often labels ad breaks explicitly."""
    if not title.strip():
        return False
    t = title.lower()
    markers = (
        "advertisement",
        "advert",
        "commercial",
        "comercial",
        "sponsor",
        "sponsored",
        "promo only",
        "ad break",
        "break for messages",
        "paid promotion",
        "paid advertisement",
        "spots",
        "adswizz",
        "ad break",
        "message from our",
        "word from our sponsor",
        "we'll be right back",
        "we will be right back",
        "stay tuned",
        "don't go away",
        "your ad could be here",
        "we'll return after these",
        "we will return after these",
        "messages from our sponsors",
        "these messages",
        "promotional announcement",
        "back after these",
        "after these messages",
        "when we return",
        "more music in a moment",
        "local ads",
        "message from",
        "messages from",
        "support for this program",
        "funding for this program",
        "programming note",
        "eight hundred",
        "nine hundred",
        "one eight hundred",
    )
    return any(m in t for m in markers)


_COMMERCIAL_TRANSCRIPT_PHRASES = (
    "brought to you by",
    "sponsored by",
    "paid for by",
    "paid advertisement",
    "advertisement for",
    "message from our sponsor",
    "promotional consideration",
    "toll-free",
    "toll free",
    "visit us at",
    "visit our website",
    "go to www",
    "dot com slash",
    "limited time offer",
    "act now",
    "order now",
    "operators are standing",
    "side effects include",
    "ask your doctor",
    "terms and conditions",
    "financing available",
    "no money down",
    "while supplies last",
    "money back guarantee",
    "not available in all states",
    "must be 18",
    "results may vary",
    "use promo code",
    "promo code",
    "mention this ad",
    "mention this spot",
    "apr as low as",
    "no payments for",
    "money down",
    "warranty included",
    "extended warranty",
    "rebate",
    "participating locations",
    "available at participating",
    "batteries not included",
    "void where prohibited",
    "see store for details",
    "see dealer for details",
    "licensed professional",
    "insurance products",
    "call for a free quote",
    "schedule your free",
    "risk free trial",
    "supplies are limited",
    "first time callers",
    "have your credit card",
    "prescription only",
    "prescription drug",
    "fda has approved",
    "not for everyone",
    "do not take if",
    "stop taking",
    "may cause",
    "common side effects",
    "talk to your doctor",
    "ask your healthcare",
    "results not typical",
    "call eight hundred",
    "dial one eight hundred",
    "licensed agent",
    "get your free estimate",
    "zero percent apr",
    "percent apr",
    "per month for",
    "per week for",
    "for only nineteen ninety nine",
    "for just nineteen ninety five",
    "but wait there's more",
    "but wait there is more",
    "call in the next",
    "in the next fifteen minutes",
    "supplies running out",
    "going fast",
    "inventory is limited",
    "certified pre-owned",
    "see us today",
    "hurry in",
    "ends sunday",
    "ends this weekend",
    "black friday",
    "door crasher",
    "doorbuster",
    "compare at",
    "regularly priced",
    "per month lease",
    "per month with approved",
)

# Strong CTA lines — often ads; "call now" alone matches too many contests/DJ lines.
_COMMERCIAL_CTA_PHRASES = (
    "call now and",
    "call today and",
    "call 1-800",
    "call 1 800",
    "call eight hundred",
    "pick up the phone and order",
    "log on today",
    "click the link",
    "download the app today",
)


def heuristic_transcript_suggests_commercial(text: str) -> bool:
    if len(text.strip()) < 16:
        return False
    tl = text.lower()
    if any(p in tl for p in _COMMERCIAL_TRANSCRIPT_PHRASES):
        return True
    if any(p in tl for p in _COMMERCIAL_CTA_PHRASES):
        return True
    if re.search(r"\b1[-.\s]?8(00|88|77|66|55)\b", tl) and any(
        w in tl
        for w in (
            "order",
            "save",
            "offer",
            "deal",
            "today",
            "free shipping",
            "appointment",
            "quote",
            "insurance",
            "warranty",
        )
    ):
        return True
    if re.search(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b", tl) and any(
        w in tl for w in ("order", "appointment", "quote", "deal", "save", "offer", "insurance", "dealer")
    ):
        return True
    return False


def _openai_classify_commercial(text: str, station_name: str = "") -> bool:
    if not (os.getenv("OPENAI_API_KEY") or "").strip():
        return False
    payload = {
        "model": COMMERCIAL_DETECT_MODEL,
        "messages": _commercial_classify_messages(text, station_name),
        "max_tokens": 6,
        "temperature": 0,
    }
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {(os.getenv('OPENAI_API_KEY') or '').strip()}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=16,
    )
    r.raise_for_status()
    data = r.json()
    msg = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    first = (msg.strip().upper().split() or [""])[0]
    return first == "YES"


def maybe_openai_commercial(station_name: str, text: str) -> bool:
    """Throttled API call so workers do not hammer OpenAI every chunk."""
    if not (os.getenv("OPENAI_API_KEY") or "").strip() or len(text.strip()) < 55:
        return False
    now = time.time()
    with _COMMERCIAL_AI_LOCK:
        last = _COMMERCIAL_AI_LAST_TS.get(station_name, 0.0)
        if now - last < COMMERCIAL_AI_MIN_INTERVAL_SEC:
            return False
        _COMMERCIAL_AI_LAST_TS[station_name] = now
    try:
        ok = _openai_classify_commercial(text, station_name)
        if ok:
            commercial_memory.record_positive(station_name, text, "openai_yes")
        return ok
    except Exception:
        return False


def _groq_classify_commercial(text: str, station_name: str = "") -> bool:
    """Same rubric as OpenAI path; uses Groq when no OpenAI key (e.g. Android)."""
    if not (os.getenv("GROQ_API_KEY") or "").strip():
        return False
    model = os.getenv("COMMERCIAL_DETECT_GROQ_MODEL", GROQ_CONTEST_MODEL).strip()
    payload = {
        "model": model,
        "messages": _commercial_classify_messages(text, station_name),
        "max_tokens": 6,
        "temperature": 0,
    }
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {(os.getenv('GROQ_API_KEY') or '').strip()}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=22,
    )
    r.raise_for_status()
    data = r.json()
    msg = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    first = (msg.strip().upper().split() or [""])[0]
    return first == "YES"


def maybe_groq_commercial(station_name: str, text: str) -> bool:
    if not (os.getenv("GROQ_API_KEY") or "").strip() or len(text.strip()) < 55:
        return False
    now = time.time()
    with _COMMERCIAL_GROQ_LOCK:
        last = _COMMERCIAL_GROQ_LAST_TS.get(station_name, 0.0)
        if now - last < COMMERCIAL_AI_MIN_INTERVAL_SEC:
            return False
        _COMMERCIAL_GROQ_LAST_TS[station_name] = now
    try:
        ok = _groq_classify_commercial(text, station_name)
        if ok:
            commercial_memory.record_positive(station_name, text, "groq_yes")
        return ok
    except Exception:
        return False


def detect_commercial_break(icy_title: str, transcript: str, station_name: str) -> bool:
    """ICY markers + phrase heuristics + optional OpenAI or Groq classification."""
    if song_play_history.icy_title_is_user_marked_ad(icy_title or ""):
        return True
    if icy_suggests_commercial(icy_title or ""):
        return True
    tx = (transcript or "").strip()
    if commercial_memory.transcript_matches_saved_ad_positive(station_name, tx):
        return True
    if heuristic_transcript_suggests_commercial(tx):
        return True
    if (os.getenv("OPENAI_API_KEY") or "").strip():
        return maybe_openai_commercial(station_name, tx)
    return maybe_groq_commercial(station_name, tx)


def _monitor_prefs_file() -> Path:
    return config.DATA_DIR / "monitor_prefs.json"


AI_CONTEST_ALERT_TERM = "(Groq AI) contest / text-to-win / call-in cue"

_CONTEST_GROQ_LOCK = threading.Lock()
_CONTEST_GROQ_LAST_TS: dict[str, float] = {}


def _load_monitor_prefs() -> dict:
    try:
        mpf = _monitor_prefs_file()
        if mpf.is_file():
            with mpf.open("r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_monitor_prefs(prefs: dict) -> None:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _monitor_prefs_file().open("w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2)
    except OSError:
        pass


_CONTEST_ENTRY_GATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Call-in mechanics
    re.compile(
        r"\b(be|you'?re|you are|we need)\s+the\s+\d{1,2}(st|nd|rd|th)?\s+caller\b",
        re.I,
    ),
    re.compile(r"\bcaller\s*(#|number|no\.?)\s*\d+", re.I),
    re.compile(r"\bcaller\s+\d+\b", re.I),
    re.compile(r"\b(first|second|third|fourth|fifth|1st|2nd|3rd)\s+caller\b", re.I),
    re.compile(r"\b(lines?\s+are)\s+open\b", re.I),
    re.compile(r"\bcall\s+the\s+studio\b", re.I),
    re.compile(r"\bstudio\s*(line|lines|phone|number)\b", re.I),
    re.compile(r"\bphone\s+in\b.*\b(win|contest|ticket|prize|studio)\b", re.I),
    re.compile(r"\bcall\s+in\b.{0,60}\b(win|contest|tickets?|prize|studio\s*line)\b", re.I),
    # Text / SMS entry
    re.compile(r"\btext[\s-]+to[\s-]+win\b", re.I),
    re.compile(r"\btext\s+(us|in|to|your|the|our)\b", re.I),
    re.compile(r"\b(sms|message)\s+us\b", re.I),
    re.compile(r"\b(keyword|code\s*word|winning\s+word|magic\s+word)\b", re.I),
    re.compile(r"\benter\s+to\s+win\b", re.I),
    re.compile(r"\bsend\s+(your\s+)?(text|message|keyword)\b", re.I),
    # Station contest framing (with entry implied nearby)
    re.compile(r"\bsecret\s+sound\b", re.I),
    re.compile(r"\bprize\s*vault\b", re.I),
    re.compile(r"\bour\s+contest\b.{0,80}\b(call|text|line|keyword|enter|win)\b", re.I),
    re.compile(r"\bcontest\s*line\b", re.I),
)

# If these dominate, it is probably news/sports/ads — skip Groq to cut false positives.
_CONTEST_NEGATIVE_HINTS = (
    "power play",
    "penalty kill",
    "touchdown",
    "quarterback",
    "stanley cup",
    "playoff",
    "lottery numbers",
    "jackpot winner",
    "winning numbers",
    "stock market",
    "dow jones",
    "weather warning",
    "traffic on the",
    "brought to you by",
    "sponsored by",
    "side effects",
    "ask your doctor",
)


def transcript_suggests_contest_entry_mechanic(text: str) -> bool:
    """Narrow pre-check: real call-in / text-in / keyword entry language before calling Groq."""
    t = text.strip()
    if len(t) < 28:
        return False
    tl = t.lower()
    if sum(1 for h in _CONTEST_NEGATIVE_HINTS if h in tl) >= 2:
        return False
    return any(p.search(t) for p in _CONTEST_ENTRY_GATE_PATTERNS)


def groq_classify_contest_cue(text: str, station_name: str = "") -> bool:
    """Groq free tier (https://console.groq.com) — OpenAI-compatible chat completions.

    Few-shot examples from ``contest_memory`` (auto + user feedback) are injected when available.
    """
    if not (os.getenv("GROQ_API_KEY") or "").strip():
        return False
    snippet = text.strip()[:2000]
    if len(snippet) < 28:
        return False
    system = (
        "You verify radio transcriptions for STATION-RUN listener contests only.\n"
        "Reply exactly YES only if the host is giving (or repeating) instructions for listeners "
        "to ENTER via: calling the station/studio line, being a numbered caller, texting/SMS "
        "a keyword or short code, or another explicit on-air entry mechanic for this station's "
        "contest or giveaway.\n"
        "Reply exactly NO for: music or lyrics; news; traffic/weather; sports results or "
        "\"they won the game\"; discussing past winners without entry instructions; movie/TV "
        "or third-party product ads; generic \"you could win\" with no call/text/keyword step; "
        "charity telethons; station imaging with no entry; DJ chat that only mentions \"prize\" "
        "or \"tickets\" without how to enter; lottery or casino ads; anything that is primarily "
        "a commercial for a non-station brand.\n\n"
        "You may see labeled examples (assistant replied YES or NO). Match that same YES/NO style "
        "on the final excerpt."
    )
    few = fetch_few_shot_messages(station_name)
    messages: list[dict] = [{"role": "system", "content": system}]
    messages.extend(few)
    messages.append(
        {
            "role": "user",
            "content": "Classify this radio transcript excerpt. Reply YES or NO only.\n\n" + snippet,
        }
    )
    payload = {
        "model": GROQ_CONTEST_MODEL,
        "messages": messages,
        "max_tokens": 6,
        "temperature": 0,
    }
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {(os.getenv('GROQ_API_KEY') or '').strip()}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=22,
    )
    r.raise_for_status()
    data = r.json()
    msg = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    first = (msg.strip().upper().split() or [""])[0]
    return first == "YES"


def maybe_groq_contest_scan(station_name: str, text: str) -> bool:
    if not (os.getenv("GROQ_API_KEY") or "").strip() or len(text.strip()) < 48:
        return False
    if not transcript_suggests_contest_entry_mechanic(text):
        return False
    now = time.time()
    with _CONTEST_GROQ_LOCK:
        last = _CONTEST_GROQ_LAST_TS.get(station_name, 0.0)
        if now - last < CONTEST_AI_MIN_INTERVAL_SEC:
            return False
        _CONTEST_GROQ_LAST_TS[station_name] = now
    try:
        return groq_classify_contest_cue(text, station_name)
    except Exception:
        return False


@dataclass
class StationRuntimeState:
    """Live per-station UI state (ICY metadata, delay estimate, music vs DJ inference). Not saved to disk."""

    icy_title: str = ""
    delay_smoothed_sec: float | None = None
    delay_last_heard: str = ""
    dj_talk_overlay: bool = False
    commercial_break: bool = False
    weather_temp: str | None = None
    weather_condition: str | None = None
    weather_text_tail: str = ""
    gas_summary: str | None = None
    gas_text_tail: str = ""
    call_in_tail: str = ""
    call_in_from_air: str = ""
    call_in_from_scrape: str = ""
    call_in_scrape_err: str = ""
    call_in_scrape_ts: float = 0.0


class StationRuntimeIndex:
    """One index for all runtime dicts that were previously scattered on the app (keyed by station name)."""

    __slots__ = ("_by_name",)

    def __init__(self) -> None:
        self._by_name: dict[str, StationRuntimeState] = {}

    def for_station(self, name: str) -> StationRuntimeState:
        key = name.strip()
        if key not in self._by_name:
            self._by_name[key] = StationRuntimeState()
        return self._by_name[key]

    def clear(self) -> None:
        self._by_name.clear()

    def prune(self, known_names: set[str]) -> None:
        """Drop state for removed or renamed stations."""
        self._by_name = {k: v for k, v in self._by_name.items() if k in known_names}

    def set_icy_title(self, station_name: str, title: str) -> None:
        st = self.for_station(station_name)
        st.icy_title = title
        st.dj_talk_overlay = False
        # Only ICY can *assert* commercial here. Do not force False: metadata often
        # still shows the last song during spots, which was clearing transcript/AI detection.
        if icy_suggests_commercial(title) or song_play_history.icy_title_is_user_marked_ad(title):
            st.commercial_break = True

    def set_commercial_break(self, station_name: str, value: bool) -> None:
        self.for_station(station_name).commercial_break = bool(value)

    def apply_speech_music_hint(self, station_name: str, is_music: bool) -> None:
        st = self.for_station(station_name)
        icy = st.icy_title
        if icy.strip() and looks_like_song_title(icy):
            st.dj_talk_overlay = not is_music
        else:
            st.dj_talk_overlay = False

    def apply_stream_delay(self, station_name: str, raw_seconds: float, heard: str) -> None:
        st = self.for_station(station_name)
        prev = st.delay_smoothed_sec
        if prev is None:
            st.delay_smoothed_sec = raw_seconds
        else:
            st.delay_smoothed_sec = (
                DELAY_SMOOTHING_ALPHA * raw_seconds + (1.0 - DELAY_SMOOTHING_ALPHA) * prev
            )
        st.delay_last_heard = heard

    def feed_weather_from_transcript(
        self, station_name: str, text: str, is_music: bool, tz_name: str
    ) -> bool:
        """Accumulate speech tail and parse weather; returns True if temp/condition changed."""
        if is_music or not (text or "").strip():
            return False
        st = self.for_station(station_name)
        tail = (st.weather_text_tail + " " + text.strip()).strip()
        if len(tail) > 900:
            tail = tail[-900:]
        st.weather_text_tail = tail
        prev_t, prev_c = st.weather_temp, st.weather_condition
        t, c = extract_on_air_weather(tail, tz_name)
        changed = False
        if t is not None and t != prev_t:
            st.weather_temp = t
            changed = True
        if c is not None and c != prev_c:
            st.weather_condition = c
            changed = True
        return changed

    def feed_call_in_from_transcript(self, station_name: str, text: str, is_music: bool) -> bool:
        """Scan speech for studio / contest phone or SMS codes; merge into live display string."""
        if is_music or not (text or "").strip():
            return False
        st = self.for_station(station_name)
        if st.commercial_break:
            return False
        tail = (st.call_in_tail + " " + text.strip()).strip()
        if len(tail) > 1100:
            tail = tail[-1100:]
        st.call_in_tail = tail
        found = best_call_in_display_string(tail)
        if not found:
            return False
        merged = merge_call_in_strings(st.call_in_from_air, found)
        if merged == st.call_in_from_air:
            return False
        st.call_in_from_air = merged
        return True

    def feed_call_in_from_icy(self, station_name: str, title: str) -> bool:
        """Sometimes promos put a phone or short code in stream title metadata."""
        t = (title or "").strip()
        if not t:
            return False
        if icy_suggests_commercial(t):
            return False
        found = best_call_in_display_string(t)
        if not found:
            return False
        st = self.for_station(station_name)
        merged = merge_call_in_strings(st.call_in_from_air, found)
        if merged == st.call_in_from_air:
            return False
        st.call_in_from_air = merged
        return True

    def feed_gas_from_transcript(self, station_name: str, text: str, is_music: bool) -> bool:
        """Accumulate speech tail and parse gas prices; returns True if summary changed."""
        if is_music or not (text or "").strip():
            return False
        st = self.for_station(station_name)
        tail = (st.gas_text_tail + " " + text.strip()).strip()
        if len(tail) > 800:
            tail = tail[-800:]
        st.gas_text_tail = tail
        prev = st.gas_summary
        g = extract_on_air_gas_summary(tail)
        if g is None:
            return False
        if g != prev:
            st.gas_summary = g
            return True
        return False


def _zoneinfo_or_toronto(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo((tz_name or "America/Toronto").strip())
    except Exception:
        return ZoneInfo("America/Toronto")


def _hour_minute_candidates(hour: int, minute: int, ampm: str | None) -> list[tuple[int, int]]:
    """24h (hour, minute) candidates for a spoken time."""
    if minute > 59:
        return []
    if ampm:
        if hour < 1 or hour > 12:
            return []
        h24 = hour
        if ampm == "pm" and h24 != 12:
            h24 += 12
        if ampm == "am" and h24 == 12:
            h24 = 0
        return [(h24, minute)]
    if hour > 23:
        return []
    if hour > 12:
        return [(hour, minute)]
    if hour == 12:
        return [(12, minute), (0, minute)]
    if hour == 0:
        return [(0, minute)]
    return [(hour, minute), (hour + 12, minute)]


def _delay_for_clock_time(now_local: datetime, h24: int, minute: int) -> float | None:
    """Return delay only if stream is 0..MAX_DELAY_SYNC_SECONDS behind (rejects wild false positives)."""
    best: float | None = None
    for day_off in (-1, 0, 1):
        cand = (now_local + timedelta(days=day_off)).replace(
            hour=h24, minute=minute, second=0, microsecond=0
        )
        d = (now_local - cand).total_seconds()
        if 0 <= d <= MAX_DELAY_SYNC_SECONDS:
            if best is None or d < best:
                best = d
    return best


def extract_spoken_time_delays(text: str, tz_name: str) -> list[tuple[float, str]]:
    """Parse spoken clock times from transcript; return (delay_sec, display) each.

    Recognizes forms like 3:45, 3.45, 15:30, 3 o'clock, **compact digits** (1134 → 11:34),
    and spaced digits (11 34). Only delays in ``0..MAX_DELAY_SYNC_SECONDS`` (default 5 minutes)
    are kept so random numbers do not skew the estimate.
    """
    if not text or not text.strip():
        return []
    tz = _zoneinfo_or_toronto(tz_name)
    now_local = datetime.now(tz)
    text_l = text.lower()
    found: list[tuple[float, str]] = []

    def _push_delays(hour: int, minute: int, ampm: str | None, display: str) -> None:
        for h24, mn in _hour_minute_candidates(hour, minute, ampm):
            d = _delay_for_clock_time(now_local, h24, mn)
            if d is not None:
                found.append((d, display))

    for m in re.finditer(r"\b(\d{1,2})[:\.](\d{2})(?:\s*(am|pm))?\b", text_l):
        hour, minute = int(m.group(1)), int(m.group(2))
        _push_delays(hour, minute, m.group(3), m.group(0).strip())

    for m in re.finditer(r"\b(\d{1,2})\s*o'?clock\s*(am|pm)?\b", text_l):
        hour = int(m.group(1))
        if hour < 1 or hour > 12:
            continue
        _push_delays(hour, 0, m.group(2), m.group(0).strip())

    # Spoken digits with a space, no colon: "11 34", "09 30", "9 05 pm"
    for m in re.finditer(r"\b(2[0-3]|[01]\d|\d)\s+([0-5]\d)(?:\s*(am|pm))?\b", text_l):
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23:
            continue
        _push_delays(hour, minute, m.group(3), m.group(0).strip())

    # Compact HHMM (no separator), e.g. Whisper → "1134" for "eleven thirty-four"
    for m in re.finditer(r"\b(2[0-3]|[01]\d)([0-5]\d)\b", text_l):
        token = m.group(0)
        if len(token) == 4 and token.isdigit():
            y = int(token)
            if 1900 <= y <= 2099:
                continue
        hour, minute = int(m.group(1)), int(m.group(2))
        display = f"{hour}:{minute:02d}"
        _push_delays(hour, minute, None, display)

    # Compact HMM (3 digits), e.g. "934" → 9:34; "123" → 1:23; "250" → 2:50
    for m in re.finditer(r"\b(1[0-2]|[1-9])([0-5]\d)\b", text_l):
        hour, minute = int(m.group(1)), int(m.group(2))
        display = f"{hour}:{minute:02d}"
        _push_delays(hour, minute, None, display)

    return found


def fresh_station_config(st: StationConfig) -> StationConfig:
    """Reload station from stations.json so keyword edits apply without restarting."""
    try:
        for s in load_stations():
            if s.name == st.name:
                return s
    except OSError:
        pass
    return st


def best_delay_sample_from_transcript(text: str, tz_name: str) -> tuple[float, str] | None:
    """Single best (smallest plausible) delay for this chunk, or None."""
    pairs = extract_spoken_time_delays(text, tz_name)
    if not pairs:
        return None
    pairs.sort(key=lambda x: x[0])
    return pairs[0]


def _default_temp_unit_hint(tz_name: str) -> str:
    """Guess °C vs °F from IANA zone when the announcer omits the unit."""
    t = (tz_name or "").lower().replace(" ", "_")
    if t.startswith("america/"):
        for slug in (
            "new_york",
            "chicago",
            "denver",
            "los_angeles",
            "phoenix",
            "detroit",
            "indianapolis",
            "louisville",
            "memphis",
            "nashville",
            "atlanta",
            "miami",
            "dallas",
            "houston",
            "seattle",
            "boston",
            "anchorage",
            "honolulu",
            "puerto_rico",
            "boise",
            "billings",
            "omaha",
            "milwaukee",
            "minneapolis",
            "st_louis",
            "kansas_city",
            "salt_lake_city",
        ):
            if slug in t:
                return "F"
    return "C"


def _sanity_temp(n: int, unit: str) -> bool:
    if unit == "C":
        return -55 <= n <= 55
    return -65 <= n <= 130


_WEATHER_STRONG_PHRASES = (
    "weather",
    "forecast",
    "meteorologist",
    "humidex",
    "wind chill",
    "windchill",
    "traffic and weather",
    "weather together",
    "weather center",
    "your radar",
    "five-day",
    "five day",
    "7-day",
    "seven day",
    "environment canada",
    "ec weather",
)


def transcript_has_weather_context(tl: str) -> bool:
    if any(p in tl for p in _WEATHER_STRONG_PHRASES):
        return True
    if "degree" in tl or "degrees" in tl:
        if any(
            w in tl
            for w in (
                "outside",
                "high of",
                "low of",
                "highs in",
                "lows in",
                "temperature",
                "currently",
                "sitting at",
                "up to",
                "feels like",
                "afternoon",
                "morning low",
                "overnight",
            )
        ):
            return True
        if any(
            w in tl
            for w in (
                "cloud",
                "sunny",
                "sun ",
                " rain",
                "snow",
                "shower",
                "storm",
                "fog",
                "overcast",
                "clearing",
                "flurries",
                "breeze",
                "humid",
                "drizzle",
                "skies",
            )
        ):
            return True
    if ("high of" in tl or "low of" in tl) and (
        "degree" in tl or "cloud" in tl or "sun" in tl or "rain" in tl or "snow" in tl
    ):
        return True
    return False


_CONDITION_PHRASES: tuple[tuple[str, str], ...] = (
    ("partly sunny", "partly sunny"),
    ("mostly sunny", "mostly sunny"),
    ("partly cloudy", "partly cloudy"),
    ("mostly cloudy", "mostly cloudy"),
    ("mix of sun and cloud", "mix sun / cloud"),
    ("sun and cloud", "mix sun / cloud"),
    ("scattered showers", "scattered showers"),
    ("chance of showers", "chance of showers"),
    ("chance of rain", "chance of rain"),
    ("thunderstorms", "thunderstorms"),
    ("thunderstorm", "thunderstorms"),
    ("freezing rain", "freezing rain"),
    ("ice pellets", "ice pellets"),
    ("blowing snow", "blowing snow"),
    ("snow squall", "snow squalls"),
    ("light snow", "light snow"),
    ("heavy snow", "heavy snow"),
    ("rain showers", "rain showers"),
    ("clear skies", "clear"),
    ("clear sky", "clear"),
    ("mainly clear", "mainly clear"),
    ("mainly sunny", "mainly sunny"),
    ("sunny skies", "sunny"),
    ("mostly clear", "mostly clear"),
    ("cloudy skies", "cloudy"),
    ("mostly dry", "mostly dry"),
    ("patchy fog", "patchy fog"),
    ("dense fog", "dense fog"),
    ("overcast", "overcast"),
    ("windy conditions", "windy"),
    ("very windy", "windy"),
    ("gusty winds", "windy"),
)


def _extract_weather_condition_last(tl: str) -> str | None:
    best: str | None = None
    best_pos = -1
    for needle, label in _CONDITION_PHRASES:
        pos = tl.rfind(needle)
        if pos >= best_pos:
            best_pos = pos
            best = label
    singles: tuple[tuple[str, str], ...] = (
        ("raining", "rain"),
        ("snowing", "snow"),
        ("cloudy", "cloudy"),
        ("breezy", "breezy"),
        ("clearing", "clearing"),
        ("humid", "humid"),
        ("foggy", "fog"),
    )
    for needle, label in singles:
        if re.search(rf"\b{re.escape(needle)}\b", tl):
            pos = tl.rfind(needle)
            if pos >= best_pos:
                best_pos = pos
                best = label
    if best is None and re.search(r"\bsunny\b", tl) and "cloud" not in tl:
        return "sunny"
    return best


def _extract_weather_temperature_last(tl: str, tz_name: str) -> str | None:
    hint = _default_temp_unit_hint(tz_name)
    best_end = -1
    best_fmt: str | None = None

    def consider(end: int, fmt: str) -> None:
        nonlocal best_end, best_fmt
        if end > best_end:
            best_end = end
            best_fmt = fmt

    for m in re.finditer(r"\b(-?\d{1,3})\s*°\s*([cf])\b", tl):
        n, u = int(m.group(1)), m.group(2).upper()
        if _sanity_temp(n, u):
            consider(m.end(), f"{n}°{u}")

    for m in re.finditer(r"\b(-?\d{1,3})\s*degrees?\s*(celsius|fahrenheit)\b", tl):
        n = int(m.group(1))
        u = "C" if m.group(2).startswith("c") else "F"
        if _sanity_temp(n, u):
            consider(m.end(), f"{n}°{u}")

    for m in re.finditer(r"\b(?:high|highs?|low|lows?)\s+of\s+(-?\d{1,3})\b", tl):
        n = int(m.group(1))
        if _sanity_temp(n, hint):
            consider(m.end(), f"{n}°{hint}")

    for m in re.finditer(r"\b(?:minus|negative)\s+(\d{1,2})\s+degrees?\b", tl):
        n = -int(m.group(1))
        if _sanity_temp(n, hint):
            consider(m.end(), f"{n}°{hint}")

    for m in re.finditer(
        r"\b(?:it'?s|its|it is|currently|about|around|near|plus)\s+(\d{1,2})\s+degrees?\b", tl
    ):
        n = int(m.group(1))
        if _sanity_temp(n, hint):
            consider(m.end(), f"{n}°{hint}")

    for m in re.finditer(r"\bfeels\s+like\s+(-?\d{1,3})\s*°\s*([cf])\b", tl):
        n = int(m.group(1))
        u = m.group(2).upper()
        if _sanity_temp(n, u):
            consider(m.end(), f"{n}°{u} (feels like)")

    for m in re.finditer(r"\bfeels\s+like\s+(-?\d{1,3})\s+degrees?\s*(celsius|fahrenheit)?\b", tl):
        n = int(m.group(1))
        g2 = m.group(2)
        u = "C" if g2 and g2.startswith("c") else ("F" if g2 and g2.startswith("f") else hint)
        if _sanity_temp(n, u):
            consider(m.end(), f"{n}°{u} (feels like)")

    for m in re.finditer(r"\bfeels\s+like\s+(-?\d{1,3})\b", tl):
        n = int(m.group(1))
        if _sanity_temp(n, hint):
            consider(m.end(), f"{n}°{hint} (feels like)")

    return best_fmt


def extract_on_air_weather(text: str, tz_name: str) -> tuple[str | None, str | None]:
    """Parse temperature and conditions from a rolling transcript tail. Needs weather context."""
    if not text.strip():
        return None, None
    tl = text.lower()
    if not transcript_has_weather_context(tl):
        return None, None
    temp = _extract_weather_temperature_last(tl, tz_name)
    cond = _extract_weather_condition_last(tl)
    return temp, cond


def format_weather_line(rt: StationRuntimeState) -> str:
    if rt.weather_temp is None and rt.weather_condition is None:
        return "?"
    a = rt.weather_temp or "?"
    b = rt.weather_condition or "?"
    return f"{a}  ·  {b}"


def weather_symbol_for_runtime(rt: StationRuntimeState) -> str:
    """Single symbol for the weather card (tk-friendly Unicode)."""
    if rt.weather_temp is None and rt.weather_condition is None:
        return "?"
    c = (rt.weather_condition or "").lower()
    if "thunder" in c:
        return "⚡"
    if "snow" in c or "flurr" in c or "blizzard" in c or "ice pellet" in c:
        return "❄"
    if "rain" in c or "shower" in c or "drizzle" in c:
        return "☂"
    if "fog" in c:
        return "▒"
    if "sun" in c and "cloud" not in c:
        return "☀"
    if "clear" in c and "cloud" not in c:
        return "☀"
    if "cloud" in c or "overcast" in c:
        return "☁"
    if "wind" in c or "breeze" in c:
        return "〜"
    if rt.weather_temp:
        return "°"
    return "?"


_GAS_CONTEXT_PHRASES = (
    "gas price",
    "gas prices",
    "price of gas",
    "at the pump",
    "filling up",
    "per litre",
    "per liter",
    "per gallon",
    "a gallon",
    "average price",
    "self-serve",
    "gas is",
    "fuel is",
    "gasbuddy",
    "price of fuel",
    "diesel is",
    "regular is",
    "premium is",
)


def transcript_has_gas_context(tl: str) -> bool:
    return any(p in tl for p in _GAS_CONTEXT_PHRASES)


def _plausible_gas_dollar(s: str) -> bool:
    try:
        v = float(s)
    except ValueError:
        return False
    return 0.85 <= v <= 6.75


def _merge_gas_findings(found: list[tuple[str, str, int]]) -> str | None:
    by_label: dict[str, tuple[str, int]] = {}
    for lab, disp, end in found:
        prev = by_label.get(lab)
        if prev is None or end >= prev[1]:
            by_label[lab] = (disp, end)
    order = ("Reg", "Mid", "Prem", "Diesel", "Gas")
    labels = [k for k in order if k in by_label]
    if not labels:
        return None
    return "  ·  ".join(by_label[k][0] for k in labels)


def extract_on_air_gas_summary(text: str) -> str | None:
    """Parse gas/fuel prices from a rolling transcript tail (needs gas-related context).

    Display is intended for Canadian stations quoting CAD per litre (including ¢/L style).
    """
    if not text.strip():
        return None
    tl = text.lower()
    if not transcript_has_gas_context(tl):
        return None
    found: list[tuple[str, str, int]] = []

    for m in re.finditer(
        r"\bregular(?:\s+unleaded)?\s*(?:gas(?:oline)?)?\s*(?:is|at|,|'?s)?\s*\$?\s*(\d+\.\d{2})\b",
        tl,
    ):
        if _plausible_gas_dollar(m.group(1)):
            found.append(("Reg", f"Reg ${m.group(1)}", m.end()))

    for m in re.finditer(
        r"\bpremium\s*(?:gas(?:oline)?)?\s*(?:is|at|,|'?s)?\s*\$?\s*(\d+\.\d{2})\b",
        tl,
    ):
        if _plausible_gas_dollar(m.group(1)):
            found.append(("Prem", f"Prem ${m.group(1)}", m.end()))

    for m in re.finditer(
        r"\bmid(?:-|\s)?grade\s*(?:gas(?:oline)?)?\s*(?:is|at|,|'?s)?\s*\$?\s*(\d+\.\d{2})\b",
        tl,
    ):
        if _plausible_gas_dollar(m.group(1)):
            found.append(("Mid", f"Mid ${m.group(1)}", m.end()))

    for m in re.finditer(r"\bdiesel\s*(?:is|at|,|'?s)?\s*\$?\s*(\d+\.\d{2})\b", tl):
        if _plausible_gas_dollar(m.group(1)):
            found.append(("Diesel", f"Diesel ${m.group(1)}", m.end()))

    for m in re.finditer(
        r"\bgas\s*(?:is|at|'s)\s*\$?\s*(\d+\.\d{2})\b"
        r"|\bfuel\s*(?:is|at|'s)\s*\$?\s*(\d+\.\d{2})\b",
        tl,
    ):
        raw = next(g for g in m.groups() if g is not None)
        if _plausible_gas_dollar(raw):
            found.append(("Gas", f"${raw}", m.end()))

    for m in re.finditer(r"\$?\s*(\d+\.\d{2})\s*(?:per|a)\s*gallon\b", tl):
        if _plausible_gas_dollar(m.group(1)):
            found.append(("Gas", f"${m.group(1)}/gal", m.end()))

    for m in re.finditer(
        r"\b(regular|premium)\s+(?:unleaded\s+)?(?:is|at|,|'?s)?\s*(1\d{2}\.\d)\s*(?:¢|cents)?\s*(?:per|a|/)\s*(?:litre|liter)\b",
        tl,
    ):
        lab = "Reg" if m.group(1).startswith("reg") else "Prem"
        found.append((lab, f"{lab} {m.group(2)}¢/L", m.end()))

    for m in re.finditer(
        r"\bdiesel\s+(?:is|at|,|'?s)?\s*(1\d{2}\.\d)\s*(?:¢|cents)?\s*(?:per|a|/)\s*(?:litre|liter)\b",
        tl,
    ):
        found.append(("Diesel", f"Diesel {m.group(1)}¢/L", m.end()))

    for m in re.finditer(
        r"\bgas\s+(?:is|at|'s)\s*(1\d{2}\.\d)\s*(?:¢|cents)?\s*(?:per|a|/)\s*(?:litre|liter)\b",
        tl,
    ):
        found.append(("Gas", f"{m.group(1)}¢/L", m.end()))

    return _merge_gas_findings(found)


def format_gas_line(rt: StationRuntimeState) -> str:
    return "?" if not rt.gas_summary else rt.gas_summary


def gas_symbol_for_runtime(rt: StationRuntimeState) -> str:
    return "⛽" if rt.gas_summary else "?"


def exact_match_spans(text: str, term: str) -> list[tuple[int, int]]:
    """Case-insensitive exact matches only: single words use whole-word boundaries; phrases use exact substring."""
    if not term.strip() or not text:
        return []
    t = term.strip().lower()
    tl = text.lower()
    spans: list[tuple[int, int]] = []
    if " " in t:
        start = 0
        while True:
            idx = tl.find(t, start)
            if idx < 0:
                break
            spans.append((idx, idx + len(t)))
            start = idx + max(1, len(t))
    else:
        pattern = re.compile(r"\b" + re.escape(t) + r"\b", re.IGNORECASE)
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end()))
    return spans


def keyword_match_spans(text: str, term: str, fuzzy_max_distance: int) -> list[tuple[int, int]]:
    """Match spans in original text. fuzzy_max_distance 0 = exact only (no price/prize confusion)."""
    if fuzzy_max_distance == 0:
        return exact_match_spans(text, term)
    norm = text.lower()
    matches = find_near_matches(term, norm, max_l_dist=fuzzy_max_distance)
    if not matches:
        return []
    matches.sort(key=lambda m: m.start)
    out: list[tuple[int, int]] = []
    last_end = -1
    for m in matches:
        if m.start < last_end:
            continue
        out.append((m.start, m.end))
        last_end = m.end
    return out


def context_contains_blacklist(context: str, blacklist: list[str], fuzzy_max_distance: int) -> bool:
    if not blacklist or not context:
        return False
    for phrase in blacklist:
        if fuzzy_max_distance == 0:
            if exact_match_spans(context, phrase):
                return True
        elif find_near_matches(phrase, context.lower(), max_l_dist=fuzzy_max_distance):
            return True
    return False


def non_overlapping_spans(text: str, term: str, fuzzy_max_distance: int) -> list[tuple[int, int]]:
    """Character spans for highlighting / Discord formatting."""
    return keyword_match_spans(text, term, fuzzy_max_distance)


def discord_context_with_bold_keyword(context: str, term: str, fuzzy_max_distance: int) -> str:
    if not context:
        return ""
    spans = non_overlapping_spans(context, term, fuzzy_max_distance)
    if not spans:
        return context
    parts: list[str] = []
    pos = 0
    for start, end in spans:
        parts.append(context[pos:start])
        parts.append("**")
        parts.append(context[start:end])
        parts.append("**")
        pos = end
    parts.append(context[pos:])
    return "".join(parts)


class StationRecorder(threading.Thread):
    def __init__(
        self,
        station: StationConfig,
        stop_event: threading.Event,
        transcribe_queue: Queue,
        ui_queue: Queue,
        log: logging.Logger,
    ):
        super().__init__(daemon=True)
        self.station = station
        self.stop_event = stop_event
        self.transcribe_queue = transcribe_queue
        self.ui_queue = ui_queue
        self.log = log

    def run(self) -> None:
        self.log.info("Recorder started: %s", self.station.name)
        station_dir = config.DATA_DIR / "audio" / self.station.name
        station_dir.mkdir(parents=True, exist_ok=True)
        current_song_title = ""
        transcribe_seq = 0

        while not self.stop_event.is_set():
            day_dir = station_dir / datetime.utcnow().date().isoformat()
            day_dir.mkdir(parents=True, exist_ok=True)
            start = datetime.utcnow()
            out_file = day_dir / f"stream_{start.isoformat(timespec='seconds')}.mp3"

            try:
                headers = {"Icy-MetaData": "1", "User-Agent": "radio-monitor/1.0"}
                with requests.get(self.station.url, stream=True, timeout=30, headers=headers) as response:
                    response.raise_for_status()
                    metaint = int(response.headers.get("icy-metaint", "0") or 0)
                    with out_file.open("wb") as f:
                        if metaint > 0:
                            # Stream contains audio blocks + metadata intervals.
                            while not self.stop_event.is_set():
                                audio_block = response.raw.read(metaint)
                                if not audio_block:
                                    break
                                f.write(audio_block)

                                meta_len_raw = response.raw.read(1)
                                if not meta_len_raw:
                                    break
                                meta_len = meta_len_raw[0] * 16
                                if meta_len > 0:
                                    metadata = response.raw.read(meta_len)
                                    meta_text = metadata.decode("utf-8", errors="ignore").strip("\x00")
                                    stream_title = parse_icy_stream_title(meta_text)
                                    if stream_title and stream_title != current_song_title:
                                        current_song_title = stream_title
                                        self.ui_queue.put(("song", self.station.name, current_song_title))

                                if (datetime.utcnow() - start).total_seconds() >= self.station.chunk_time_seconds:
                                    break
                        else:
                            for block in response.iter_content(1024):
                                if self.stop_event.is_set():
                                    break
                                if block:
                                    f.write(block)
                                if (datetime.utcnow() - start).total_seconds() >= self.station.chunk_time_seconds:
                                    break
                self.transcribe_queue.put((str(out_file), self.station, current_song_title, transcribe_seq))
                transcribe_seq += 1
            except Exception as exc:
                self.log.error("Recorder error on %s: %s", self.station.name, exc)
                time.sleep(3)

        self.log.info("Recorder stopped: %s", self.station.name)


class TranscriptionWorker(threading.Thread):
    def __init__(
        self,
        worker_id: int,
        transcribe_queue: Queue,
        stop_event: threading.Event,
        ui_queue: Queue,
        log: logging.Logger,
        alert_cache: dict[tuple[str, str], float],
        alert_lock: threading.Lock,
        get_global_blacklist: Callable[[], list[str]],
        merge_state: TranscriptMergeState,
        get_contest_ai_enabled: Callable[[], bool],
        shared_transcriber: Transcriber | None = None,
    ):
        super().__init__(daemon=True)
        self.worker_id = worker_id
        self.transcribe_queue = transcribe_queue
        self.stop_event = stop_event
        self.ui_queue = ui_queue
        self.log = log
        self.alert_cache = alert_cache
        self.alert_lock = alert_lock
        self._get_global_blacklist = get_global_blacklist
        self.merge_state = merge_state
        self._get_contest_ai_enabled = get_contest_ai_enabled
        self._shared = shared_transcriber
        self._local: FasterWhisperTranscriber | None = None
        if self._shared is None:
            self._local = FasterWhisperTranscriber()

    def _send_discord_alert(self, event: MatchEvent, station: StationConfig) -> None:
        webhook = station.discord_webhook_url or (os.getenv("DISCORD_WEBHOOK_URL") or "").strip()
        if not webhook:
            return
        color = 0x2ECC71 if event.is_live else 0xF1C40F
        title = f"LIVE match: {event.term}" if event.is_live else f"TEST match: {event.term}"
        mention = station.discord_mention.strip()
        ctx_fmt = discord_context_with_bold_keyword(
            event.context_text, event.term, event.fuzzy_max_distance
        )
        description = (
            f"Station: **{event.station}**\n"
            f"Keyword: **🟢 {event.term}**\n"
            f"Context ({KEYWORD_CONTEXT_WORDS_EACH_SIDE} before / {KEYWORD_CONTEXT_WORDS_EACH_SIDE} after):\n{ctx_fmt}"
        )
        if mention:
            description = f"{mention}\n{description}"
        payload = {
            "username": station.discord_username or "Radio Keyword Bot",
            "embeds": [
                {
                    "title": title,
                    "description": description,
                    "color": color,
                    "timestamp": datetime.utcnow().isoformat(),
                }
            ],
        }
        try:
            r = requests.post(webhook, json=payload, timeout=6)
            if r.status_code not in (200, 204):
                self.log.warning("Discord webhook status=%s", r.status_code)
        except Exception as exc:
            self.log.warning("Discord alert failed: %s", exc)

    def _extract_context_window(self, text: str, start_idx: int, end_idx: int) -> tuple[str, str]:
        words_with_pos: list[tuple[str, int, int]] = []
        for m in re.finditer(r"\S+", text):
            words_with_pos.append((m.group(0), m.start(), m.end()))
        if not words_with_pos:
            return text.strip(), ""

        keyword_word_idx = None
        for i, (_, w_start, w_end) in enumerate(words_with_pos):
            if (w_start <= start_idx < w_end) or (w_start < end_idx <= w_end) or (start_idx <= w_start and end_idx >= w_end):
                keyword_word_idx = i
                break

        if keyword_word_idx is None:
            # Fallback: first chunk when the match cannot be aligned to a word (phrase span).
            n = KEYWORD_CONTEXT_WORDS_EACH_SIDE * 2 + 1
            context = " ".join(w for w, _, _ in words_with_pos[:n]).strip()
            return context, ""

        left = max(0, keyword_word_idx - KEYWORD_CONTEXT_WORDS_EACH_SIDE)
        right = min(len(words_with_pos), keyword_word_idx + KEYWORD_CONTEXT_WORDS_EACH_SIDE + 1)
        context_words = [w for w, _, _ in words_with_pos[left:right]]
        context = " ".join(context_words).strip()
        keyword_word = words_with_pos[keyword_word_idx][0]
        return context, keyword_word

    def _find_matches(
        self, station: StationConfig, text: str, global_blacklist: list[str]
    ) -> list[MatchEvent]:
        if not text.strip():
            return []
        events: list[MatchEvent] = []
        for term in station.live_terms:
            spans = keyword_match_spans(text, term, station.fuzzy_max_distance)
            if not spans:
                continue
            start_idx, end_idx = spans[0]
            context, _ = self._extract_context_window(text, start_idx, end_idx)
            if context_contains_blacklist(context, global_blacklist, station.fuzzy_max_distance):
                continue
            events.append(
                MatchEvent(
                    station.name,
                    term,
                    context,
                    True,
                    fuzzy_max_distance=station.fuzzy_max_distance,
                )
            )
        for term in station.dev_terms:
            spans = keyword_match_spans(text, term, station.fuzzy_max_distance)
            if not spans:
                continue
            start_idx, end_idx = spans[0]
            context, _ = self._extract_context_window(text, start_idx, end_idx)
            if context_contains_blacklist(context, global_blacklist, station.fuzzy_max_distance):
                continue
            events.append(
                MatchEvent(
                    station.name,
                    term,
                    context,
                    False,
                    fuzzy_max_distance=station.fuzzy_max_distance,
                )
            )
        return events

    def run(self) -> None:
        if self._shared is not None:
            self.log.info("Transcriber worker-%s started (shared API backend)", self.worker_id)
        else:
            self.log.info(
                "Transcriber worker-%s started model=%s device=%s compute_type=%s",
                self.worker_id,
                MODEL_SIZE,
                WHISPER_DEVICE,
                WHISPER_COMPUTE_TYPE,
            )
        while not self.stop_event.is_set():
            try:
                item = self.transcribe_queue.get(timeout=1)
            except Empty:
                continue

            try:
                if len(item) == 4:
                    file_path, station, current_song_title, chunk_seq = item
                else:
                    file_path, station, current_song_title = item
                    chunk_seq = 0
            except (TypeError, ValueError):
                self.transcribe_queue.task_done()
                continue

            try:
                station = fresh_station_config(station)
                blacklist = self._get_global_blacklist()
                impl = self._shared if self._shared is not None else self._local
                assert impl is not None
                whisper_text = impl.transcribe_path(str(file_path))
                icy = (current_song_title or "").strip()
                is_music_chunk = icy_and_transcript_suggest_music_lyrics(whisper_text, icy)
                commercial = detect_commercial_break(icy, whisper_text, station.name)
                self.ui_queue.put(("commercial", station.name, commercial))
                if is_music_chunk:
                    self.ui_queue.put(("now_playing", station.name, current_song_title.strip()))
                if whisper_text.strip():
                    append_transcript(
                        station.name,
                        whisper_text.strip(),
                        is_music=is_music_chunk,
                        icy_title=icy,
                    )
                    self.ui_queue.put(("speech", station.name, (whisper_text, is_music_chunk)))
                    delay_sample = best_delay_sample_from_transcript(whisper_text, station.station_timezone)
                    if delay_sample is not None:
                        self.ui_queue.put(("stream_delay", station.name, delay_sample))

                text_for_keywords = self.merge_state.feed(station.name, chunk_seq, whisper_text)
                if text_for_keywords is not None:
                    match_station = fresh_station_config(station)
                    for event in self._find_matches(match_station, text_for_keywords, blacklist):
                        key = (event.station, event.term)
                        now_ts = time.time()
                        send_now = False
                        with self.alert_lock:
                            prev = self.alert_cache.get(key, 0.0)
                            if now_ts - prev >= ALERT_DEDUP_SECONDS:
                                self.alert_cache[key] = now_ts
                                send_now = True
                        if not send_now:
                            continue
                        self.log.info(
                            "Keyword match → %s | term=%s | live=%s",
                            event.station,
                            event.term,
                            event.is_live,
                        )
                        self._send_discord_alert(event, match_station)
                        self.ui_queue.put(("alert", event.station, event))
                    if self._get_contest_ai_enabled() and maybe_groq_contest_scan(
                        station.name, text_for_keywords
                    ):
                        try:
                            record_contest_learn_positive(
                                station.name, text_for_keywords, "groq_yes"
                            )
                        except Exception:
                            pass
                        ctx = text_for_keywords.strip()
                        if len(ctx) > 600:
                            ctx = ctx[:597] + "..."
                        ai_event = MatchEvent(
                            station.name,
                            AI_CONTEST_ALERT_TERM,
                            ctx,
                            True,
                            fuzzy_max_distance=0,
                        )
                        key = (ai_event.station, ai_event.term)
                        now_ts = time.time()
                        send_ai = False
                        with self.alert_lock:
                            prev = self.alert_cache.get(key, 0.0)
                            if now_ts - prev >= ALERT_DEDUP_SECONDS:
                                self.alert_cache[key] = now_ts
                                send_ai = True
                        if send_ai:
                            self.log.info(
                                "Groq contest cue → %s (secondary to keyword list)",
                                station.name,
                            )
                            self._send_discord_alert(ai_event, match_station)
                            self.ui_queue.put(("alert", ai_event.station, ai_event))
            except Exception as exc:
                self.log.error("Transcribe error [%s] on %s: %s", station.name, file_path, exc)
                try:
                    self.merge_state.feed(station.name, chunk_seq, "")
                except Exception:
                    pass
            finally:
                self.transcribe_queue.task_done()
        self.log.info("Transcriber worker-%s stopped", self.worker_id)
