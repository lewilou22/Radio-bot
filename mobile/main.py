"""
KivyMD Radio Contest Monitor — Android + desktop (PYTHONPATH=. python mobile/main.py).
"""

from __future__ import annotations

from datetime import datetime
from queue import Empty
import threading

from kivy.clock import Clock
from kivy.metrics import dp
from kivy.properties import BooleanProperty, StringProperty
from kivy.uix.scrollview import ScrollView
from kivymd.app import MDApp
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.button import MDRaisedButton, MDFlatButton
from kivymd.uix.dialog import MDDialog
from kivymd.uix.floatlayout import MDFloatLayout
from kivymd.uix.label import MDLabel
from kivymd.uix.list import MDList, OneLineListItem, TwoLineListItem
from kivymd.uix.screen import MDScreen
from kivymd.uix.selectioncontrol import MDSwitch
from kivymd.uix.tab import MDTabs, MDTabsBase
from kivymd.uix.textfield import MDTextField
from kivymd.uix.toolbar import MDTopAppBar

import commercial_memory
import contest_memory
import song_play_history
from config import (
    StationConfig,
    apply_secrets_to_environ,
    load_secrets,
    load_stations,
    refresh_env_keys,
    save_secrets,
    save_stations,
)
from fmstream_client import (
    fetch_fmstream_page,
    fmstream_search_url,
    parse_fmstream_page,
    stream_options_for_station,
)
from monitor_controller import MonitorController
from monitor_event_handler import (
    MonitorUIContext,
    process_ui_queue_item,
    runtime_status_line,
    scrape_call_in_background,
    verify_call_in_background,
)
from radio_engine import (
    MatchEvent,
    StationRuntimeIndex,
    _load_monitor_prefs,
    _save_monitor_prefs,
    looks_like_song_title,
    setup_log,
)
from mobile.bootstrap import init_app_environment


