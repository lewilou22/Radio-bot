"""GUI-based multi-station radio monitor with fast transcription workers."""

from __future__ import annotations

from collections.abc import Callable

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from queue import Empty, Queue
import re
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from config import (
    CONTEST_AI_MIN_INTERVAL_SEC,
    DISCORD_WEBHOOK_URL,
    GROQ_API_KEY,
    GROQ_CONTEST_MODEL,
    OPENAI_API_KEY,
    StationConfig,
    TRANSCRIBE_WORKERS,
    load_stations,
    save_stations,
)
from fmstream_client import (
    FMSTREAM_SEARCH_PREFIX,
    FmStreamOption,
    fetch_fmstream_page,
    fmstream_search_url,
    parse_fmstream_page,
    stream_options_for_station,
)
import commercial_memory
import contest_memory
import song_play_history
from call_in_scrape import scrape_station_call_ins
from call_in_verified_db import get_verified_pair
from call_in_verify import process_station_candidates

from radio_engine import (
    AI_CONTEST_ALERT_TERM,
    MatchEvent,
    StationRecorder,
    StationRuntimeIndex,
    TranscriptMergeState,
    TranscriptionWorker,
    _load_monitor_prefs,
    _save_monitor_prefs,
    format_gas_line,
    format_weather_line,
    gas_symbol_for_runtime,
    icy_suggests_commercial,
    looks_like_song_title,
    non_overlapping_spans,
    setup_log,
    weather_symbol_for_runtime,
)
from transcribers import default_transcribe_backend, make_shared_transcriber_for_backend


# Station list row colours (tk)
STATION_LIST_MUSIC_FG = "#2563eb"
STATION_LIST_COMMERCIAL_FG = "#c0392b"


def _stream_player_command(url: str) -> list[str] | None:
    """Build argv to play a stream URL (mpv preferred, then ffplay, then VLC)."""
    if shutil.which("mpv"):
        return ["mpv", "--no-terminal", "--no-video", "--really-quiet", url]
    if shutil.which("ffplay"):
        return [
            "ffplay",
            "-nodisp",
            "-loglevel",
            "error",
            "-infbuf",
            "-i",
            url,
        ]
    if shutil.which("cvlc"):
        return ["cvlc", "--quiet", url]
    if shutil.which("vlc"):
        return ["vlc", "-Idummy", "--quiet", url]
    return None



def insert_text_with_keyword_highlights(
    widget: scrolledtext.ScrolledText,
    text: str,
    term: str,
    fuzzy_max_distance: int,
    tag: str,
) -> None:
    if not text:
        return
    spans = non_overlapping_spans(text, term, fuzzy_max_distance)
    if not spans:
        widget.insert(tk.END, text)
        return
    pos = 0
    for start, end in spans:
        widget.insert(tk.END, text[pos:start])
        widget.insert(tk.END, text[start:end], tag)
        pos = end
    widget.insert(tk.END, text[pos:])

