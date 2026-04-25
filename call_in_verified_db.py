"""Persist web-verified studio phone vs SMS lines per station (and ad rejections)."""

from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timezone

import config

_lock = threading.Lock()
_ready = False


def _db_path() -> str:
    return str(config.DATA_DIR / "call_in_verified.sqlite3")


def _init() -> None:
    global _ready
    if _ready:
        return
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        if _ready:
            return
        conn = sqlite3.connect(_db_path())
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS verified_line (
                    station_name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('studio', 'sms')),
                    display TEXT NOT NULL,
                    norm_key TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (station_name, role)
                );
                CREATE TABLE IF NOT EXISTS rejected_ad (
                    station_name TEXT NOT NULL,
                    norm_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (station_name, norm_key)
                );
                """
            )
            conn.commit()
        finally:
            conn.close()
        _ready = True


def norm_key_from_segment(segment: str) -> str:
    t = (segment or "").strip()
    if re.match(r"(?i)^text\s+", t):
        d = re.sub(r"\D", "", t)
        if len(d) in (5, 6):
            return f"sms:{d}"
    d = re.sub(r"\D", "", t)
    if len(d) >= 10:
        return d[-11:] if len(d) > 11 else d
    if len(d) in (5, 6):
        return f"sms:{d}"
    return t.lower()[:80]


def get_verified_pair(station_name: str) -> tuple[str, str]:
    """Returns (studio_display, sms_display), may be empty."""
    st = (station_name or "").strip() or "unknown"
    _init()
    with _lock:
        conn = sqlite3.connect(_db_path())
        try:
            studio = ""
            sms = ""
            row = conn.execute(
                "SELECT display FROM verified_line WHERE station_name = ? AND role = 'studio'",
                (st,),
            ).fetchone()
            if row:
                studio = row[0] or ""
            row = conn.execute(
                "SELECT display FROM verified_line WHERE station_name = ? AND role = 'sms'",
                (st,),
            ).fetchone()
            if row:
                sms = row[0] or ""
            return studio.strip(), sms.strip()
        finally:
            conn.close()


def upsert_verified(station_name: str, role: str, display: str, norm_key: str) -> None:
    st = (station_name or "").strip() or "unknown"
    role = role if role in ("studio", "sms") else "studio"
    disp = (display or "").strip()
    nk = (norm_key or "").strip()
    if not disp or not nk:
        return
    ts = datetime.now(timezone.utc).isoformat()
    _init()
    with _lock:
        conn = sqlite3.connect(_db_path())
        try:
            conn.execute(
                """
                INSERT INTO verified_line (station_name, role, display, norm_key, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(station_name, role) DO UPDATE SET
                    display = excluded.display,
                    norm_key = excluded.norm_key,
                    updated_at = excluded.updated_at
                """,
                (st, role, disp, nk, ts),
            )
            conn.commit()
        finally:
            conn.close()


def is_rejected_ad(station_name: str, norm_key: str) -> bool:
    st = (station_name or "").strip() or "unknown"
    nk = (norm_key or "").strip()
    if not nk:
        return False
    _init()
    with _lock:
        conn = sqlite3.connect(_db_path())
        try:
            row = conn.execute(
                "SELECT 1 FROM rejected_ad WHERE station_name = ? AND norm_key = ?",
                (st, nk),
            ).fetchone()
            return row is not None
        finally:
            conn.close()


def add_rejected_ad(station_name: str, norm_key: str) -> None:
    st = (station_name or "").strip() or "unknown"
    nk = (norm_key or "").strip()
    if not nk:
        return
    ts = datetime.now(timezone.utc).isoformat()
    _init()
    with _lock:
        conn = sqlite3.connect(_db_path())
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO rejected_ad (station_name, norm_key, created_at)
                VALUES (?, ?, ?)
                """,
                (st, nk, ts),
            )
            conn.commit()
        finally:
            conn.close()
