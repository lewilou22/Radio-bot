"""SQLite store for per-station transcript chunks (all stations, shared file)."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

import config

_lock = threading.Lock()
_schema_ready = False


def _db_path() -> str:
    return str(config.DATA_DIR / "transcripts.sqlite3")


def init_transcript_db() -> None:
    global _schema_ready
    if _schema_ready:
        return
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        if _schema_ready:
            return
        conn = sqlite3.connect(_db_path())
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS transcripts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    station_name TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    text TEXT NOT NULL,
                    is_music INTEGER NOT NULL DEFAULT 0,
                    icy_title TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_transcripts_station_time
                    ON transcripts (station_name, received_at DESC);
                """
            )
            conn.commit()
        finally:
            conn.close()
        _schema_ready = True


def append_transcript(
    station_name: str,
    text: str,
    *,
    is_music: bool,
    icy_title: str = "",
) -> None:
    """Record one non-empty transcript chunk (workers may call from multiple threads)."""
    if not (text or "").strip():
        return
    init_transcript_db()
    ts = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = sqlite3.connect(_db_path())
        try:
            conn.execute(
                """
                INSERT INTO transcripts (station_name, received_at, text, is_music, icy_title)
                VALUES (?, ?, ?, ?, ?)
                """,
                (station_name.strip(), ts, text.strip(), 1 if is_music else 0, icy_title or ""),
            )
            conn.commit()
        finally:
            conn.close()
