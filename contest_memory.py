"""
Learn contest / call-in phrasing over time: store snippets Groq confirmed (or the user
confirmed), inject them as few-shot examples into the next Groq checks. False-alarm
snippets are stored as explicit NO examples.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from datetime import datetime, timezone

import config

_lock = threading.Lock()
_schema_ready = False

_MAX_SNIPPET = int(os.getenv("CONTEST_LEARN_SNIPPET_CHARS", "520"))
_MAX_POSITIVE = int(os.getenv("CONTEST_LEARN_MAX_POSITIVE", "4"))
_MAX_NEGATIVE = int(os.getenv("CONTEST_LEARN_MAX_NEGATIVE", "2"))
_MAX_ROWS_PER_STATION = int(os.getenv("CONTEST_LEARN_MAX_ROWS_PER_STATION", "250"))


def _db_path() -> str:
    return str(config.DATA_DIR / "contest_memory.sqlite3")


def _clip(s: str) -> str:
    s = (s or "").strip()
    if len(s) > _MAX_SNIPPET:
        return s[: _MAX_SNIPPET - 1] + "…"
    return s


def _hash(snippet: str) -> str:
    return hashlib.sha256(snippet.strip().encode("utf-8")).hexdigest()


def init_schema() -> None:
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
                CREATE TABLE IF NOT EXISTS contest_positive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    station_name TEXT NOT NULL,
                    snippet TEXT NOT NULL,
                    snippet_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_contest_pos
                    ON contest_positive(station_name, snippet_hash);

                CREATE TABLE IF NOT EXISTS contest_negative (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    station_name TEXT NOT NULL,
                    snippet TEXT NOT NULL,
                    snippet_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_contest_neg
                    ON contest_negative(station_name, snippet_hash);
                """
            )
            conn.commit()
        finally:
            conn.close()
        _schema_ready = True


def _prune_old(conn: sqlite3.Connection, table: str, station: str) -> None:
    (cnt,) = conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE station_name = ?", (station,)
    ).fetchone()
    excess = int(cnt) - _MAX_ROWS_PER_STATION
    if excess <= 0:
        return
    conn.execute(
        f"""
        DELETE FROM {table}
        WHERE id IN (
            SELECT id FROM {table}
            WHERE station_name = ?
            ORDER BY datetime(created_at) ASC
            LIMIT ?
        )
        """,
        (station, excess),
    )


def record_positive(station_name: str, snippet: str, source: str) -> None:
    """Remember text the model (or user) marked as a real contest/call-in cue."""
    snippet = _clip(snippet)
    if len(snippet) < 24:
        return
    st = (station_name or "").strip() or "unknown"
    init_schema()
    h = _hash(snippet)
    ts = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = sqlite3.connect(_db_path())
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO contest_positive
                    (station_name, snippet, snippet_hash, created_at, source)
                VALUES (?, ?, ?, ?, ?)
                """,
                (st, snippet, h, ts, source[:32]),
            )
            _prune_old(conn, "contest_positive", st)
            conn.commit()
        finally:
            conn.close()


def record_negative(station_name: str, snippet: str, source: str = "user") -> None:
    """Remember a false alarm so similar wording is discouraged."""
    snippet = _clip(snippet)
    if len(snippet) < 24:
        return
    st = (station_name or "").strip() or "unknown"
    init_schema()
    h = _hash(snippet)
    ts = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = sqlite3.connect(_db_path())
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO contest_negative
                    (station_name, snippet, snippet_hash, created_at, source)
                VALUES (?, ?, ?, ?, ?)
                """,
                (st, snippet, h, ts, source[:32]),
            )
            _prune_old(conn, "contest_negative", st)
            conn.commit()
        finally:
            conn.close()


def fetch_few_shot_messages(station_name: str) -> list[dict]:
    """
    Build extra chat messages: positive examples -> assistant YES, negative -> NO.
    Caller prepends system + these + final user classify message.
    """
    st = (station_name or "").strip() or "unknown"
    init_schema()
    positives: list[str] = []
    negatives: list[str] = []
    with _lock:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute(
                """
                SELECT snippet FROM contest_positive
                WHERE station_name = ?
                ORDER BY datetime(created_at) DESC
                LIMIT ?
                """,
                (st, _MAX_POSITIVE),
            ):
                positives.append(row["snippet"])
            need = _MAX_POSITIVE - len(positives)
            if need > 0:
                for row in conn.execute(
                    """
                    SELECT snippet FROM contest_positive
                    WHERE station_name != ?
                    ORDER BY datetime(created_at) DESC
                    LIMIT ?
                    """,
                    (st, need),
                ):
                    positives.append(row["snippet"])
            for row in conn.execute(
                """
                SELECT snippet FROM contest_negative
                WHERE station_name = ?
                ORDER BY datetime(created_at) DESC
                LIMIT ?
                """,
                (st, _MAX_NEGATIVE),
            ):
                negatives.append(row["snippet"])
            need_n = _MAX_NEGATIVE - len(negatives)
            if need_n > 0:
                for row in conn.execute(
                    """
                    SELECT snippet FROM contest_negative
                    WHERE station_name != ?
                    ORDER BY datetime(created_at) DESC
                    LIMIT ?
                    """,
                    (st, need_n),
                ):
                    negatives.append(row["snippet"])
        finally:
            conn.close()

    out: list[dict] = []
    for snip in positives[:_MAX_POSITIVE]:
        out.append(
            {
                "role": "user",
                "content": "Classify this radio transcript excerpt. Reply YES or NO only.\n\n" + snip,
            }
        )
        out.append({"role": "assistant", "content": "YES"})
    for snip in negatives[:_MAX_NEGATIVE]:
        out.append(
            {
                "role": "user",
                "content": "Classify this radio transcript excerpt. Reply YES or NO only.\n\n" + snip,
            }
        )
        out.append({"role": "assistant", "content": "NO"})
    return out


def learned_counts() -> tuple[int, int]:
    """(positive_rows, negative_rows) for status display."""
    init_schema()
    with _lock:
        conn = sqlite3.connect(_db_path())
        try:
            p = conn.execute("SELECT COUNT(*) FROM contest_positive").fetchone()[0]
            n = conn.execute("SELECT COUNT(*) FROM contest_negative").fetchone()[0]
            return int(p), int(n)
        finally:
            conn.close()
