"""Process ui_queue events from MonitorController (shared desktop + mobile logic)."""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any

import song_play_history
from call_in_numbers import merge_call_in_strings
from call_in_scrape import scrape_station_call_ins
from call_in_verified_db import get_verified_pair
from call_in_verify import process_station_candidates
from config import StationConfig
from radio_engine import (
    AI_CONTEST_ALERT_TERM,
    MatchEvent,
    StationRuntimeIndex,
    format_gas_line,
    format_weather_line,
    icy_suggests_commercial,
    looks_like_song_title,
)


@dataclass
class UIEventResult:
    """Side effects for the UI layer after one queue message."""

    kind: str
    station_name: str
    speech_line: str | None = None
    speech_is_music: bool = False
    now_playing: str | None = None
    alert: MatchEvent | None = None
    commercial: bool | None = None
    commercial_was: bool | None = None
    stream_delay: tuple[float, str] | None = None
    call_in_changed: bool = False
    weather_changed: bool = False
    gas_changed: bool = False
    favorite_auto_switch_to: str | None = None
    commercial_auto_switch_to: str | None = None
    ad_feedback_snippet: str | None = None
    ai_feedback_context: str | None = None
    record_song_play: tuple[str, str] | None = None
    status_message: str | None = None


@dataclass
class MonitorUIContext:
    stations: list[StationConfig]
    station_runtime: StationRuntimeIndex
    monitor_prefs: dict[str, Any]
    selected_station: str
    last_speech_snippet_by_station: dict[str, str] = field(default_factory=dict)
    last_favorite_auto_switch_ts: float = 0.0
    last_ad_auto_switch_ts: float = 0.0
    listen_enabled: bool = True

    def station_tz(self, name: str) -> str:
        for s in self.stations:
            if s.name == name:
                return s.station_timezone or "America/Toronto"
        return "America/Toronto"

    def station_by_name(self, name: str) -> StationConfig | None:
        for s in self.stations:
            if s.name == name:
                return s
        return None

    def pick_fallback_station(self, exclude_name: str) -> StationConfig | None:
        exclude_name = exclude_name.strip()
        for s in self.stations:
            if s.name == exclude_name or not (s.url or "").strip():
                continue
            if not self.station_runtime.for_station(s.name).commercial_break:
                return s
        for s in self.stations:
            if s.name != exclude_name and (s.url or "").strip():
                return s
        return None

    def maybe_favorite_auto_switch(self, station_name: str, title: str) -> str | None:
        if not bool(self.monitor_prefs.get("favorite_auto_switch")):
            return None
        if not self.listen_enabled:
            return None
        if self.selected_station.strip() != station_name.strip():
            return None
        favs = self.monitor_prefs.get("favorite_songs_ordered") or []
        if not isinstance(favs, list) or not favs:
            return None
        key = song_play_history.normalize_song_key(title)
        if not key or key not in {song_play_history.normalize_song_key(f) for f in favs}:
            return None
        now = time.time()
        if now - self.last_favorite_auto_switch_ts < 20.0:
            return None
        for s in self.stations:
            if s.name == station_name:
                continue
            rt = self.station_runtime.for_station(s.name)
            icy = (rt.icy_title or "").strip()
            if not icy or not looks_like_song_title(icy):
                continue
            if song_play_history.normalize_song_key(icy) != key:
                continue
            if rt.commercial_break:
                continue
            self.last_favorite_auto_switch_ts = now
            return s.name
        return None

    def maybe_commercial_auto_switch(self, ad_station: str) -> str | None:
        if not bool(self.monitor_prefs.get("commercial_auto_switch")):
            return None
        if not self.listen_enabled:
            return None
        cur = self.selected_station.strip()
        if not cur or ad_station.strip() != cur:
            return None
        now = time.time()
        if now - self.last_ad_auto_switch_ts < 15.0:
            return None
        target = self.pick_fallback_station(cur)
        if target is None:
            return None
        self.last_ad_auto_switch_ts = now
        return target.name


