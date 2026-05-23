"""Shared start/stop orchestration for desktop Tk and mobile Kivy UIs."""

from __future__ import annotations

import logging
import threading
from queue import Queue

from config import TRANSCRIBE_WORKERS, StationConfig
from radio_engine import (
    StationRecorder,
    TranscriptMergeState,
    TranscriptionWorker,
    setup_log,
)
from transcribers import default_transcribe_backend, make_shared_transcriber_for_backend


class MonitorController:
    def __init__(self, log: logging.Logger | None = None) -> None:
        self.log = log or setup_log()
        self.stop_event = threading.Event()
        self.ui_queue: Queue = Queue()
        self.transcribe_queue: Queue = Queue()
        self.transcript_merge_state = TranscriptMergeState()
        self.alert_cache: dict[tuple[str, str], float] = {}
        self.alert_lock = threading.Lock()
        self.recorders: list[StationRecorder] = []
        self.transcribers: list[TranscriptionWorker] = []
        self.running = False
        self._contest_ai_enabled = False

    def set_contest_ai_enabled(self, enabled: bool) -> None:
        self._contest_ai_enabled = bool(enabled)

    def contest_ai_enabled(self) -> bool:
        return self._contest_ai_enabled

    def active_stations(self, stations: list[StationConfig]) -> list[StationConfig]:
        return [s for s in stations if s.enabled and (s.url or "").strip()]

    def start(self, stations: list[StationConfig]) -> int:
        """Start recorders and transcription workers. Returns worker count."""
        if self.running:
            return len(self.transcribers)
        active = self.active_stations(stations)
        if not active:
            return 0
        self.stop_event.clear()
        self.transcript_merge_state.reset()
        self.recorders = [
            StationRecorder(s, self.stop_event, self.transcribe_queue, self.ui_queue, self.log)
            for s in active
        ]
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
                get_contest_ai_enabled=self.contest_ai_enabled,
                shared_transcriber=shared,
            )
            worker.start()
            self.transcribers.append(worker)
        self.running = True
        self.log.info(
            "Monitor started: %s station(s), %s worker(s), backend=%s",
            len(active),
            len(self.transcribers),
            backend,
        )
        return len(self.transcribers)

    def stop(self) -> None:
        if not self.running:
            return
        self.stop_event.set()
        self.running = False
        self.recorders = []
        self.transcribers = []
        self.log.info("Monitor stopped")
