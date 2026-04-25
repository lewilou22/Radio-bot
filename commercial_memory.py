"""
Few-shot learning for ad/commercial classification: store snippets the model (or user)
marked as paid spots vs not, inject into OpenAI/Groq commercial checks.
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

_MAX_SNIPPET = int(os.getenv("COMMERCIAL_LEARN_SNIPPET_CHARS", "520"))
_MAX_POSITIVE = int(os.getenv("COMMERCIAL_LEARN_MAX_POSITIVE", "4"))
_MAX_NEGATIVE = int(os.getenv("COMMERCIAL_LEARN_MAX_NEGATIVE", "2"))
_MAX_ROWS_PER_STATION = int(os.getenv("COMMERCIAL_LEARN_MAX_ROWS_PER_STATION", "250"))


def _db_path() -> str:
    return str(config.DATA_DIR / "commercial_memory.sqlite3")


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
                CREATE TABLE IF NOT EXISTS commercial_positive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    station_name TEXT NOT NULL,
                    snippet TEXT NOT NULL,
                    snippet_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_commercial_pos
                    ON commercial_positive(station_name, snippet_hash);

                CREATE TABLE IF NOT EXISTS commercial_negative (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    station_name TEXT NOT NULL,
                    snippet TEXT NOT NULL,
                    snippet_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_commercial_neg
                    ON commercial_negative(station_name, snippet_hash);
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
    snippet = _clip(snippet)
    if len(snippet) < 24:
        return
    st = (station_name or "").strip() or "unknown"
    init_schema()
    h = _hash(snippet)
    ts = datetime.now(timezone.utc).isoformat()
    src = source[:32]
    # User-taught YES overrides a prior NO for the same clipped text (API auto-yes does not).
    clear_neg = src in ("user_confirm", "user_speech_ad")
    with _lock:
        conn = sqlite3.connect(_db_path())
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO commercial_positive
                    (station_name, snippet, snippet_hash, created_at, source)
                VALUES (?, ?, ?, ?, ?)
                """,
                (st, snippet, h, ts, src),
            )
            if clear_neg:
                conn.execute(
                    "DELETE FROM commercial_negative WHERE station_name = ? AND snippet_hash = ?",
                    (st, h),
                )
            _prune_old(conn, "commercial_positive", st)
            conn.commit()
        finally:
            conn.close()


def record_negative(station_name: str, snippet: str, source: str = "user") -> None:
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
                INSERT OR IGNORE INTO commercial_negative
                    (station_name, snippet, snippet_hash, created_at, source)
                VALUES (?, ?, ?, ?, ?)
                """,
                (st, snippet, h, ts, source[:32]),
            )
            conn.execute(
                "DELETE FROM commercial_positive WHERE station_name = ? AND snippet_hash = ?",
                (st, h),
            )
            _prune_old(conn, "commercial_negative", st)
            conn.commit()
        finally:
            conn.close()


def fetch_few_shot_messages(station_name: str) -> list[dict]:
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
                SELECT snippet FROM commercial_positive
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
                    SELECT snippet FROM commercial_positive
                    WHERE station_name != ?
                    ORDER BY datetime(created_at) DESC
                    LIMIT ?
                    """,
                    (st, need),
                ):
                    positives.append(row["snippet"])
            for row in conn.execute(
                """
                SELECT snippet FROM commercial_negative
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
                    SELECT snippet FROM commercial_negative
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
                "content": "Is this a paid radio advertisement or sponsored spot? Reply YES or NO only.\n\n"
                + snip,
            }
        )
        out.append({"role": "assistant", "content": "YES"})
    for snip in negatives[:_MAX_NEGATIVE]:
        out.append(
            {
                "role": "user",
                "content": "Is this a paid radio advertisement or sponsored spot? Reply YES or NO only.\n\n"
                + snip,
            }
        )
        out.append({"role": "assistant", "content": "NO"})
    return out


def transcript_matches_saved_ad_positive(station_name: str, transcript: str) -> bool:
    """
    True if transcript text contains (or is contained in) a user- or API-saved YES snippet
    for this station — used so taught ads count as commercial without re-calling the model.
    """
    tx_raw = (transcript or "").strip()
    if len(tx_raw) < 24:
        return False
    tx = tx_raw.lower()
    st = (station_name or "").strip() or "unknown"
    init_schema()
    with _lock:
        conn = sqlite3.connect(_db_path())
        try:
            rows = conn.execute(
                """
                SELECT snippet FROM commercial_positive
                WHERE station_name = ?
                ORDER BY datetime(created_at) DESC
                LIMIT 40
                """,
                (st,),
            ).fetchall()
        finally:
            conn.close()
    for (snip,) in rows:
        s = (snip or "").strip().lower()
        if len(s) < 40:
            continue
        if s in tx:
            return True
        if len(tx) >= 48 and tx in s:
            return True
    return False


def learned_counts() -> tuple[int, int]:
    init_schema()
    with _lock:
        conn = sqlite3.connect(_db_path())
        try:
            p = conn.execute("SELECT COUNT(*) FROM commercial_positive").fetchone()[0]
            n = conn.execute("SELECT COUNT(*) FROM commercial_negative").fetchone()[0]
            return int(p), int(n)
        finally:
            conn.close()


def list_positive_rows(limit: int = 500) -> list[tuple[int, str, str, str, str]]:
    """Newest first: (id, station_name, source, created_at, snippet)."""
    lim = max(1, min(int(limit), 2000))
    init_schema()
    with _lock:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, station_name, source, created_at, snippet
                FROM commercial_positive
                ORDER BY datetime(created_at) DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
            return [
                (
                    int(r["id"]),
                    str(r["station_name"] or ""),
                    str(r["source"] or ""),
                    str(r["created_at"] or ""),
                    str(r["snippet"] or ""),
                )
                for r in rows
            ]
        finally:
            conn.close()


def list_negative_rows(limit: int = 500) -> list[tuple[int, str, str, str, str]]:
    """Newest first: (id, station_name, source, created_at, snippet)."""
    lim = max(1, min(int(limit), 2000))
    init_schema()
    with _lock:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, station_name, source, created_at, snippet
                FROM commercial_negative
                ORDER BY datetime(created_at) DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
            return [
                (
                    int(r["id"]),
                    str(r["station_name"] or ""),
                    str(r["source"] or ""),
                    str(r["created_at"] or ""),
                    str(r["snippet"] or ""),
                )
                for r in rows
            ]
        finally:
            conn.close()


def delete_positive_by_id(row_id: int) -> bool:
    init_schema()
    with _lock:
        conn = sqlite3.connect(_db_path())
        try:
            cur = conn.execute("DELETE FROM commercial_positive WHERE id = ?", (int(row_id),))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def delete_negative_by_id(row_id: int) -> bool:
    init_schema()
    with _lock:
        conn = sqlite3.connect(_db_path())
        try:
            cur = conn.execute("DELETE FROM commercial_negative WHERE id = ?", (int(row_id),))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