def process_ui_queue_item(
    ctx: MonitorUIContext,
    kind: str,
    station_name: str,
    payload: Any,
) -> UIEventResult | None:
    sn = str(station_name)
    res = UIEventResult(kind=kind, station_name=sn)

    if kind == "speech":
        if isinstance(payload, tuple) and len(payload) == 2:
            line, is_music = str(payload[0]), bool(payload[1])
        else:
            line, is_music = str(payload), False
        t = line.strip()
        if t:
            ctx.last_speech_snippet_by_station[sn] = t[:1200]
        ctx.station_runtime.apply_speech_music_hint(sn, is_music)
        res.speech_line = line
        res.speech_is_music = is_music
        if ctx.station_runtime.feed_call_in_from_transcript(sn, line, is_music):
            res.call_in_changed = True
        tz = ctx.station_tz(sn)
        if ctx.station_runtime.feed_weather_from_transcript(sn, line, is_music, tz):
            res.weather_changed = True
        if ctx.station_runtime.feed_gas_from_transcript(sn, line, is_music):
            res.gas_changed = True
        return res

    if kind == "now_playing" or kind == "song":
        title = str(payload).strip()
        was = ctx.station_runtime.for_station(sn).commercial_break
        ctx.station_runtime.set_icy_title(sn, title)
        res.now_playing = title
        if ctx.station_runtime.feed_call_in_from_icy(sn, title):
            res.call_in_changed = True
        if kind == "song" and title and looks_like_song_title(title) and not icy_suggests_commercial(title):
            res.record_song_play = (sn, title)
            switch = ctx.maybe_favorite_auto_switch(sn, title)
            if switch:
                res.favorite_auto_switch_to = switch
        if ctx.station_runtime.for_station(sn).commercial_break and not was:
            ad_sw = ctx.maybe_commercial_auto_switch(sn)
            if ad_sw:
                res.commercial_auto_switch_to = ad_sw
        return res

    if kind == "alert" and isinstance(payload, MatchEvent):
        res.alert = payload
        if payload.term == AI_CONTEST_ALERT_TERM:
            res.ai_feedback_context = payload.context_text
        return res

    if kind == "commercial":
        was = ctx.station_runtime.for_station(sn).commercial_break
        ctx.station_runtime.set_commercial_break(sn, bool(payload))
        res.commercial = bool(payload)
        res.commercial_was = was
        if bool(payload) and not was:
            ad_sw = ctx.maybe_commercial_auto_switch(sn)
            if ad_sw:
                res.commercial_auto_switch_to = ad_sw
            snip = ctx.last_speech_snippet_by_station.get(sn, "").strip()
            if len(snip) >= 24:
                res.ad_feedback_snippet = snip
        return res

    if kind == "stream_delay" and isinstance(payload, tuple) and len(payload) == 2:
        try:
            res.stream_delay = (float(payload[0]), str(payload[1]))
        except (TypeError, ValueError):
            return None
        return res

    return None


def call_in_display_for_station(ctx: MonitorUIContext, station_name: str) -> str:
    st_cfg = ctx.station_by_name(station_name)
    rt = ctx.station_runtime.for_station(station_name)
    parts: list[str] = []
    if st_cfg and (st_cfg.call_in_number or "").strip():
        parts.append((st_cfg.call_in_number or "").strip())
    if (rt.call_in_from_air or "").strip():
        parts.append(rt.call_in_from_air.strip())
    if (rt.call_in_from_scrape or "").strip():
        parts.append(f"web: {rt.call_in_from_scrape.strip()}")
    studio_v, sms_v = get_verified_pair(station_name)
    if studio_v:
        parts.append(f"studio: {studio_v}")
    if sms_v:
        parts.append(f"sms: {sms_v}")
    merged = merge_call_in_strings(*parts)
    if merged:
        return merged
    if rt.call_in_scrape_err:
        return f"(scrape: {rt.call_in_scrape_err})"
    return "—"


def scrape_call_in_background(
    ctx: MonitorUIContext,
    station_name: str,
    cache_seconds: float,
) -> tuple[str, str, str]:
    """Returns (station_name, merged_scrape_result, error_message)."""
    st = ctx.station_by_name(station_name)
    if st is None:
        return station_name, "", "unknown station"
    page = (st.call_in_page_url or "").strip()
    if not page:
        return station_name, "", ""
    rt = ctx.station_runtime.for_station(station_name)
    now = time.time()
    if rt.call_in_scrape_ts and now - rt.call_in_scrape_ts < cache_seconds:
        return station_name, rt.call_in_from_scrape, ""
    try:
        found = scrape_station_call_ins(page)
        rt.call_in_from_scrape = found or ""
        rt.call_in_scrape_err = ""
        rt.call_in_scrape_ts = now
        return station_name, found or "", ""
    except Exception as exc:
        rt.call_in_scrape_err = str(exc)[:200]
        rt.call_in_scrape_ts = now
        return station_name, "", rt.call_in_scrape_err


def verify_call_in_background(
    ctx: MonitorUIContext,
    station_name: str,
) -> str:
    """Run web verify for heard + scraped call-in blobs."""
    st = ctx.station_by_name(station_name)
    rt = ctx.station_runtime.for_station(station_name)
    page = (st.call_in_page_url or "").strip() if st else ""
    try:
        process_station_candidates(
            station_name,
            rt.call_in_from_air or "",
            rt.call_in_from_scrape or "",
            page,
        )
    except Exception:
        pass
    return station_name


def runtime_status_line(ctx: MonitorUIContext, station_name: str) -> str:
    rt = ctx.station_runtime.for_station(station_name)
    lines = []
    icy = (rt.icy_title or "").strip()
    if icy:
        lines.append(f"♪ {icy}")
    w = format_weather_line(rt)
    if w:
        lines.append(w)
    g = format_gas_line(rt)
    if g:
        lines.append(g)
    ci = call_in_display_for_station(ctx, station_name)
    if ci and ci != "—":
        lines.append(f"Call: {ci}")
    if rt.delay_smoothed_sec is not None:
        lines.append(f"Delay ~{rt.delay_smoothed_sec:.0f}s ({rt.delay_last_heard})")
    if rt.commercial_break:
        lines.append("Commercial break")
    elif rt.dj_talk_overlay:
        lines.append("DJ talk (song on metadata)")
    return " | ".join(lines) if lines else "—"