class RadioMonitorApp:
    def __init__(self) -> None:
        self.log = setup_log()
        self.stations = load_stations()
        self.stop_event = threading.Event()
        self.ui_queue: Queue = Queue()
        self.transcribe_queue: Queue = Queue()
        self.recorders: list[StationRecorder] = []
        self.transcribers: list[TranscriptionWorker] = []
        self.alert_cache: dict[tuple[str, str], float] = {}
        self.alert_lock = threading.Lock()
        self.transcript_merge_state = TranscriptMergeState()
        self.running = False
        self._monitor_prefs: dict = _load_monitor_prefs()
        self._contest_ai_enabled_shared = [bool(self._monitor_prefs.get("contest_ai_groq", False))]
        self._monitor_prefs.setdefault("favorite_songs_ordered", [])
        self._monitor_prefs.setdefault("favorite_auto_switch", False)
        self._monitor_prefs.setdefault("commercial_auto_switch", False)
        if not isinstance(self._monitor_prefs.get("favorite_songs_ordered"), list):
            self._monitor_prefs["favorite_songs_ordered"] = []

        self.root = tk.Tk()
        self.root.title("Radio Contest Monitor")
        self.root.geometry("1040x780")
        self.station_runtime = StationRuntimeIndex()
        self._last_speech_snippet_by_station: dict[str, str] = {}
        self._last_favorite_auto_switch_ts = 0.0
        self._last_ad_auto_switch_ts = 0.0
        self._song_hist_win: tk.Toplevel | None = None
        self._ads_db_win: tk.Toplevel | None = None
        self._stream_player_proc: subprocess.Popen | None = None
        self._stream_player_url: str | None = None
        self._call_in_scrape_inflight: set[str] = set()
        self._call_in_scrape_cache_sec = float(os.getenv("CALL_IN_SCRAPE_CACHE_SECONDS", "3600"))
        self._call_in_verify_after_id: dict[str, int] = {}
        self._call_in_verify_inflight: set[str] = set()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.station_var = tk.StringVar(value=self.stations[0].name if self.stations else "")
        self.status_var = tk.StringVar(value="Idle")
        self.start_btn: ttk.Button | None = None
        self.stop_btn: ttk.Button | None = None

        self._build_ui()
        self._refresh_station_list()
        self._poll_ui_queue()

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")
        self.start_btn = ttk.Button(top, text="Start", command=self.start)
        self.stop_btn = ttk.Button(top, text="Stop", command=self.stop, state="disabled")
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn.pack(side="left", padx=4)
        ttk.Label(top, textvariable=self.status_var).pack(side="left", padx=10)

        main = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        main.pack(fill="both", expand=True, padx=8, pady=8)

        left = ttk.Frame(main)
        right = ttk.Frame(main)
        main.add(left, weight=1)
        main.add(right, weight=2)

        ai_frame = ttk.LabelFrame(
            left,
            text="Secondary AI (Groq — learns contest patterns over time)",
            padding=6,
        )
        ai_frame.pack(fill="x", pady=(0, 8))
        self._contest_ai_var = tk.BooleanVar(value=self._contest_ai_enabled_shared[0])
        ttk.Checkbutton(
            ai_frame,
            text="Contest / text-to-win / call-in scan (alongside keyword list)",
            variable=self._contest_ai_var,
            command=self._on_contest_ai_toggle,
        ).pack(anchor="w")
        _groq_hint = (
            "Requires GROQ_API_KEY in the environment. Throttled per station "
            f"({CONTEST_AI_MIN_INTERVAL_SEC:g}s). Model: {GROQ_CONTEST_MODEL}."
        )
        if not GROQ_API_KEY:
            _groq_hint += " Key not set — scanning will no-op until you export GROQ_API_KEY."
        ttk.Label(ai_frame, text=_groq_hint, foreground="#555", font=("TkDefaultFont", 8)).pack(anchor="w")

        ttk.Button(
            left,
            text="Song history & favorites…",
            command=self._open_song_history_window,
        ).pack(anchor="w", pady=(0, 6))

        ttk.Label(left, text="Stations").pack(anchor="w")
        ttk.Label(
            left,
            text="Each row: station name and stream “now playing” (ICY metadata). "
            "Colors: red = commercial, blue = music, black = talk/other. "
            "Commercial = ICY + speech heuristics + OpenAI or (if no OpenAI key) Groq; "
            "song metadata during ads no longer clears red by itself. "
            "Under keyword alerts you can confirm or reject commercial detections to teach the model.",
            foreground="#555",
            font=("TkDefaultFont", 8),
            wraplength=280,
        ).pack(anchor="w", pady=(0, 2))
        self.station_list = tk.Listbox(left, height=14, width=58, font=("TkDefaultFont", 9))
        self.station_list.pack(fill="x", pady=4)
        self.station_list.bind("<<ListboxSelect>>", self._on_station_selected)
        self._station_list_default_fg = self.station_list.cget("foreground") or ""
        if not self._station_list_default_fg.strip():
            self._station_list_default_fg = "SystemWindowText"

        play_frame = ttk.Frame(left)
        play_frame.pack(fill="x", pady=(0, 6))
        self._listen_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            play_frame,
            text="Play stream for selected station",
            variable=self._listen_var,
            command=self._on_listen_toggle,
        ).pack(anchor="w")
        self._ad_auto_switch_var = tk.BooleanVar(
            value=bool(self._monitor_prefs.get("commercial_auto_switch"))
        )
        ttk.Checkbutton(
            play_frame,
            text="Switch station when an ad is detected on the one you're listening to",
            variable=self._ad_auto_switch_var,
            command=self._on_commercial_auto_switch_toggle,
        ).pack(anchor="w", pady=(4, 0))
        ttk.Label(
            play_frame,
            text="Uses mpv, ffplay, or VLC if installed on your system.",
            foreground="#555",
            font=("TkDefaultFont", 8),
        ).pack(anchor="w")

        form = ttk.Frame(left)
        form.pack(fill="x", pady=4)
        ttk.Label(form, text="Name").grid(row=0, column=0, sticky="w")
        ttk.Label(form, text="URL").grid(row=1, column=0, sticky="w")
        ttk.Label(form, text="Live terms").grid(row=2, column=0, sticky="nw", padx=(0, 4), pady=(4, 0))
        ttk.Label(form, text="Dev terms").grid(row=3, column=0, sticky="nw", padx=(0, 4), pady=(4, 0))
        ttk.Label(form, text="Station webhook (optional)").grid(row=4, column=0, sticky="w")
        ttk.Label(form, text="Discord name").grid(row=5, column=0, sticky="w")
        ttk.Label(form, text="Mention (optional)").grid(row=6, column=0, sticky="w")
        ttk.Label(form, text="Timezone (IANA)").grid(row=7, column=0, sticky="w")
        ttk.Label(form, text="Call-in / text # (optional)").grid(row=8, column=0, sticky="w")
        ttk.Label(form, text="Website to scan (optional)").grid(row=9, column=0, sticky="w")
        self.name_entry = ttk.Entry(form)
        self.url_entry = ttk.Entry(form)
        term_opts = {"height": 5, "wrap": tk.WORD, "font": ("TkDefaultFont", 9), "padx": 4, "pady": 4}
        self.live_entry = scrolledtext.ScrolledText(form, **term_opts)
        self.dev_entry = scrolledtext.ScrolledText(form, **term_opts)
        self.webhook_entry = ttk.Entry(form)
        self.discord_name_entry = ttk.Entry(form)
        self.mention_entry = ttk.Entry(form)
        self.tz_entry = ttk.Entry(form)
        self.call_in_entry = ttk.Entry(form)
        self.call_in_page_entry = ttk.Entry(form)
        self.name_entry.grid(row=0, column=1, sticky="ew", padx=4, pady=2)
        self.url_entry.grid(row=1, column=1, sticky="ew", padx=4, pady=2)
        self.live_entry.grid(row=2, column=1, sticky="ew", padx=4, pady=2)
        self.dev_entry.grid(row=3, column=1, sticky="ew", padx=4, pady=2)
        self.webhook_entry.grid(row=4, column=1, sticky="ew", padx=4, pady=2)
        self.discord_name_entry.grid(row=5, column=1, sticky="ew", padx=4, pady=2)
        self.mention_entry.grid(row=6, column=1, sticky="ew", padx=4, pady=2)
        self.tz_entry.grid(row=7, column=1, sticky="ew", padx=4, pady=2)
        self.call_in_entry.grid(row=8, column=1, sticky="ew", padx=4, pady=2)
        self.call_in_page_entry.grid(row=9, column=1, sticky="ew", padx=4, pady=2)
        form.columnconfigure(1, weight=1)
        ttk.Label(
            form,
            text="One phrase per line (or comma-separated). Single words use whole-word match.",
            foreground="#555",
            font=("TkDefaultFont", 8),
        ).grid(row=10, column=1, sticky="w", padx=4, pady=(0, 4))

        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=4)
        ttk.Button(btns, text="Add Station", command=self.add_station).pack(side="left", padx=4)
        ttk.Button(btns, text="Update Station", command=self.update_station).pack(side="left", padx=4)
        ttk.Button(btns, text="New Station", command=self.new_station).pack(side="left", padx=4)
        ttk.Button(btns, text="Delete Station", command=self.delete_station).pack(side="left", padx=4)
        ttk.Button(btns, text="From fmstream…", command=self.browse_fmstream).pack(side="left", padx=4)

        out_pane = ttk.Panedwindow(right, orient=tk.VERTICAL)
        out_pane.pack(fill="both", expand=True)

        now_frame = ttk.LabelFrame(out_pane, text="Now playing (stream metadata)", padding=8)
        self.now_playing_var = tk.StringVar(value="—")
        ttk.Label(
            now_frame,
            textvariable=self.now_playing_var,
            wraplength=520,
            justify="left",
            font=("TkDefaultFont", 10, "bold"),
        ).pack(anchor="w", fill="x")
        ttk.Label(
            now_frame,
            text="Artist and title from the station when the stream sends ICY data (not from speech recognition).",
            foreground="#555",
            wraplength=520,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        call_inner = ttk.Frame(now_frame)
        call_inner.pack(fill="x", pady=(12, 0), anchor="w")
        ttk.Label(
            call_inner,
            text="Call-in / text line",
            font=("TkDefaultFont", 9, "bold"),
        ).pack(anchor="w")
        self.call_in_var = tk.StringVar(
            value="— (web scan, then on-air detection, optional pins in the station form)"
        )
        ttk.Label(
            call_inner,
            textvariable=self.call_in_var,
            wraplength=520,
            justify="left",
            font=("TkDefaultFont", 12, "bold"),
        ).pack(anchor="w", pady=(4, 0))
        call_btn_row = ttk.Frame(call_inner)
        call_btn_row.pack(anchor="w", pady=(2, 0))
        ttk.Button(
            call_btn_row,
            text="Refresh call-in from web now",
            command=self._refresh_call_in_from_web_clicked,
        ).pack(side=tk.LEFT)
        ttk.Label(
            call_inner,
            text="Uses “Website to scan” in the station form: fetches the page, reads tel:/sms: links and visible text, "
            "then falls back to live transcription + stream title if nothing useful is found online. "
            "Re-fetch is cached (~1 h) unless you use the button.",
            foreground="#555",
            wraplength=520,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        self.delay_var = tk.StringVar(value="?")
        delay_inner = ttk.Frame(now_frame)
        delay_inner.pack(fill="x", pady=(10, 0), anchor="w")
        ttk.Label(delay_inner, text="Stream delay (spoken time vs your clock)", font=("TkDefaultFont", 9, "bold")).pack(
            anchor="w"
        )
        ttk.Label(
            delay_inner,
            textvariable=self.delay_var,
            wraplength=520,
            justify="left",
            font=("TkDefaultFont", 11),
        ).pack(anchor="w", pady=(2, 0))
        ttk.Label(
            delay_inner,
            text="Updates when an announcer says the time (e.g. “8:45”). Uses each station’s timezone. Shows ? until a time is heard.",
            foreground="#555",
            wraplength=520,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        weather_inner = ttk.Frame(now_frame)
        weather_inner.pack(fill="x", pady=(14, 0), anchor="w")
        ttk.Label(
            weather_inner,
            text="On-air weather (from speech)",
            font=("TkDefaultFont", 9, "bold"),
        ).pack(anchor="w")
        weather_row = ttk.Frame(weather_inner)
        weather_row.pack(fill="x", anchor="w", pady=(6, 0))
        self._weather_symbol_lbl = ttk.Label(weather_row, text="?", font=("TkDefaultFont", 20))
        self._weather_symbol_lbl.pack(side="left", padx=(0, 12))
        self.weather_var = tk.StringVar(value="?")
        ttk.Label(
            weather_row,
            textvariable=self.weather_var,
            wraplength=460,
            justify="left",
            font=("TkDefaultFont", 12, "bold"),
        ).pack(side="left", fill="x", expand=True)
        ttk.Label(
            weather_inner,
            text="Per station: temperature and conditions when the forecast is heard in transcription. "
            "? if unknown. Station timezone picks °C vs °F when the announcer omits the unit.",
            foreground="#555",
            wraplength=520,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        gas_inner = ttk.Frame(now_frame)
        gas_inner.pack(fill="x", pady=(14, 0), anchor="w")
        ttk.Label(
            gas_inner,
            text="Gas prices (from speech, CAD/L)",
            font=("TkDefaultFont", 9, "bold"),
        ).pack(anchor="w")
        gas_row = ttk.Frame(gas_inner)
        gas_row.pack(fill="x", anchor="w", pady=(6, 0))
        self._gas_symbol_lbl = ttk.Label(gas_row, text="?", font=("TkDefaultFont", 20))
        self._gas_symbol_lbl.pack(side="left", padx=(0, 12))
        self.gas_var = tk.StringVar(value="?")
        ttk.Label(
            gas_row,
            textvariable=self.gas_var,
            wraplength=460,
            justify="left",
            font=("TkDefaultFont", 12, "bold"),
        ).pack(side="left", fill="x", expand=True)
        ttk.Label(
            gas_inner,
            text="Canadian litres: on-air amounts are parsed as CAD per litre (e.g. regular $1.59, "
            "or 159.9¢/L). ? until heard on this station.",
            foreground="#555",
            wraplength=520,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        speech_frame = ttk.LabelFrame(out_pane, text="Live speech (Whisper transcription)", padding=8)
        ttk.Label(
            speech_frame,
            text=(
                "Each chunk: blue ♪ when the on-air title looks like a track *and* the transcript "
                "matches that title (stations often leave the old song in metadata during DJ talk)."
            ),
            foreground="#555",
            wraplength=520,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        self.transcript = scrolledtext.ScrolledText(speech_frame, wrap=tk.WORD, height=14)
        self.transcript.pack(fill="both", expand=True)
        self.transcript.configure(state="disabled")
        self.transcript.tag_configure("ts", foreground="#666")
        self.transcript.tag_configure("music", foreground="#2563eb")
        self.transcript.bind("<Key>", lambda _e: "break")
        self.transcript.bind("<Button-2>", lambda _e: "break")
        self.transcript.bind("<Button-3>", lambda _e: "break")

        speech_learn = ttk.Frame(speech_frame)
        speech_learn.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(
            speech_learn,
            text="This speech is an ad — teach AI",
            command=self._teach_current_speech_as_ad,
        ).pack(side=tk.LEFT)
        ttk.Label(
            speech_learn,
            text="Uses the latest transcription chunk for the station selected in the list (same data as commercial learning).",
            foreground="#555",
            wraplength=400,
            justify="left",
        ).pack(side=tk.LEFT, padx=(10, 0))

        alerts_frame = ttk.LabelFrame(out_pane, text="Keyword alerts (all stations)", padding=8)
        ttk.Label(
            alerts_frame,
            text="Matches from every station appear here. The speech log above is only for the station you select in the list.",
            foreground="#555",
            wraplength=520,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        self.alerts = scrolledtext.ScrolledText(alerts_frame, wrap=tk.WORD, height=9)
        self.alerts.pack(fill="both", expand=True)
        self.alerts.tag_configure("keyword_green", foreground="#1db954")
        self.alerts.configure(state="disabled")
        self.alerts.bind("<Key>", lambda _e: "break")
        self.alerts.bind("<Button-2>", lambda _e: "break")
        self.alerts.bind("<Button-3>", lambda _e: "break")

        self._ai_feedback_pending: tuple[str, str] | None = None
        self._ad_feedback_pending: tuple[str, str] | None = None
        ai_fb = ttk.Frame(alerts_frame)
        ai_fb.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(
            ai_fb,
            text="Last Groq contest alert — teach the model:",
            foreground="#555",
        ).pack(side=tk.LEFT)
        self._btn_ai_good = ttk.Button(
            ai_fb, text="✓ Really a contest", command=self._ai_learn_confirm, state=tk.DISABLED
        )
        self._btn_ai_good.pack(side=tk.LEFT, padx=(8, 4))
        self._btn_ai_bad = ttk.Button(
            ai_fb, text="✗ False alarm", command=self._ai_learn_reject, state=tk.DISABLED
        )
        self._btn_ai_bad.pack(side=tk.LEFT, padx=4)
        p0, n0 = contest_memory.learned_counts()
        self._ai_learn_stats = tk.StringVar(value=f"Learned: {p0}↑ {n0}↓")
        ttk.Label(ai_fb, textvariable=self._ai_learn_stats, foreground="#666").pack(
            side=tk.LEFT, padx=(12, 0)
        )

        ad_fb = ttk.Frame(alerts_frame)
        ad_fb.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(
            ad_fb,
            text="Commercial break detected — correct the ad detector:",
            foreground="#555",
        ).pack(side=tk.LEFT)
        self._btn_ad_good = ttk.Button(
            ad_fb, text="✓ Confirm ad", command=self._ad_learn_confirm, state=tk.DISABLED
        )
        self._btn_ad_good.pack(side=tk.LEFT, padx=(8, 4))
        self._btn_ad_bad = ttk.Button(
            ad_fb, text="✗ Not an ad", command=self._ad_learn_reject, state=tk.DISABLED
        )
        self._btn_ad_bad.pack(side=tk.LEFT, padx=4)
        ap0, an0 = commercial_memory.learned_counts()
        self._ad_learn_stats = tk.StringVar(value=f"Ads learned: {ap0}↑ {an0}↓")
        ttk.Label(ad_fb, textvariable=self._ad_learn_stats, foreground="#666").pack(
            side=tk.LEFT, padx=(12, 0)
        )
        ttk.Button(ad_fb, text="Browse ads in DB…", command=self._open_ads_database_window).pack(
            side=tk.LEFT, padx=(10, 0)
        )

        out_pane.add(now_frame, weight=0)
        out_pane.add(speech_frame, weight=3)
        out_pane.add(alerts_frame, weight=2)

    def _refresh_station_list_row_styles(self) -> None:
        """Red = commercial, blue = on-air music, default = talk / unknown."""
        for i, st in enumerate(self.stations):
            if i >= self.station_list.size():
                break
            rt = self.station_runtime.for_station(st.name)
            title = (rt.icy_title or "").strip()
            is_music = bool(title and looks_like_song_title(title) and not rt.dj_talk_overlay)
            if rt.commercial_break:
                fg = STATION_LIST_COMMERCIAL_FG
            elif is_music:
                fg = STATION_LIST_MUSIC_FG
            else:
                fg = self._station_list_default_fg
            try:
                self.station_list.itemconfig(i, foreground=fg)
            except tk.TclError:
                pass

    def _refresh_station_list(self, select_name: str | None = None) -> None:
        self.station_runtime.prune({s.name for s in self.stations})
        self.station_list.delete(0, tk.END)
        for station in self.stations:
            rt = self.station_runtime.for_station(station.name)
            self.station_list.insert(tk.END, self._station_row_text(station.name, rt))
        self._refresh_station_list_row_styles()
        if not self.stations:
            self._clear_transcript_view()
            self.now_playing_var.set("—")
            self.delay_var.set("?")
            self.call_in_var.set("—")
            self.weather_var.set("?")
            self._weather_symbol_lbl.configure(text="?")
            self.gas_var.set("?")
            self._gas_symbol_lbl.configure(text="?")
            self._sync_stream_player()
            return
        if select_name:
            for i, s in enumerate(self.stations):
                if s.name == select_name:
                    self.station_list.selection_clear(0, tk.END)
                    self.station_list.selection_set(i)
                    self.station_list.see(i)
                    self._show_station(s)
                    self._clear_transcript_view()
                    self._sync_stream_player()
                    return
        self.station_list.selection_clear(0, tk.END)
        self.station_list.selection_set(0)
        self._show_station(self.stations[0])
        self._clear_transcript_view()
        self._sync_stream_player()

    def _stop_stream_player(self) -> None:
        if self._stream_player_proc is None:
            self._stream_player_url = None
            return
        proc = self._stream_player_proc
        self._stream_player_proc = None
        self._stream_player_url = None
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _start_stream_player(self, url: str) -> None:
        url = url.strip()
        if not url:
            self._stop_stream_player()
            return
        if self._stream_player_proc is not None and self._stream_player_url == url:
            try:
                if self._stream_player_proc.poll() is None:
                    return
            except Exception:
                pass
        self._stop_stream_player()
        cmd = _stream_player_command(url)
        if not cmd:
            self.status_var.set("No mpv, ffplay, or VLC found — install one to play streams.")
            return
        popen_kw: dict = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            cf = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if cf:
                popen_kw["creationflags"] = cf
        try:
            self._stream_player_proc = subprocess.Popen(cmd, **popen_kw)
            self._stream_player_url = url
        except OSError as exc:
            self._stream_player_proc = None
            self._stream_player_url = None
            self.status_var.set(f"Could not start stream player: {exc}")

    def _sync_stream_player(self) -> None:
        if not self._listen_var.get():
            self._stop_stream_player()
            return
        idx = self.station_list.curselection()
        if not idx or not self.stations:
            self._stop_stream_player()
            return
        st = self.stations[idx[0]]
        url = (st.url or "").strip()
        if not url:
            self._stop_stream_player()
            return
        self._start_stream_player(url)

    def _on_listen_toggle(self) -> None:
        self._sync_stream_player()

    def _on_commercial_auto_switch_toggle(self) -> None:
        self._monitor_prefs["commercial_auto_switch"] = bool(self._ad_auto_switch_var.get())
        _save_monitor_prefs(self._monitor_prefs)
        self.status_var.set(
            "Ad auto-switch " + ("on" if self._ad_auto_switch_var.get() else "off") + " (saved)."
        )

    def _pick_fallback_station(self, exclude_name: str) -> StationConfig | None:
        """Prefer a station with a URL that is not in commercial break; else any other with URL."""
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

    def _maybe_commercial_auto_switch(self, ad_station: str) -> None:
        if not bool(self._monitor_prefs.get("commercial_auto_switch")):
            return
        if not self._listen_var.get():
            return
        cur = self.station_var.get().strip()
        if not cur or ad_station.strip() != cur:
            return
        now = time.time()
        if now - self._last_ad_auto_switch_ts < 15.0:
            return
        target = self._pick_fallback_station(cur)
        if target is None:
            self.status_var.set("Ad detected — no other station with a stream URL to switch to.")
            return
        self._last_ad_auto_switch_ts = now
        self._select_station_by_name(target.name)
        self.status_var.set(f"Switched to {target.name} — ad detected on {cur}.")

    def _show_station(self, station: StationConfig) -> None:
        self.name_entry.delete(0, tk.END)
        self.name_entry.insert(0, station.name)
        self.url_entry.delete(0, tk.END)
        self.url_entry.insert(0, station.url)
        self._fill_terms_text_widget(self.live_entry, station.live_terms)
        self._fill_terms_text_widget(self.dev_entry, station.dev_terms)
        self.webhook_entry.delete(0, tk.END)
        self.webhook_entry.insert(0, station.discord_webhook_url)
        self.discord_name_entry.delete(0, tk.END)
        self.discord_name_entry.insert(0, station.discord_username)
        self.mention_entry.delete(0, tk.END)
        self.mention_entry.insert(0, station.discord_mention)
        self.tz_entry.delete(0, tk.END)
        self.tz_entry.insert(0, station.station_timezone)
        self.call_in_entry.delete(0, tk.END)
        self.call_in_entry.insert(0, station.call_in_number)
        self.call_in_page_entry.delete(0, tk.END)
        self.call_in_page_entry.insert(0, station.call_in_page_url)
        self.station_var.set(station.name)
        self._sync_now_playing_label()
        self._refresh_delay_label()
        self._refresh_call_in_label()
        self.root.after(100, lambda n=station.name: self._schedule_call_in_scrape_if_configured(n))
        self.root.after(200, lambda n=station.name: self._schedule_call_in_verify(n))
        self._refresh_weather_label()
        self._refresh_gas_label()

    def _sync_now_playing_label(self) -> None:
        name = self.station_var.get().strip()
        if not name:
            self.now_playing_var.set("—")
            return
        title = self.station_runtime.for_station(name).icy_title
        self.now_playing_var.set(f"♪ {title}" if title else "— (no track metadata yet)")

    def _refresh_delay_label(self) -> None:
        name = self.station_var.get().strip()
        if not name:
            self.delay_var.set("?")
            return
        rt = self.station_runtime.for_station(name)
        if rt.delay_smoothed_sec is None:
            self.delay_var.set("?")
            return
        sec = rt.delay_smoothed_sec
        heard_bit = f' (on-air: “{rt.delay_last_heard}”)' if rt.delay_last_heard else ""
        self.delay_var.set(f"~{sec:.0f} s behind live{heard_bit}")

    def _schedule_call_in_scrape_if_configured(self, station_name: str) -> None:
        u = ""
        for s in self.stations:
            if s.name == station_name:
                u = (s.call_in_page_url or "").strip()
                break
        if u:
            self._request_call_in_scrape(station_name, u, force=False)

    def _request_call_in_scrape(self, station_name: str, page_url: str, force: bool) -> None:
        key = station_name.strip()
        u = (page_url or "").strip()
        if not key or not u:
            return
        rt = self.station_runtime.for_station(key)
        now = time.time()
        if (
            not force
            and rt.call_in_from_scrape
            and (now - rt.call_in_scrape_ts) < self._call_in_scrape_cache_sec
        ):
            return
        if key in self._call_in_scrape_inflight:
            return
        self._call_in_scrape_inflight.add(key)

        def work() -> None:
            disp, err = "", ""
            try:
                disp, err = scrape_station_call_ins(u)
            except Exception as exc:
                err = str(exc)[:140]
            disp = (disp or "").strip()

            def apply() -> None:
                self._call_in_scrape_inflight.discard(key)
                st = self.station_runtime.for_station(key)
                st.call_in_from_scrape = disp
                st.call_in_scrape_err = err
                st.call_in_scrape_ts = time.time()
                if self.station_var.get().strip() == key:
                    self._refresh_call_in_label()
                if err and not disp:
                    self.status_var.set(f"Call-in web scan: {err}")
                elif disp:
                    self.status_var.set("Call-in numbers found on website (see above).")
                    self._schedule_call_in_verify(key)

            self.root.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _schedule_call_in_verify(self, station_name: str) -> None:
        key = (station_name or "").strip()
        if not key:
            return
        old = self._call_in_verify_after_id.pop(key, None)
        if old is not None:
            try:
                self.root.after_cancel(old)
            except tk.TclError:
                pass
        aid = self.root.after(3500, lambda k=key: self._kick_call_in_verify(k))
        self._call_in_verify_after_id[key] = aid

    def _kick_call_in_verify(self, station_name: str) -> None:
        self._call_in_verify_after_id.pop(station_name, None)
        if station_name in self._call_in_verify_inflight:
            return
        self._call_in_verify_inflight.add(station_name)
        page_url = ""
        for s in self.stations:
            if s.name == station_name:
                page_url = (s.call_in_page_url or "").strip()
                break
        rt = self.station_runtime.for_station(station_name)
        heard = rt.call_in_from_air or ""
        scrape = rt.call_in_from_scrape or ""

        def work() -> None:
            try:
                process_station_candidates(
                    station_name,
                    heard,
                    scrape,
                    page_url,
                    log=lambda m: self.log.info("%s", m),
                )
            finally:

                def done() -> None:
                    self._call_in_verify_inflight.discard(station_name)
                    if self.station_var.get().strip() == station_name:
                        self._refresh_call_in_label()

                self.root.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def _refresh_call_in_from_web_clicked(self) -> None:
        name = self.station_var.get().strip()
        if not name:
            return
        u = self.call_in_page_entry.get().strip()
        if not u:
            self.status_var.set(
                'Enter a full https:// URL in “Website to scan”, then click again (Update Station to keep it).'
            )
            return
        self._request_call_in_scrape(name, u, force=True)

    def _refresh_call_in_label(self) -> None:
        name = self.station_var.get().strip()
        if not name:
            self.call_in_var.set("—")
            return
        manual = ""
        page_url = ""
        for s in self.stations:
            if s.name == name:
                manual = (s.call_in_number or "").strip()
                page_url = (s.call_in_page_url or "").strip()
                break
        rt = self.station_runtime.for_station(name)
        scrape = (rt.call_in_from_scrape or "").strip()
        heard = (rt.call_in_from_air or "").strip()
        v_call, v_sms = get_verified_pair(name)

        if v_call or v_sms:
            parts: list[str] = []
            if manual:
                parts.append(f"Pinned: {manual}")
            if v_call:
                parts.append(f"Call: {v_call}")
            if v_sms:
                parts.append(f"Text: {v_sms}")
            sources: list[str] = []
            if scrape:
                sources.append("website")
            if heard:
                sources.append("on-air")
            if sources:
                parts.append(f"(verified via web search · {' + '.join(sources)})")
            self.call_in_var.set("   ".join(parts))
            return

        body: list[str] = []
        if scrape:
            body.append(f"Web: {scrape}")
        elif heard:
            body.append(f"Heard / metadata: {heard}")
        elif page_url and (rt.call_in_scrape_err or "").strip():
            body.append(f"Web: ({(rt.call_in_scrape_err or '')[:100]})")
        if manual:
            body.insert(0, f"Pinned: {manual}")
        if not body:
            self.call_in_var.set(
                "— (add “Website to scan”, use Refresh from web, and/or wait for on-air mentions)"
            )
        else:
            self.call_in_var.set("   ".join(body))

    def _station_tz(self, station_name: str) -> str:
        for s in self.stations:
            if s.name == station_name:
                return s.station_timezone
        return "America/Toronto"

    @staticmethod
    def _station_row_text(name: str, rt: StationRuntimeState) -> str:
        raw = (rt.icy_title or "").strip()
        if not raw:
            playing = "—"
        else:
            playing = raw.replace("\n", " ").replace("\t", " ")
            max_p = 54
            if len(playing) > max_p:
                playing = playing[: max_p - 1] + "…"
        max_name = 20
        short = name if len(name) <= max_name else name[: max_name - 1] + "…"
        return f"{short}   ♪   {playing}"

    def _refresh_weather_label(self) -> None:
        name = self.station_var.get().strip()
        if not name:
            self.weather_var.set("?")
            self._weather_symbol_lbl.configure(text="?")
            return
        rt = self.station_runtime.for_station(name)
        self.weather_var.set(format_weather_line(rt))
        self._weather_symbol_lbl.configure(text=weather_symbol_for_runtime(rt))

    def _refresh_gas_label(self) -> None:
        name = self.station_var.get().strip()
        if not name:
            self.gas_var.set("?")
            self._gas_symbol_lbl.configure(text="?")
            return
        rt = self.station_runtime.for_station(name)
        self.gas_var.set(format_gas_line(rt))
        self._gas_symbol_lbl.configure(text=gas_symbol_for_runtime(rt))

    def _rebuild_station_list_entries(self) -> None:
        sel = self.station_list.curselection()
        idx = sel[0] if sel else None
        self.station_list.delete(0, tk.END)
        for station in self.stations:
            rt = self.station_runtime.for_station(station.name)
            self.station_list.insert(tk.END, self._station_row_text(station.name, rt))
        self._refresh_station_list_row_styles()
        if idx is not None and 0 <= idx < len(self.stations):
            self.station_list.selection_set(idx)
            self.station_list.see(idx)

    def _apply_stream_delay(self, station_name: str, raw_seconds: float, heard: str) -> None:
        self.station_runtime.apply_stream_delay(station_name, raw_seconds, heard)
        if station_name == self.station_var.get():
            self._refresh_delay_label()

    def _clear_transcript_view(self) -> None:
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", tk.END)
        self.transcript.configure(state="disabled")

    def _on_station_selected(self, _event=None) -> None:
        idx = self.station_list.curselection()
        if not idx:
            return
        self._show_station(self.stations[idx[0]])
        self._clear_transcript_view()
        self._refresh_delay_label()
        self._sync_stream_player()

    def _selected_index(self) -> int | None:
        idx = self.station_list.curselection()
        if not idx:
            return None
        return idx[0]

    def new_station(self) -> None:
        self.station_list.selection_clear(0, tk.END)
        self.name_entry.delete(0, tk.END)
        self.url_entry.delete(0, tk.END)
        self._fill_terms_text_widget(self.live_entry, [])
        self._fill_terms_text_widget(self.dev_entry, [])
        self.webhook_entry.delete(0, tk.END)
        self.discord_name_entry.delete(0, tk.END)
        self.discord_name_entry.insert(0, "Radio Keyword Bot")
        self.mention_entry.delete(0, tk.END)
        self.tz_entry.delete(0, tk.END)
        self.tz_entry.insert(0, "America/Toronto")
        self.call_in_entry.delete(0, tk.END)
        self.call_in_page_entry.delete(0, tk.END)
        self.station_var.set("")
        self._sync_now_playing_label()
        self._refresh_delay_label()
        self._refresh_call_in_label()
        self._refresh_weather_label()
        self._refresh_gas_label()
        self._sync_stream_player()
        self.status_var.set("New station — fill the form, then Add Station")

    def _on_contest_ai_toggle(self) -> None:
        on = bool(self._contest_ai_var.get())
        self._contest_ai_enabled_shared[0] = on
        self._monitor_prefs["contest_ai_groq"] = on
        _save_monitor_prefs(self._monitor_prefs)
        self.status_var.set("Groq contest scan " + ("on" if on else "off") + " (saved).")

    @staticmethod
    def _terms_from_text_widget(w: scrolledtext.ScrolledText) -> list[str]:
        raw = w.get("1.0", "end-1c")
        seen: set[str] = set()
        out: list[str] = []
        for line in raw.splitlines():
            for part in line.split(","):
                t = part.strip().lower()
                if t and t not in seen:
                    seen.add(t)
                    out.append(t)
        return out

    @staticmethod
    def _fill_terms_text_widget(w: scrolledtext.ScrolledText, terms: list[str]) -> None:
        w.configure(state="normal")
        w.delete("1.0", tk.END)
        if terms:
            w.insert("1.0", "\n".join(terms))
        w.see("1.0")

    def _read_form_station(self, preserve: StationConfig | None) -> StationConfig | None:
        name = self.name_entry.get().strip()
        url = self.url_entry.get().strip()
        live = self._terms_from_text_widget(self.live_entry)
        dev = self._terms_from_text_widget(self.dev_entry)
        webhook = self.webhook_entry.get().strip()
        discord_name = self.discord_name_entry.get().strip() or "Radio Keyword Bot"
        mention = self.mention_entry.get().strip()
        station_tz = self.tz_entry.get().strip() or "America/Toronto"
        call_in = self.call_in_entry.get().strip()
        call_in_page = self.call_in_page_entry.get().strip()
        if not name or not url:
            messagebox.showerror("Invalid station", "Name and URL are required.")
            return None
        if preserve:
            return StationConfig(
                name=name,
                url=url,
                live_terms=live,
                dev_terms=dev,
                chunk_time_seconds=preserve.chunk_time_seconds,
                fuzzy_max_distance=preserve.fuzzy_max_distance,
                enabled=preserve.enabled,
                discord_webhook_url=webhook,
                discord_username=discord_name,
                discord_mention=mention,
                station_timezone=station_tz,
                call_in_number=call_in,
                call_in_page_url=call_in_page,
            )
        return StationConfig(
            name=name,
            url=url,
            live_terms=live,
            dev_terms=dev,
            discord_webhook_url=webhook,
            discord_username=discord_name,
            discord_mention=mention,
            station_timezone=station_tz,
            call_in_number=call_in,
            call_in_page_url=call_in_page,
        )

    def _station_name_exists(self, name: str, exclude_index: int | None) -> bool:
        name_lower = name.strip().lower()
        for i, s in enumerate(self.stations):
            if exclude_index is not None and i == exclude_index:
                continue
            if s.name.strip().lower() == name_lower:
                return True
        return False

    def browse_fmstream(self) -> None:
        """Search fmstream.org and copy a chosen stream into the station form."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Add station from fmstream.org")
        dlg.minsize(720, 420)
        dlg.transient(self.root)

        top = ttk.Frame(dlg, padding=8)
        top.pack(fill="x")
        ttk.Label(
            top,
            text=(
                "Search the directory at fmstream.org (same as the site’s search). "
                "Results include embedded stream URLs — pick a station, then a stream."
            ),
            wraplength=680,
        ).pack(anchor="w")
        url_row = ttk.Frame(top)
        url_row.pack(fill="x", pady=(8, 4))
        ttk.Label(url_row, text=FMSTREAM_SEARCH_PREFIX, font=("TkFixedFont", 9)).pack(side="left")
        search_var = tk.StringVar()
        search_entry = ttk.Entry(url_row, textvariable=search_var)
        search_entry.pack(side="left", fill="x", expand=True, padx=4)
        opts_row = ttk.Frame(top)
        opts_row.pack(fill="x", pady=(0, 4))
        mp3_only_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts_row, text="MP3 only (recommended)", variable=mp3_only_var).pack(side="left")

        mid = ttk.Frame(dlg, padding=(8, 0))
        mid.pack(fill="both", expand=True)
        pan = ttk.Panedwindow(mid, orient=tk.HORIZONTAL)
        pan.pack(fill="both", expand=True)

        left_f = ttk.LabelFrame(pan, text="Stations", padding=4)
        right_f = ttk.LabelFrame(pan, text="Streams", padding=4)
        pan.add(left_f, weight=1)
        pan.add(right_f, weight=2)

        st_lb = tk.Listbox(left_f, exportselection=False)
        st_sb = ttk.Scrollbar(left_f, command=st_lb.yview)
        st_lb.configure(yscrollcommand=st_sb.set)
        st_lb.pack(side="left", fill="both", expand=True)
        st_sb.pack(side="right", fill="y")

        str_lb = tk.Listbox(right_f, exportselection=False, font=("TkFixedFont", 9))
        str_sb = ttk.Scrollbar(right_f, command=str_lb.yview)
        str_lb.configure(yscrollcommand=str_sb.set)
        str_lb.pack(side="left", fill="both", expand=True)
        str_sb.pack(side="right", fill="y")

        status_var = tk.StringVar(value="Enter at least 3 characters, then Load results.")
        ttk.Label(dlg, textvariable=status_var, wraplength=700).pack(fill="x", padx=8, pady=4)

        bottom = ttk.Frame(dlg, padding=8)
        bottom.pack(fill="x")
        load_btn = ttk.Button(bottom, text="Load results")
        apply_btn = ttk.Button(bottom, text="Apply to form")
        ttk.Button(bottom, text="Cancel", command=dlg.destroy).pack(side="right", padx=2)
        apply_btn.pack(side="left", padx=2)
        load_btn.pack(side="left", padx=2)

        state: dict[str, object] = {"data": None, "names": None}
        stream_opts: list[FmStreamOption] = []

        def set_status(msg: str) -> None:
            status_var.set(msg)

        def populate_stations(data: object, names: list[str]) -> None:
            st_lb.delete(0, tk.END)
            str_lb.delete(0, tk.END)
            stream_opts.clear()
            for n in names:
                st_lb.insert(tk.END, n)
            state["data"], state["names"] = data, names
            set_status(f"Loaded {len(names)} stations. Select one, then pick a stream.")

        def refresh_streams(_event: object | None = None) -> None:
            str_lb.delete(0, tk.END)
            stream_opts.clear()
            data = state["data"]
            if not isinstance(data, list):
                return
            sel = st_lb.curselection()
            if not sel:
                return
            idx = sel[0]
            rows = data[idx]
            opts = stream_options_for_station(rows, mp3_only_var.get())
            for o in opts:
                str_lb.insert(tk.END, f"{o.label}  |  {o.url}")
                stream_opts.append(o)
            if not opts:
                set_status("No streams match the filter (try turning off MP3 only).")

        def on_load() -> None:
            query = search_var.get().strip()
            if len(query) < 3:
                messagebox.showwarning(
                    "Search too short",
                    "fmstream.org requires at least 3 characters in the search box.",
                    parent=dlg,
                )
                return
            url = fmstream_search_url(query)
            load_btn.state(["disabled"])
            set_status("Loading…")

            def work() -> None:
                try:
                    html = fetch_fmstream_page(url)
                    data, names = parse_fmstream_page(html)

                    def finish_ok() -> None:
                        load_btn.state(["!disabled"])
                        populate_stations(data, names)
                        if st_lb.size():
                            st_lb.selection_set(0)
                            refresh_streams()

                    self.root.after(0, finish_ok)
                except Exception as e:
                    err = str(e)

                    def finish_err() -> None:
                        load_btn.state(["!disabled"])
                        set_status(f"Error: {err}")
                        messagebox.showerror("Could not load search results", err, parent=dlg)

                    self.root.after(0, finish_err)

            threading.Thread(target=work, daemon=True).start()

        def on_mp3_toggle(*_args: object) -> None:
            refresh_streams()

        def on_apply() -> None:
            if state["data"] is None:
                messagebox.showwarning("Nothing loaded", "Run a search and load results first.", parent=dlg)
                return
            names = state["names"]
            if not isinstance(names, list):
                return
            ss = st_lb.curselection()
            ts = str_lb.curselection()
            if not ss:
                messagebox.showwarning("No station", "Select a station in the left list.", parent=dlg)
                return
            if not ts:
                messagebox.showwarning(
                    "No stream",
                    "Select a stream on the right (or adjust the MP3 filter).",
                    parent=dlg,
                )
                return
            name = str(names[ss[0]])
            opt = stream_opts[ts[0]]
            self.name_entry.delete(0, tk.END)
            self.name_entry.insert(0, name)
            self.url_entry.delete(0, tk.END)
            self.url_entry.insert(0, opt.url)
            dlg.destroy()
            self.status_var.set(f"Filled from fmstream.org: {name} — click Add Station to save.")

        load_btn.configure(command=on_load)
        apply_btn.configure(command=on_apply)
        search_entry.bind("<Return>", lambda _e: on_load())
        st_lb.bind("<<ListboxSelect>>", refresh_streams)
        mp3_only_var.trace_add("write", on_mp3_toggle)
        dlg.grab_set()

    def add_station(self) -> None:
        station = self._read_form_station(preserve=None)
        if station is None:
            return
        if self._station_name_exists(station.name, exclude_index=None):
            messagebox.showerror(
                "Duplicate station",
                "A station with this name already exists.\n"
                "Select it in the list and use Update Station to change keywords or URL.",
            )
            return
        self.stations.append(station)
        save_stations(self.stations)
        self._refresh_station_list(select_name=station.name)
        self.status_var.set(f"Added station: {station.name}")

    def update_station(self) -> None:
        idx = self._selected_index()
        if idx is None:
            messagebox.showwarning(
                "No station selected",
                "Select a station in the list, then edit the form and click Update Station.",
            )
            return
        old = self.stations[idx]
        station = self._read_form_station(preserve=old)
        if station is None:
            return
        if self._station_name_exists(station.name, exclude_index=idx):
            messagebox.showerror(
                "Duplicate name",
                "Another station already uses this name. Choose a unique name.",
            )
            return
        self.stations[idx] = station
        save_stations(self.stations)
        self._refresh_station_list(select_name=station.name)
        self.status_var.set(f"Updated station: {station.name}")

    def delete_station(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        removed = self.stations.pop(idx)
        save_stations(self.stations)
        self._refresh_station_list()
        self.status_var.set(f"Deleted: {removed.name}")

    def start(self) -> None:
        if self.running:
            return
        if not self.stations:
            messagebox.showerror("No stations", "Add at least one station.")
            return
        self.stop_event.clear()
        self.transcript_merge_state.reset()
        self.recorders = [StationRecorder(s, self.stop_event, self.transcribe_queue, self.ui_queue, self.log) for s in self.stations]
        for recorder in self.recorders:
            recorder.start()
        backend = default_transcribe_backend()
        shared = make_shared_transcriber_for_backend(backend)
        self.transcribers = []
        for worker_id in range(max(1, TRANSCRIBE_WORKERS)):
            worker = TranscriptionWorker(
                worker_id=worker_id + 1,
                transcribe_queue=self.transcribe_queue,
                stop_event=self.stop_event,
                ui_queue=self.ui_queue,
                log=self.log,
                alert_cache=self.alert_cache,
                alert_lock=self.alert_lock,
                merge_state=self.transcript_merge_state,
                get_contest_ai_enabled=lambda: self._contest_ai_enabled_shared[0],
                shared_transcriber=shared,
            )
            worker.start()
            self.transcribers.append(worker)
        self.running = True
        self.status_var.set(f"Running ({len(self.transcribers)} workers)")
        if self.start_btn:
            self.start_btn.configure(state="disabled")
        if self.stop_btn:
            self.stop_btn.configure(state="normal")

    def stop(self) -> None:
        if not self.running:
            return
        self.stop_event.set()
        self.running = False
        self.station_runtime.clear()
        self._rebuild_station_list_entries()
        self._sync_now_playing_label()
        self._refresh_weather_label()
        self._refresh_gas_label()
        self.status_var.set("Stopped")
        if self.start_btn:
            self.start_btn.configure(state="normal")
        if self.stop_btn:
            self.stop_btn.configure(state="disabled")

    def _note_speech_music_hint(self, station_name: str, is_music: bool) -> None:
        """Track DJ-talk vs music for station list when ICY is stale (all stations, not only selected)."""
        self.station_runtime.apply_speech_music_hint(station_name, is_music)
        self._refresh_station_list_row_styles()

    def _append_speech_line(self, station_name: str, text: str, is_music: bool = False) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.transcript.configure(state="normal")
        self.transcript.insert(tk.END, f"[{stamp}] ", "ts")
        if is_music:
            self.transcript.insert(tk.END, "♪ ", "music")
            self.transcript.insert(tk.END, text, "music")
            self.transcript.insert(tk.END, " ♪\n", "music")
        else:
            self.transcript.insert(tk.END, f"{text}\n")
        self.transcript.see(tk.END)
        self.transcript.configure(state="disabled")

    def _handle_speech_line(self, station_name: str, text: str, is_music: bool) -> None:
        self._note_speech_music_hint(station_name, is_music)
        ci_changed = self.station_runtime.feed_call_in_from_transcript(
            station_name, text, is_music
        )
        tz = self._station_tz(station_name)
        w_changed = self.station_runtime.feed_weather_from_transcript(
            station_name, text, is_music, tz
        )
        g_changed = self.station_runtime.feed_gas_from_transcript(station_name, text, is_music)
        sel = self.station_var.get().strip()
        if ci_changed and station_name == sel:
            self._refresh_call_in_label()
            self._schedule_call_in_verify(station_name)
        if station_name != sel:
            return
        self._append_speech_line(station_name, text, is_music)
        if w_changed:
            self._refresh_weather_label()
        if g_changed:
            self._refresh_gas_label()

    def _update_now_playing(self, station_name: str, title: str) -> None:
        rt = self.station_runtime.for_station(station_name)
        was_commercial = rt.commercial_break
        self.station_runtime.set_icy_title(station_name, title)
        if rt.commercial_break and not was_commercial:
            self._maybe_commercial_auto_switch(station_name)
        icy_ci = self.station_runtime.feed_call_in_from_icy(station_name, title)
        if station_name == self.station_var.get():
            self.now_playing_var.set(f"♪ {title}" if title else "—")
            if icy_ci:
                self._refresh_call_in_label()
                self._schedule_call_in_verify(station_name)
        self._rebuild_station_list_entries()

    def _append_alert(self, event: MatchEvent) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        kind = "LIVE" if event.is_live else "TEST"
        self.alerts.configure(state="normal")
        self.alerts.insert(tk.END, f"[{stamp}] {kind} {event.station} -> ")
        self.alerts.insert(tk.END, event.term, "keyword_green")
        self.alerts.insert(tk.END, "\n")
        insert_text_with_keyword_highlights(
            self.alerts,
            event.context_text,
            event.term,
            event.fuzzy_max_distance,
            "keyword_green",
        )
        self.alerts.insert(tk.END, "\n\n")
        self.alerts.see(tk.END)
        self.alerts.configure(state="disabled")
        if event.term == AI_CONTEST_ALERT_TERM:
            self._ai_feedback_pending = (event.station, event.context_text or "")
            self._btn_ai_good.configure(state=tk.NORMAL)
            self._btn_ai_bad.configure(state=tk.NORMAL)

    def _refresh_ai_learn_stats(self) -> None:
        try:
            p, n = contest_memory.learned_counts()
            self._ai_learn_stats.set(f"Learned: {p}↑ {n}↓")
        except Exception:
            pass
        try:
            ap, an = commercial_memory.learned_counts()
            self._ad_learn_stats.set(f"Ads learned: {ap}↑ {an}↓")
        except Exception:
            pass

    def _ai_learn_confirm(self) -> None:
        if not self._ai_feedback_pending:
            return
        st, ctx = self._ai_feedback_pending
        contest_memory.record_positive(st, ctx, "user_confirm")
        self._ai_feedback_pending = None
        self._btn_ai_good.configure(state=tk.DISABLED)
        self._btn_ai_bad.configure(state=tk.DISABLED)
        self._refresh_ai_learn_stats()
        self.status_var.set("Saved as contest example for AI few-shot learning.")

    def _ai_learn_reject(self) -> None:
        if not self._ai_feedback_pending:
            return
        st, ctx = self._ai_feedback_pending
        contest_memory.record_negative(st, ctx, "user_reject")
        self._ai_feedback_pending = None
        self._btn_ai_good.configure(state=tk.DISABLED)
        self._btn_ai_bad.configure(state=tk.DISABLED)
        self._refresh_ai_learn_stats()
        self.status_var.set("Saved as NO example — AI will treat similar text as unlikely contest.")

    def _teach_current_speech_as_ad(self) -> None:
        st = self.station_var.get().strip()
        if not st:
            self.status_var.set("Select a station in the list first.")
            return
        snip = (self._last_speech_snippet_by_station.get(st) or "").strip()
        if len(snip) < 24:
            self.status_var.set(
                "No recent speech chunk for this station yet — wait for transcription, then try again."
            )
            return
        commercial_memory.record_positive(st, snip, "user_speech_ad")
        self._refresh_ai_learn_stats()
        self.status_var.set("Saved latest speech as an ad example for AI few-shot learning.")

    def _ad_learn_confirm(self) -> None:
        if not self._ad_feedback_pending:
            return
        st, snip = self._ad_feedback_pending
        commercial_memory.record_positive(st, snip, "user_confirm")
        self._ad_feedback_pending = None
        self._btn_ad_good.configure(state=tk.DISABLED)
        self._btn_ad_bad.configure(state=tk.DISABLED)
        self._refresh_ai_learn_stats()
        self.status_var.set("Saved as ad example for commercial few-shot learning.")

    def _ad_learn_reject(self) -> None:
        if not self._ad_feedback_pending:
            return
        st, snip = self._ad_feedback_pending
        commercial_memory.record_negative(st, snip, "user_reject")
        self._ad_feedback_pending = None
        self._btn_ad_good.configure(state=tk.DISABLED)
        self._btn_ad_bad.configure(state=tk.DISABLED)
        self._refresh_ai_learn_stats()
        self.status_var.set("Saved as NOT-ad — similar transcript text is less likely to trigger red.")

    def _open_ads_database_window(self) -> None:
        """Browse commercial_memory + ICY ad marks; delete mistaken rows."""
        if self._ads_db_win is not None:
            try:
                if self._ads_db_win.winfo_exists():
                    self._ads_db_win.lift()
                    self._ads_db_win.focus_force()
                    return
            except tk.TclError:
                self._ads_db_win = None

        win = tk.Toplevel(self.root)
        self._ads_db_win = win
        win.title("Learned commercials (database)")
        win.geometry("900x580")
        win.transient(self.root)

        def _ts_short(iso: str) -> str:
            s = (iso or "").strip()
            return s[:19].replace("T", " ") if s else ""

        def _preview(s: str, n: int = 90) -> str:
            t = re.sub(r"[\r\n]+", " ", (s or "").strip())
            if len(t) > n:
                return t[: n - 1] + "…"
            return t

        detail_frame = ttk.LabelFrame(win, text="Selection (full text)", padding=6)
        detail_frame.pack(fill="x", padx=8, pady=(0, 8))
        detail = scrolledtext.ScrolledText(detail_frame, height=5, wrap=tk.WORD, font=("TkDefaultFont", 9))
        detail.pack(fill="x")
        detail.configure(state="disabled")

        def show_detail(text: str) -> None:
            detail.configure(state="normal")
            detail.delete("1.0", tk.END)
            detail.insert(tk.END, text or "")
            detail.configure(state="disabled")

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        pos_meta: dict[str, str] = {}
        neg_meta: dict[str, str] = {}
        icy_meta: dict[str, str] = {}

        def add_table_tab(
            label: str,
            *,
            load_rows: Callable[[], None],
            on_delete: Callable[[], None],
        ) -> ttk.Treeview:
            tab = ttk.Frame(nb)
            nb.add(tab, text=label)
            bar = ttk.Frame(tab)
            bar.pack(fill="x", pady=(0, 6))
            ttk.Button(bar, text="Refresh", command=load_rows).pack(side=tk.LEFT)
            ttk.Button(bar, text="Delete selected", command=on_delete).pack(side=tk.LEFT, padx=(8, 0))
            fr = ttk.Frame(tab)
            fr.pack(fill="both", expand=True)
            sy = ttk.Scrollbar(fr)
            sy.pack(side=tk.RIGHT, fill=tk.Y)
            cols = ("station", "source", "when", "preview")
            tv = ttk.Treeview(
                fr,
                columns=cols,
                show="headings",
                yscrollcommand=sy.set,
                height=14,
            )
            sy.config(command=tv.yview)
            tv.heading("station", text="Station")
            tv.heading("source", text="Source")
            tv.heading("when", text="Saved (UTC)")
            tv.heading("preview", text="Snippet (preview)")
            tv.column("station", width=130)
            tv.column("source", width=110)
            tv.column("when", width=136)
            tv.column("preview", width=440)
            tv.pack(side=tk.LEFT, fill="both", expand=True)
            return tv

        # --- Positives (YES / ad) ---
        def load_pos() -> None:
            pos_tv.delete(*pos_tv.get_children())
            pos_meta.clear()
            for rid, stn, src, cr, snip in commercial_memory.list_positive_rows():
                pos_tv.insert(
                    "",
                    tk.END,
                    iid=str(rid),
                    values=(stn, src, _ts_short(cr), _preview(snip)),
                )
                pos_meta[str(rid)] = snip

        def del_pos() -> None:
            sel = pos_tv.selection()
            if not sel:
                messagebox.showinfo("Delete", "Select a row first.", parent=win)
                return
            if not messagebox.askyesno(
                "Delete",
                "Remove this ad YES example from the database?",
                parent=win,
            ):
                return
            rid = int(sel[0])
            if commercial_memory.delete_positive_by_id(rid):
                load_pos()
                show_detail("")
                self._refresh_ai_learn_stats()
                self.status_var.set("Removed one ad YES row from commercial_memory.")

        pos_tv = add_table_tab("Ad YES (transcript / learning)", load_rows=load_pos, on_delete=del_pos)
        pos_tv.bind("<<TreeviewSelect>>", lambda _e: show_detail(pos_meta.get(pos_tv.selection()[0], "")) if pos_tv.selection() else None)

        # --- Negatives (NOT ad) ---
        def load_neg() -> None:
            neg_tv.delete(*neg_tv.get_children())
            neg_meta.clear()
            for rid, stn, src, cr, snip in commercial_memory.list_negative_rows():
                neg_tv.insert(
                    "",
                    tk.END,
                    iid=str(rid),
                    values=(stn, src, _ts_short(cr), _preview(snip)),
                )
                neg_meta[str(rid)] = snip

        def del_neg() -> None:
            sel = neg_tv.selection()
            if not sel:
                messagebox.showinfo("Delete", "Select a row first.", parent=win)
                return
            if not messagebox.askyesno(
                "Delete",
                "Remove this NOT-ad example from the database?",
                parent=win,
            ):
                return
            rid = int(sel[0])
            if commercial_memory.delete_negative_by_id(rid):
                load_neg()
                show_detail("")
                self._refresh_ai_learn_stats()
                self.status_var.set("Removed one NOT-ad row from commercial_memory.")

        neg_tv = add_table_tab("NOT ad (rejections)", load_rows=load_neg, on_delete=del_neg)
        neg_tv.bind("<<TreeviewSelect>>", lambda _e: show_detail(neg_meta.get(neg_tv.selection()[0], "")) if neg_tv.selection() else None)

        # --- ICY titles marked as ads (song_play_history) ---
        icy_tab = ttk.Frame(nb)
        nb.add(icy_tab, text="ICY marked as ad")
        icy_bar = ttk.Frame(icy_tab)
        icy_bar.pack(fill="x", pady=(0, 6))
        icy_fr = ttk.Frame(icy_tab)
        icy_fr.pack(fill="both", expand=True)
        icy_sy = ttk.Scrollbar(icy_fr)
        icy_sy.pack(side=tk.RIGHT, fill=tk.Y)
        icy_cols = ("title", "when")
        icy_tv = ttk.Treeview(
            icy_fr,
            columns=icy_cols,
            show="headings",
            yscrollcommand=icy_sy.set,
            height=14,
        )
        icy_sy.config(command=icy_tv.yview)
        icy_tv.heading("title", text="ICY title (as marked)")
        icy_tv.heading("when", text="Marked (UTC)")
        icy_tv.column("title", width=620)
        icy_tv.column("when", width=160)
        icy_tv.pack(side=tk.LEFT, fill="both", expand=True)

        def load_icy() -> None:
            icy_tv.delete(*icy_tv.get_children())
            icy_meta.clear()
            for nk, label, at in song_play_history.list_user_marked_icy_ads():
                iid = icy_tv.insert(
                    "",
                    tk.END,
                    values=(label, _ts_short(at)),
                )
                icy_meta[iid] = nk

        def del_icy() -> None:
            sel = icy_tv.selection()
            if not sel:
                messagebox.showinfo("Delete", "Select a row first.", parent=win)
                return
            nk = icy_meta.get(sel[0], "")
            if not nk:
                return
            if not messagebox.askyesno(
                "Unmark",
                "Stop treating this ICY title as an ad? (Song logging will be allowed again.)",
                parent=win,
            ):
                return
            if song_play_history.delete_user_marked_icy_ad(nk):
                load_icy()
                show_detail("")
                self.status_var.set("Removed ICY title from ad blocklist.")

        ttk.Button(icy_bar, text="Refresh", command=load_icy).pack(side=tk.LEFT)
        ttk.Button(icy_bar, text="Unmark selected", command=del_icy).pack(side=tk.LEFT, padx=(8, 0))

        def on_icy_sel(_e=None) -> None:
            sel = icy_tv.selection()
            if not sel:
                return
            nk = icy_meta.get(sel[0], "")
            lab = ""
            try:
                lab = icy_tv.item(sel[0], "values")[0] or ""
            except (tk.TclError, IndexError):
                pass
            show_detail(f"norm_key:\n{nk}\n\nlabel:\n{lab}")

        icy_tv.bind("<<TreeviewSelect>>", on_icy_sel)

        ttk.Label(
            win,
            text="Data: commercial_memory.sqlite3 (YES/NO snippets) and song_plays.sqlite3 "
            "(ICY titles you marked in song history). Deleting updates live behavior on refresh.",
            foreground="#555",
            wraplength=860,
            justify="left",
        ).pack(anchor="w", padx=10, pady=(0, 4))

        bottom = ttk.Frame(win)
        bottom.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(bottom, text="Close", command=win.destroy).pack(side=tk.RIGHT)

        load_pos()
        load_neg()
        load_icy()

        def _on_close_ads() -> None:
            self._ads_db_win = None
            try:
                win.destroy()
            except tk.TclError:
                pass

        win.protocol("WM_DELETE_WINDOW", _on_close_ads)

    def _persist_favorites_prefs(self) -> None:
        _save_monitor_prefs(self._monitor_prefs)

    def _select_station_by_name(self, name: str) -> None:
        name = name.strip()
        if not name:
            return
        for i, s in enumerate(self.stations):
            if s.name == name:
                self.station_list.selection_clear(0, tk.END)
                self.station_list.selection_set(i)
                self.station_list.see(i)
                self._show_station(s)
                self._clear_transcript_view()
                self._sync_stream_player()
                return

    def _maybe_favorite_auto_switch(self, event_station: str, new_title: str) -> None:
        if not self._listen_var.get():
            return
        if not bool(self._monitor_prefs.get("favorite_auto_switch")):
            return
        raw = self._monitor_prefs.get("favorite_songs_ordered")
        if not isinstance(raw, list):
            return
        favorites = [str(x).strip() for x in raw if str(x).strip()]
        if not favorites:
            return
        nt = (new_title or "").strip()
        if not nt or not looks_like_song_title(nt) or icy_suggests_commercial(nt):
            return
        if song_play_history.best_favorite_index_for_title(nt, favorites) is None:
            return
        cur = self.station_var.get().strip()
        if not cur or event_station.strip() == cur:
            return
        cur_title = (self.station_runtime.for_station(cur).icy_title or "").strip()
        if song_play_history.icy_matches_any_favorite(cur_title, favorites):
            return
        now = time.time()
        if now - self._last_favorite_auto_switch_ts < 20.0:
            return
        self._last_favorite_auto_switch_ts = now
        self._select_station_by_name(event_station.strip())
        self.status_var.set(
            f"Switched to {event_station.strip()} — favorite track in stream metadata."
        )

    @staticmethod
    def _parse_hist_date(s: str, *, end_of_day: bool) -> datetime | None:
        s = (s or "").strip()
        if not s:
            return None
        try:
            d = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if end_of_day:
                return d.replace(hour=23, minute=59, second=59, microsecond=999999)
            return d.replace(hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            return None

    def _open_song_history_window(self) -> None:
        if self._song_hist_win is not None:
            try:
                if self._song_hist_win.winfo_exists():
                    self._song_hist_win.lift()
                    self._song_hist_win.focus_force()
                    return
            except tk.TclError:
                self._song_hist_win = None

        win = tk.Toplevel(self.root)
        self._song_hist_win = win
        win.title("Song plays & favorites")
        win.geometry("820x640")
        win.transient(self.root)

        top = ttk.LabelFrame(win, text="Collective plays (ICY metadata, all stations)", padding=8)
        top.pack(fill="both", expand=True, padx=8, pady=8)

        filt = ttk.Frame(top)
        filt.pack(fill="x")
        ttk.Label(filt, text="From (UTC date)").pack(side=tk.LEFT)
        today = datetime.now(timezone.utc).date()
        from_default = (today - timedelta(days=7)).isoformat()
        from_var = tk.StringVar(value=from_default)
        to_var = tk.StringVar(value=today.isoformat())
        ttk.Entry(filt, textvariable=from_var, width=12).pack(side=tk.LEFT, padx=(4, 8))
        ttk.Label(filt, text="To").pack(side=tk.LEFT)
        ttk.Entry(filt, textvariable=to_var, width=12).pack(side=tk.LEFT, padx=(4, 8))
        search_var = tk.StringVar()
        ttk.Label(filt, text="Search").pack(side=tk.LEFT, padx=(12, 0))
        ttk.Entry(filt, textvariable=search_var, width=18).pack(side=tk.LEFT, padx=4)

        tree_frame = ttk.Frame(top)
        tree_frame.pack(fill="both", expand=True, pady=8)
        scroll_y = ttk.Scrollbar(tree_frame)
        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        cols = ("title", "plays", "last", "st")
        tree = ttk.Treeview(
            tree_frame,
            columns=cols,
            show="headings",
            yscrollcommand=scroll_y.set,
            height=14,
        )
        scroll_y.config(command=tree.yview)
        tree.heading("title", text="Song (latest title text)")
        tree.heading("plays", text="Plays")
        tree.heading("last", text="Last (UTC)")
        tree.heading("st", text="Station (last)")
        tree.column("title", width=320)
        tree.column("plays", width=52)
        tree.column("last", width=160)
        tree.column("st", width=120)
        tree.pack(side=tk.LEFT, fill="both", expand=True)

        ttk.Label(
            top,
            text="Commercials sometimes appear here because ICY metadata looks like “Artist - Title”. "
            "Select a row and use “Mark selection as ad” to drop it from stats, stop logging it as a song, "
            "and add it as a YES example for commercial detection (same learning DB as “Confirm ad”).",
            foreground="#555",
            wraplength=780,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        fav_list = list(self._monitor_prefs.get("favorite_songs_ordered") or [])
        if not isinstance(fav_list, list):
            fav_list = []
        fav_list = [str(x) for x in fav_list]

        def refresh_tree() -> None:
            for x in tree.get_children():
                tree.delete(x)
            start_d = self._parse_hist_date(from_var.get(), end_of_day=False)
            end_d = self._parse_hist_date(to_var.get(), end_of_day=True)
            if start_d and end_d and start_d > end_d:
                start_d, end_d = end_d, start_d
            rows = song_play_history.aggregate_by_title(
                start_d,
                end_d,
                search_substring=search_var.get(),
                limit=350,
            )
            for r in rows:
                last_short = r.last_played_at[:19] if len(r.last_played_at) >= 19 else r.last_played_at
                tree.insert(
                    "",
                    tk.END,
                    values=(r.display_title, r.play_count, last_short, r.station_sample),
                )

        bot = ttk.LabelFrame(win, text="Favorite songs (order = priority) & auto-switch", padding=8)
        bot.pack(fill="x", padx=8, pady=(0, 8))

        auto_var = tk.BooleanVar(value=bool(self._monitor_prefs.get("favorite_auto_switch")))

        def on_auto_toggle() -> None:
            self._monitor_prefs["favorite_auto_switch"] = bool(auto_var.get())
            self._persist_favorites_prefs()

        ttk.Checkbutton(
            bot,
            text="Auto-switch played stream when a favorite appears on another station "
            "(only if the station you are listening to is not already playing a favorite)",
            variable=auto_var,
            command=on_auto_toggle,
        ).pack(anchor="w")

        ttk.Label(
            bot,
            text="Double-click a row above to append that song to favorites. "
            "Higher entries are preferred when several favorites match.",
            foreground="#555",
            wraplength=760,
            justify="left",
        ).pack(anchor="w", pady=(4, 6))

        mid = ttk.Frame(bot)
        mid.pack(fill="both", expand=True)
        fb_scroll = ttk.Scrollbar(mid)
        fb_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        fav_box = tk.Listbox(mid, height=8, yscrollcommand=fb_scroll.set, font=("TkDefaultFont", 9))
        fb_scroll.config(command=fav_box.yview)
        fav_box.pack(side=tk.LEFT, fill="both", expand=True)

        def sync_fav_box() -> None:
            fav_box.delete(0, tk.END)
            for line in fav_list:
                fav_box.insert(tk.END, line)

        def save_fav_order() -> None:
            self._monitor_prefs["favorite_songs_ordered"] = list(fav_list)
            self._persist_favorites_prefs()

        sync_fav_box()

        def add_title_to_favorites(title: str) -> None:
            t = (title or "").strip()
            if len(t) < 3:
                return
            if t not in fav_list:
                fav_list.append(t)
                sync_fav_box()
                save_fav_order()

        def on_tree_double_click(_evt=None) -> None:
            sel = tree.selection()
            if not sel:
                return
            vals = tree.item(sel[0], "values")
            if vals and vals[0]:
                add_title_to_favorites(str(vals[0]))

        tree.bind("<Double-1>", on_tree_double_click)

        btn_row = ttk.Frame(bot)
        btn_row.pack(fill="x", pady=6)

        def fav_move(delta: int) -> None:
            sel = fav_box.curselection()
            if not sel:
                return
            i = sel[0]
            j = i + delta
            if j < 0 or j >= fav_box.size():
                return
            fav_list[i], fav_list[j] = fav_list[j], fav_list[i]
            sync_fav_box()
            fav_box.selection_set(j)
            save_fav_order()

        def fav_remove() -> None:
            sel = fav_box.curselection()
            if not sel:
                return
            i = sel[0]
            fav_list.pop(i)
            sync_fav_box()
            save_fav_order()

        def add_now_playing_fav() -> None:
            st = self.station_var.get().strip()
            if not st:
                return
            icy = (self.station_runtime.for_station(st).icy_title or "").strip()
            if not icy:
                return
            add_title_to_favorites(icy)

        ttk.Button(btn_row, text="↑", width=3, command=lambda: fav_move(-1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="↓", width=3, command=lambda: fav_move(1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="Remove", command=fav_remove).pack(side=tk.LEFT, padx=8)
        ttk.Button(
            btn_row,
            text="Add now playing (selected station)",
            command=add_now_playing_fav,
        ).pack(side=tk.LEFT, padx=8)

        def mark_selection_as_ad() -> None:
            sel = tree.selection()
            if not sel:
                messagebox.showinfo(
                    "Mark as ad",
                    "Select a row in the song list first.",
                    parent=win,
                )
                return
            vals = tree.item(sel[0], "values")
            if not vals or not str(vals[0]).strip():
                return
            title = str(vals[0]).strip()
            nk = song_play_history.normalize_song_key(title)
            preview = title if len(title) <= 100 else title[:97] + "…"
            if not messagebox.askyesno(
                "Mark as ad",
                "Remove this title from collective song stats, block it from being logged again as a song, "
                "and teach the commercial detector that this stream metadata is an ad?\n\n"
                f"{preview}",
                parent=win,
            ):
                return
            song_play_history.mark_norm_as_ad(nk, title)
            commercial_memory.record_positive("icy_metadata", title, "song_history_ad")
            refresh_tree()
            self._refresh_ai_learn_stats()
            self.status_var.set(
                "Marked as ad: stripped from song history; commercial few-shot updated (if title long enough)."
            )

        top_btn = ttk.Frame(top)
        top_btn.pack(fill="x", pady=(8, 4))
        ttk.Button(top_btn, text="Refresh list", command=refresh_tree).pack(side=tk.LEFT)
        ttk.Button(
            top_btn,
            text="Mark selection as ad (false song)",
            command=mark_selection_as_ad,
        ).pack(side=tk.LEFT, padx=(12, 0))

        def close_hist() -> None:
            self._song_hist_win = None
            try:
                win.destroy()
            except tk.TclError:
                pass

        win.protocol("WM_DELETE_WINDOW", close_hist)
        ttk.Button(top_btn, text="Close", command=close_hist).pack(side=tk.RIGHT)
        refresh_tree()

    def _poll_ui_queue(self) -> None:
        try:
            while True:
                item = self.ui_queue.get_nowait()
                if not isinstance(item, tuple) or len(item) != 3:
                    continue
                kind, station_name, payload = item
                if kind == "speech":
                    if isinstance(payload, tuple) and len(payload) == 2:
                        line = str(payload[0])
                        self._handle_speech_line(str(station_name), line, bool(payload[1]))
                    else:
                        line = str(payload)
                        self._handle_speech_line(str(station_name), line, False)
                    t = line.strip()
                    if t:
                        self._last_speech_snippet_by_station[str(station_name)] = t[:1200]
                elif kind == "now_playing":
                    self._update_now_playing(str(station_name), str(payload))
                elif kind == "alert":
                    if isinstance(payload, MatchEvent):
                        self._append_alert(payload)
                elif kind == "song":
                    title = str(payload).strip()
                    self._update_now_playing(str(station_name), title)
                    if (
                        title
                        and looks_like_song_title(title)
                        and not icy_suggests_commercial(title)
                    ):
                        song_play_history.record_play(str(station_name), title)
                    self._maybe_favorite_auto_switch(str(station_name), title)
                elif kind == "commercial":
                    sn = str(station_name)
                    was_commercial = self.station_runtime.for_station(sn).commercial_break
                    self.station_runtime.set_commercial_break(sn, bool(payload))
                    self._refresh_station_list_row_styles()
                    if bool(payload) and not was_commercial:
                        self._maybe_commercial_auto_switch(sn)
                    if payload:
                        snip = self._last_speech_snippet_by_station.get(
                            str(station_name), ""
                        ).strip()
                        if len(snip) >= 24:
                            self._ad_feedback_pending = (str(station_name), snip)
                            self._btn_ad_good.configure(state=tk.NORMAL)
                            self._btn_ad_bad.configure(state=tk.NORMAL)
                    else:
                        self._ad_feedback_pending = None
                        self._btn_ad_good.configure(state=tk.DISABLED)
                        self._btn_ad_bad.configure(state=tk.DISABLED)
                elif kind == "stream_delay":
                    if isinstance(payload, tuple) and len(payload) == 2:
                        try:
                            raw_sec = float(payload[0])
                            self._apply_stream_delay(str(station_name), raw_sec, str(payload[1]))
                        except (TypeError, ValueError):
                            pass
        except Empty:
            pass
        self.root.after(300, self._poll_ui_queue)

    def on_close(self) -> None:
        self._stop_stream_player()
        self.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    RadioMonitorApp().run()