def _terms_from_text(raw: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for line in raw.replace(",", "\n").splitlines():
        t = line.strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _terms_to_text(terms: list[str]) -> str:
    return "\n".join(terms)


class TabMonitor(MDFloatLayout, MDTabsBase):
    pass


class TabStations(MDFloatLayout, MDTabsBase):
    pass


class TabSettings(MDFloatLayout, MDTabsBase):
    pass


class RadioMonitorApp(MDApp):
    title = "Radio Monitor"
    status_text = StringProperty("Idle")
    running = BooleanProperty(False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.log = setup_log()
        self._monitor = MonitorController(log=self.log)
        self.stations: list[StationConfig] = []
        self.station_runtime = StationRuntimeIndex()
        self._monitor_prefs: dict = {}
        self._ui_ctx: MonitorUIContext | None = None
        self._selected_station = ""
        self._call_in_scrape_inflight: set[str] = set()
        self._call_in_verify_inflight: set[str] = set()
        self._call_in_cache_sec = 3600.0
        self._ai_feedback_pending: tuple[str, str] | None = None
        self._ad_feedback_pending: tuple[str, str] | None = None
        self._fmstream_dialog: MDDialog | None = None
        self._fmstream_data: list | None = None
        self._fmstream_names: list[str] = []

    def build(self):
        init_app_environment()
        self.stations = load_stations()
        self._monitor_prefs = _load_monitor_prefs()
        self._monitor_prefs.setdefault("favorite_songs_ordered", [])
        self._monitor_prefs.setdefault("favorite_auto_switch", False)
        self._monitor_prefs.setdefault("commercial_auto_switch", False)
        self._monitor.set_contest_ai_enabled(bool(self._monitor_prefs.get("contest_ai_groq", False)))
        self._rebuild_ui_ctx()

        root = MDScreen()
        box = MDBoxLayout(orientation="vertical")
        self.toolbar = MDTopAppBar(title="Radio Contest Monitor", elevation=2)
        box.add_widget(self.toolbar)

        self.tabs = MDTabs()
        self._build_monitor_tab()
        self._build_stations_tab()
        self._build_settings_tab()
        box.add_widget(self.tabs)

        self.status_label = MDLabel(
            text="Idle",
            size_hint_y=None,
            height=dp(28),
            theme_text_color="Secondary",
            halign="center",
        )
        box.add_widget(self.status_label)
        root.add_widget(box)
        Clock.schedule_interval(self._poll_ui_queue, 0.3)
        Clock.schedule_interval(self._bind_status, 0.5)
        if self.stations:
            self._select_station(self.stations[0].name)
        return root

    def _rebuild_ui_ctx(self) -> None:
        self._ui_ctx = MonitorUIContext(
            stations=self.stations,
            station_runtime=self.station_runtime,
            monitor_prefs=self._monitor_prefs,
            selected_station=self._selected_station,
            listen_enabled=True,
        )

    def _bind_status(self, _dt: float) -> None:
        self.status_label.text = self.status_text

    # --- Monitor tab ---

    def _build_monitor_tab(self) -> None:
        tab = TabMonitor(title="Monitor")
        layout = MDBoxLayout(orientation="vertical", padding=dp(8), spacing=dp(6))

        row = MDBoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        self.btn_start = MDRaisedButton(text="Start", on_release=lambda *_: self.start_monitor())
        self.btn_stop = MDRaisedButton(text="Stop", on_release=lambda *_: self.stop_monitor())
        row.add_widget(self.btn_start)
        row.add_widget(self.btn_stop)
        layout.add_widget(row)

        self.station_list_monitor = MDList(size_hint_y=None, height=dp(160))
        scroll_st = ScrollView(size_hint_y=None, height=dp(160))
        scroll_st.add_widget(self.station_list_monitor)
        layout.add_widget(scroll_st)

        self.meta_label = MDLabel(
            text="—",
            size_hint_y=None,
            height=dp(56),
            theme_text_color="Secondary",
        )
        layout.add_widget(self.meta_label)

        layout.add_widget(MDLabel(text="Transcript (selected station)", size_hint_y=None, height=dp(22)))
        self.transcript_label = MDLabel(
            text="",
            size_hint_y=None,
            markup=True,
            valign="top",
        )
        scroll_tx = ScrollView()
        scroll_tx.add_widget(self.transcript_label)
        layout.add_widget(scroll_tx)

        layout.add_widget(MDLabel(text="Alerts", size_hint_y=None, height=dp(22)))
        self.alerts_label = MDLabel(text="", size_hint_y=None, markup=True, valign="top")
        scroll_al = ScrollView(size_hint_y=None, height=dp(140))
        scroll_al.add_widget(self.alerts_label)
        layout.add_widget(scroll_al)

        learn_row = MDBoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        self.btn_ad_good = MDFlatButton(text="Ad ✓", on_release=lambda *_: self._ad_learn_confirm())
        self.btn_ad_bad = MDFlatButton(text="Ad ✗", on_release=lambda *_: self._ad_learn_reject())
        self.btn_ai_good = MDFlatButton(text="Contest ✓", on_release=lambda *_: self._ai_learn_confirm())
        self.btn_ai_bad = MDFlatButton(text="Contest ✗", on_release=lambda *_: self._ai_learn_reject())
        learn_row.add_widget(self.btn_ad_good)
        learn_row.add_widget(self.btn_ad_bad)
        learn_row.add_widget(self.btn_ai_good)
        learn_row.add_widget(self.btn_ai_bad)
        layout.add_widget(learn_row)

        tab.add_widget(layout)
        self.tabs.add_widget(tab)
        self._refresh_monitor_station_list()

    def _refresh_monitor_station_list(self) -> None:
        self.station_list_monitor.clear_widgets()
        for st in self.stations:
            rt = self.station_runtime.for_station(st.name)
            icy = (rt.icy_title or "—")[:48]
            item = TwoLineListItem(
                text=st.name,
                secondary_text=icy,
                on_release=lambda x, n=st.name: self._select_station(n),
            )
            icy_t = (rt.icy_title or "").strip()
            if rt.commercial_break:
                item.text_color = (0.75, 0.15, 0.15, 1)
            elif icy_t and looks_like_song_title(icy_t) and not rt.dj_talk_overlay:
                item.text_color = (0.15, 0.39, 0.92, 1)
            self.station_list_monitor.add_widget(item)

    def _select_station(self, name: str) -> None:
        self._selected_station = name.strip()
        if self._ui_ctx:
            self._ui_ctx.selected_station = self._selected_station
        self._refresh_meta()
        self._schedule_call_in_scrape(name)
        self._schedule_call_in_verify(name)

    def _refresh_meta(self) -> None:
        if not self._ui_ctx or not self._selected_station:
            self.meta_label.text = "—"
            return
        self.meta_label.text = runtime_status_line(self._ui_ctx, self._selected_station)

    def start_monitor(self) -> None:
        if self.running:
            return
        self.stations = load_stations()
        self._rebuild_ui_ctx()
        self._monitor.set_contest_ai_enabled(bool(self._monitor_prefs.get("contest_ai_groq", False)))
        n = self._monitor.start(self.stations)
        if n == 0:
            self.status_text = "No enabled stations with URLs"
            return
        self.running = True
        self.status_text = f"Running ({n} workers)"

    def stop_monitor(self) -> None:
        if not self.running:
            return
        self._monitor.stop()
        self.running = False
        self.station_runtime.clear()
        self._refresh_monitor_station_list()
        self._refresh_meta()
        self.status_text = "Stopped"

    def _poll_ui_queue(self, _dt: float) -> None:
        if not self._ui_ctx:
            return
        try:
            while True:
                item = self._monitor.ui_queue.get_nowait()
                if not isinstance(item, tuple) or len(item) != 3:
                    continue
                kind, station_name, payload = item
                res = process_ui_queue_item(self._ui_ctx, str(kind), str(station_name), payload)
                if res is None:
                    continue
                self._apply_ui_result(res)
        except Empty:
            pass

    def _apply_ui_result(self, res) -> None:
        sn = res.station_name
        if res.record_song_play:
            song_play_history.record_play(res.record_song_play[0], res.record_song_play[1])
        if res.commercial is not None or res.now_playing:
            self._refresh_monitor_station_list()
        if res.favorite_auto_switch_to:
            self._select_station(res.favorite_auto_switch_to)
            self.status_text = f"Favorite song — switched to {res.favorite_auto_switch_to}"
        if res.commercial_auto_switch_to:
            self._select_station(res.commercial_auto_switch_to)
            self.status_text = f"Ad on {sn} — switched to {res.commercial_auto_switch_to}"
        if res.call_in_changed and sn == self._selected_station:
            self._refresh_meta()
            self._schedule_call_in_verify(sn)
        if res.weather_changed or res.gas_changed:
            if sn == self._selected_station:
                self._refresh_meta()
        if res.stream_delay and sn == self._selected_station:
            try:
                raw_sec, heard = res.stream_delay
                self.station_runtime.apply_stream_delay(sn, raw_sec, heard)
                self._refresh_meta()
            except Exception:
                pass
        if res.speech_line and sn == self._selected_station:
            stamp = datetime.now().strftime("%H:%M:%S")
            prefix = f"[color=3366cc][{stamp}] ♪[/color] " if res.speech_is_music else f"[{stamp}] "
            line = res.speech_line.replace("[", "(").replace("]", ")")
            prev = self.transcript_label.text or ""
            self.transcript_label.text = (prev + prefix + line + "\n")[-12000:]
            self.transcript_label.height = max(dp(200), len(self.transcript_label.text) * 0.35)
        if res.alert:
            self._append_alert(res.alert)
            if res.ai_feedback_context:
                self._ai_feedback_pending = (sn, res.ai_feedback_context)
        if res.ad_feedback_snippet:
            self._ad_feedback_pending = (sn, res.ad_feedback_snippet)
        if res.now_playing and sn == self._selected_station:
            self._refresh_meta()

    def _append_alert(self, event: MatchEvent) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        live = "LIVE" if event.is_live else "TEST"
        line = f"[b][{stamp}] {live}[/b] {event.station}: {event.term}\n{event.context_text[:400]}\n\n"
        prev = self.alerts_label.text or ""
        self.alerts_label.text = (prev + line)[-8000:]
        self.alerts_label.height = max(dp(80), len(self.alerts_label.text) * 0.4)

    def _schedule_call_in_scrape(self, station_name: str) -> None:
        if station_name in self._call_in_scrape_inflight or not self._ui_ctx:
            return
        st = self._ui_ctx.station_by_name(station_name)
        if not st or not (st.call_in_page_url or "").strip():
            return
        self._call_in_scrape_inflight.add(station_name)

        def work() -> None:
            scrape_call_in_background(self._ui_ctx, station_name, self._call_in_cache_sec)
            Clock.schedule_once(lambda _dt: self._on_scrape_done(station_name), 0)

        threading.Thread(target=work, daemon=True).start()

    def _on_scrape_done(self, station_name: str) -> None:
        self._call_in_scrape_inflight.discard(station_name)
        if station_name == self._selected_station:
            self._refresh_meta()

    def _schedule_call_in_verify(self, station_name: str) -> None:
        if not self._ui_ctx or station_name in self._call_in_verify_inflight:
            return
        rt = self.station_runtime.for_station(station_name)
        candidate = (rt.call_in_from_air or rt.call_in_from_scrape or "").strip()
        if not candidate:
            return
        self._call_in_verify_inflight.add(station_name)

        def work() -> None:
            if self._ui_ctx:
                verify_call_in_background(self._ui_ctx, station_name)
            Clock.schedule_once(lambda _dt: self._on_verify_done(station_name), 0)

        threading.Thread(target=work, daemon=True).start()

    def _on_verify_done(self, station_name: str) -> None:
        self._call_in_verify_inflight.discard(station_name)
        if station_name == self._selected_station:
            self._refresh_meta()

    def _ad_learn_confirm(self) -> None:
        if not self._ad_feedback_pending:
            return
        st, snip = self._ad_feedback_pending
        commercial_memory.record_positive(st, snip, "user_confirm")
        self._ad_feedback_pending = None
        self.status_text = "Saved ad example"

    def _ad_learn_reject(self) -> None:
        if not self._ad_feedback_pending:
            return
        st, snip = self._ad_feedback_pending
        commercial_memory.record_negative(st, snip, "user_reject")
        self._ad_feedback_pending = None
        self.status_text = "Saved NOT-ad example"

    def _ai_learn_confirm(self) -> None:
        if not self._ai_feedback_pending:
            return
        st, ctx = self._ai_feedback_pending
        contest_memory.record_positive(st, ctx, "user_confirm")
        self._ai_feedback_pending = None
        self.status_text = "Saved contest example"

    def _ai_learn_reject(self) -> None:
        if not self._ai_feedback_pending:
            return
        st, ctx = self._ai_feedback_pending
        contest_memory.record_negative(st, ctx, "user_reject")
        self._ai_feedback_pending = None
        self.status_text = "Saved contest NO example"

    # --- Stations tab ---

    def _build_stations_tab(self) -> None:
        tab = TabStations(title="Stations")
        layout = MDBoxLayout(orientation="vertical", padding=dp(8), spacing=dp(4))

        self.field_name = MDTextField(hint_text="Station name")
        self.field_url = MDTextField(hint_text="Stream URL")
        self.field_live = MDTextField(hint_text="Live terms (comma or newline)", multiline=True)
        self.field_dev = MDTextField(hint_text="Test terms", multiline=True)
        self.field_webhook = MDTextField(hint_text="Discord webhook URL")
        self.field_discord_name = MDTextField(hint_text="Discord bot name")
        self.field_mention = MDTextField(hint_text="Discord mention")
        self.field_tz = MDTextField(hint_text="Timezone", text="America/Toronto")
        self.field_call_in = MDTextField(hint_text="Call-in number (manual)")
        self.field_call_in_page = MDTextField(hint_text="Station web page (call-in scrape)")
        self.switch_enabled = MDSwitch(active=True, size_hint=(None, None), size=(dp(48), dp(32)))
        layout.add_widget(self.field_name)
        layout.add_widget(self.field_url)
        layout.add_widget(self.field_live)
        layout.add_widget(self.field_dev)
        layout.add_widget(self.field_webhook)
        layout.add_widget(self.field_discord_name)
        layout.add_widget(self.field_mention)
        layout.add_widget(self.field_tz)
        layout.add_widget(self.field_call_in)
        layout.add_widget(self.field_call_in_page)
        layout.add_widget(MDLabel(text="Station enabled", size_hint_y=None, height=dp(24)))
        layout.add_widget(self.switch_enabled)

        btn_row = MDBoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        btn_row.add_widget(MDRaisedButton(text="Add", on_release=lambda *_: self._station_add()))
        btn_row.add_widget(MDRaisedButton(text="Update", on_release=lambda *_: self._station_update()))
        btn_row.add_widget(MDFlatButton(text="Delete", on_release=lambda *_: self._station_delete()))
        btn_row.add_widget(MDFlatButton(text="FmStream", on_release=lambda *_: self._open_fmstream_dialog()))
        layout.add_widget(btn_row)

        self.station_pick_list = MDList(size_hint_y=None, height=dp(120))
        scroll = ScrollView(size_hint_y=None, height=dp(120))
        scroll.add_widget(self.station_pick_list)
        layout.add_widget(scroll)

        tab.add_widget(layout)
        self.tabs.add_widget(tab)
        self._refresh_station_pick_list()

    def _refresh_station_pick_list(self) -> None:
        self.station_pick_list.clear_widgets()
        for st in self.stations:
            self.station_pick_list.add_widget(
                OneLineListItem(text=st.name, on_release=lambda x, s=st: self._load_station_form(s))
            )

    def _load_station_form(self, st: StationConfig) -> None:
        self.field_name.text = st.name
        self.field_url.text = st.url
        self.field_live.text = _terms_to_text(st.live_terms)
        self.field_dev.text = _terms_to_text(st.dev_terms)
        self.field_webhook.text = st.discord_webhook_url
        self.field_discord_name.text = st.discord_username
        self.field_mention.text = st.discord_mention
        self.field_tz.text = st.station_timezone
        self.field_call_in.text = st.call_in_number
        self.field_call_in_page.text = st.call_in_page_url
        self.switch_enabled.active = st.enabled

    def _read_station_form(self, preserve: StationConfig | None = None) -> StationConfig | None:
        name = (self.field_name.text or "").strip()
        url = (self.field_url.text or "").strip()
        if not name or not url:
            self.status_text = "Name and URL required"
            return None
        live = _terms_from_text(self.field_live.text or "")
        dev = _terms_from_text(self.field_dev.text or "")
        chunk = preserve.chunk_time_seconds if preserve else 10
        fuzzy = preserve.fuzzy_max_distance if preserve else 0
        return StationConfig(
            name=name,
            url=url,
            live_terms=live,
            dev_terms=dev,
            chunk_time_seconds=chunk,
            fuzzy_max_distance=fuzzy,
            enabled=bool(self.switch_enabled.active),
            discord_webhook_url=(self.field_webhook.text or "").strip(),
            discord_username=(self.field_discord_name.text or "").strip() or "Radio Keyword Bot",
            discord_mention=(self.field_mention.text or "").strip(),
            station_timezone=(self.field_tz.text or "").strip() or "America/Toronto",
            call_in_number=(self.field_call_in.text or "").strip(),
            call_in_page_url=(self.field_call_in_page.text or "").strip(),
        )

    def _station_add(self) -> None:
        st = self._read_station_form()
        if not st:
            return
        if any(s.name.lower() == st.name.lower() for s in self.stations):
            self.status_text = "Duplicate station name"
            return
        self.stations.append(st)
        save_stations(self.stations)
        self._rebuild_ui_ctx()
        self._refresh_station_pick_list()
        self._refresh_monitor_station_list()
        self.status_text = f"Added {st.name}"

    def _station_update(self) -> None:
        name = (self.field_name.text or "").strip()
        idx = next((i for i, s in enumerate(self.stations) if s.name == name), None)
        if idx is None:
            self.status_text = "Select existing station to update"
            return
        st = self._read_station_form(self.stations[idx])
        if not st:
            return
        self.stations[idx] = st
        save_stations(self.stations)
        self._rebuild_ui_ctx()
        self._refresh_station_pick_list()
        self._refresh_monitor_station_list()
        self.status_text = f"Updated {st.name}"

    def _station_delete(self) -> None:
        name = (self.field_name.text or "").strip()
        self.stations = [s for s in self.stations if s.name != name]
        save_stations(self.stations)
        self._rebuild_ui_ctx()
        self._refresh_station_pick_list()
        self._refresh_monitor_station_list()
        self.status_text = f"Deleted {name}"

    def _open_fmstream_dialog(self) -> None:
        field = MDTextField(hint_text="Search fmstream.org (3+ chars)")
        content = MDBoxLayout(orientation="vertical", spacing=dp(8), size_hint_y=None, height=dp(120))
        content.add_widget(field)
        results = MDList(size_hint_y=None, height=dp(200))

        def do_search(*_args) -> None:
            q = (field.text or "").strip()
            if len(q) < 3:
                self.status_text = "Enter at least 3 characters"
                return
            try:
                html = fetch_fmstream_page(fmstream_search_url(q))
                data, names = parse_fmstream_page(html)
            except Exception as exc:
                self.status_text = f"FmStream error: {exc}"
                return
            self._fmstream_data = data
            self._fmstream_names = names
            results.clear_widgets()
            for i, name in enumerate(names[:30]):
                if i >= len(data):
                    break
                opts = stream_options_for_station(data[i], mp3_only=True)
                if not opts:
                    opts = stream_options_for_station(data[i], mp3_only=False)
                url = opts[0].url if opts else ""
                if not url:
                    continue
                results.add_widget(
                    OneLineListItem(
                        text=f"{name[:50]} — {url[:40]}",
                        on_release=lambda x, u=url, n=name: self._pick_fmstream(n, u),
                    )
                )

        content.add_widget(MDRaisedButton(text="Search", on_release=do_search))
        scroll = ScrollView(size_hint_y=None, height=dp(200))
        scroll.add_widget(results)
        content.add_widget(scroll)
        self._fmstream_dialog = MDDialog(
            title="FmStream search",
            type="custom",
            content_cls=content,
            buttons=[MDFlatButton(text="Close", on_release=lambda *_: self._fmstream_dialog.dismiss())],
        )
        self._fmstream_dialog.open()

    def _pick_fmstream(self, name: str, url: str) -> None:
        self.field_name.text = name
        self.field_url.text = url
        if self._fmstream_dialog:
            self._fmstream_dialog.dismiss()
        self.status_text = "Station filled from FmStream — tap Add to save"

    # --- Settings tab ---

    def _build_settings_tab(self) -> None:
        tab = TabSettings(title="Settings")
        layout = MDBoxLayout(orientation="vertical", padding=dp(8), spacing=dp(6))
        secrets = load_secrets()
        self.field_groq = MDTextField(hint_text="GROQ_API_KEY", text=secrets.get("GROQ_API_KEY", ""))
        self.field_openai = MDTextField(hint_text="OPENAI_API_KEY", text=secrets.get("OPENAI_API_KEY", ""))
        self.field_discord = MDTextField(hint_text="DISCORD_WEBHOOK_URL", text=secrets.get("DISCORD_WEBHOOK_URL", ""))
        self.field_backend = MDTextField(
            hint_text="TRANSCRIBE_BACKEND (api|groq|openai|local)",
            text=secrets.get("TRANSCRIBE_BACKEND", "api"),
        )
        layout.add_widget(self.field_groq)
        layout.add_widget(self.field_openai)
        layout.add_widget(self.field_discord)
        layout.add_widget(self.field_backend)

        self.switch_contest_ai = MDSwitch(active=bool(self._monitor_prefs.get("contest_ai_groq")))
        layout.add_widget(MDLabel(text="Groq contest / call-in scan", size_hint_y=None, height=dp(24)))
        layout.add_widget(self.switch_contest_ai)

        self.switch_fav_auto = MDSwitch(active=bool(self._monitor_prefs.get("favorite_auto_switch")))
        self.switch_ad_auto = MDSwitch(active=bool(self._monitor_prefs.get("commercial_auto_switch")))
        layout.add_widget(MDLabel(text="Favorite song auto-switch", size_hint_y=None, height=dp(24)))
        layout.add_widget(self.switch_fav_auto)
        layout.add_widget(MDLabel(text="Commercial auto-switch station", size_hint_y=None, height=dp(24)))
        layout.add_widget(self.switch_ad_auto)

        layout.add_widget(
            MDRaisedButton(text="Save settings", on_release=lambda *_: self._save_settings())
        )
        layout.add_widget(
            MDFlatButton(text="Song history", on_release=lambda *_: self._open_song_history())
        )
        layout.add_widget(
            MDFlatButton(text="Teach last speech as ad", on_release=lambda *_: self._teach_speech_ad())
        )
        favs = self._monitor_prefs.get("favorite_songs_ordered") or []
        self.field_favorites = MDTextField(
            hint_text="Favorite songs (one per line, for auto-switch)",
            text="\n".join(favs) if isinstance(favs, list) else "",
            multiline=True,
        )
        layout.add_widget(self.field_favorites)
        tab.add_widget(layout)
        self.tabs.add_widget(tab)

    def _save_settings(self) -> None:
        secrets = {
            "GROQ_API_KEY": (self.field_groq.text or "").strip(),
            "OPENAI_API_KEY": (self.field_openai.text or "").strip(),
            "DISCORD_WEBHOOK_URL": (self.field_discord.text or "").strip(),
            "TRANSCRIBE_BACKEND": (self.field_backend.text or "api").strip().lower() or "api",
        }
        save_secrets(secrets)
        apply_secrets_to_environ()
        refresh_env_keys()
        self._monitor_prefs["contest_ai_groq"] = bool(self.switch_contest_ai.active)
        self._monitor_prefs["favorite_auto_switch"] = bool(self.switch_fav_auto.active)
        self._monitor_prefs["commercial_auto_switch"] = bool(self.switch_ad_auto.active)
        self._monitor_prefs["favorite_songs_ordered"] = _terms_from_text(
            self.field_favorites.text or ""
        )
        _save_monitor_prefs(self._monitor_prefs)
        self._monitor.set_contest_ai_enabled(bool(self.switch_contest_ai.active))
        self.status_text = "Settings saved"

    def _teach_speech_ad(self) -> None:
        if not self._ui_ctx or not self._selected_station:
            self.status_text = "Select a station on Monitor tab"
            return
        snip = (self._ui_ctx.last_speech_snippet_by_station.get(self._selected_station) or "").strip()
        if len(snip) < 24:
            self.status_text = "Wait for speech transcription first"
            return
        commercial_memory.record_positive(self._selected_station, snip, "user_speech_ad")
        self.status_text = "Saved speech as ad example"

    def _open_song_history(self) -> None:
        try:
            rows = song_play_history.aggregate_by_title(None, None, limit=40)
        except Exception as exc:
            self.status_text = f"Song history error: {exc}"
            return
        lines = [f"{r.play_count}× {r.display_title[:60]}" for r in rows]
        text = "\n".join(lines) or "(no plays logged yet)"
        dlg = MDDialog(title="Song history (top 40)", text=text)
        dlg.buttons = [MDFlatButton(text="OK", on_release=lambda *_: dlg.dismiss())]
        dlg.open()


def main() -> None:
    RadioMonitorApp().run()


if __name__ == "__main__":
    main()
