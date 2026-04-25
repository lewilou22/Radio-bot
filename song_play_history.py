"""
Log ICY song metadata plays across all stations; aggregate counts and date-range queries.
Used for a collective "what's been played" view and favorite-based station switching.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import config

_lock = threading.Lock()
_schema_ready = False
_marked_ad_table_ready = False

# Skip re-logging the same track on the same station within this window (metadata bounce).
_DEDUP_SECONDS_SAME_STATION = 180


def _db_path() -> str:
    return str(config.DATA_DIR / "song_plays.sqlite3")


def _ensure_marked_ad_table() -> None:
    """Migration for DBs created before song_marked_ad_norm existed."""
    global _marked_ad_table_ready
    if _marked_ad_table_ready:
        return
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        if _marked_ad_table_ready:
            return
        conn = sqlite3.connect(_db_path())
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS song_marked_ad_norm (
                    norm_key TEXT PRIMARY KEY,
                    label_title TEXT NOT NULL,
                    marked_at TEXT NOT NULL
                );
                """
            )
            conn.commit()
        finally:
            conn.close()
        _marked_ad_table_ready = True


def normalize_song_key(title: str) -> str:
    """Stable key for grouping the same logical track across stations / formatting variants."""
    t = (title or "").lower().strip()
    t = re.sub(r"\s+", " ", t)
    return t


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
                CREATE TABLE IF NOT EXISTS song_plays (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    played_at TEXT NOT NULL,
                    station_name TEXT NOT NULL,
                    raw_title TEXT NOT NULL,
                    norm_key TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_song_plays_time
                    ON song_plays(played_at DESC);
                CREATE INDEX IF NOT EXISTS idx_song_plays_norm_time
                    ON song_plays(norm_key, played_at DESC);
                """
            )
            conn.commit()
        finally:
            conn.close()
        _schema_ready = True


def record_play(station_name: str, raw_title: str) -> None:
    """Append one play row if title passes caller's filters (e.g. looks like music, not an ad)."""
    raw = (raw_title or "").strip()
    if len(raw) < 3:
        return
    st = (station_name or "").strip() or "unknown"
    nk = normalize_song_key(raw)
    if len(nk) < 3:
        return
    ts = datetime.now(timezone.utc).isoformat()
    init_schema()
    _ensure_marked_ad_table()
    with _lock:
        conn = sqlite3.connect(_db_path())
        try:
            if conn.execute(
                "SELECT 1 FROM song_marked_ad_norm WHERE norm_key = ?", (nk,)
            ).fetchone():
                return
            row = conn.execute(
                """
                SELECT played_at FROM song_plays
                WHERE station_name = ? AND norm_key = ?
                ORDER BY id DESC LIMIT 1
                """,
                (st, nk),
            ).fetchone()
            if row:
                try:
                    prev_raw = row[0].replace("Z", "+00:00")
                    prev = datetime.fromisoformat(prev_raw)
                    if prev.tzinfo is None:
                        prev = prev.replace(tzinfo=timezone.utc)
                    delta = datetime.now(timezone.utc) - prev
                    if delta.total_seconds() < _DEDUP_SECONDS_SAME_STATION:
                        return
                except (TypeError, ValueError):
                    pass
            conn.execute(
                """
                INSERT INTO song_plays (played_at, station_name, raw_title, norm_key)
                VALUES (?, ?, ?, ?)
                """,
                (ts, st, raw, nk),
            )
            conn.commit()
        finally:
            conn.close()


@dataclass(frozen=True)
class SongAggregateRow:
    display_title: str
    play_count: int
    last_played_at: str
    first_played_at: str
    station_sample: str


def aggregate_by_title(
    start_utc: datetime | None,
    end_utc: datetime | None,
    search_substring: str = "",
    limit: int = 400,
) -> list[SongAggregateRow]:
    """Group by norm_key; display_title is the most recent raw_title for that key in range."""
    init_schema()
    start_utc = start_utc or datetime(1970, 1, 1, tzinfo=timezone.utc)
    end_utc = end_utc or datetime.now(timezone.utc) + timedelta(days=1)
    start_s = start_utc.astimezone(timezone.utc).isoformat()
    end_s = end_utc.astimezone(timezone.utc).isoformat()
    needle = (search_substring or "").strip().lower()
    fetch_cap = min(max(limit * 8, limit), 4000)

    _ensure_marked_ad_table()
    with _lock:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        try:
            groups = conn.execute(
                """
                WITH ranked AS (
                    SELECT norm_key,
                           raw_title,
                           played_at,
                           station_name,
                           COUNT(*) OVER (PARTITION BY norm_key) AS play_count,
                           MIN(played_at) OVER (PARTITION BY norm_key) AS first_at,
                           MAX(played_at) OVER (PARTITION BY norm_key) AS last_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY norm_key ORDER BY played_at DESC
                           ) AS rn
                    FROM song_plays
                    WHERE played_at >= ? AND played_at <= ?
                      AND norm_key NOT IN (SELECT norm_key FROM song_marked_ad_norm)
                )
                SELECT norm_key, raw_title, station_name, play_count, first_at, last_at
                FROM ranked
                WHERE rn = 1
                ORDER BY play_count DESC, last_at DESC
                LIMIT ?
                """,
                (start_s, end_s, fetch_cap),
            ).fetchall()
        finally:
            conn.close()

    rows: list[SongAggregateRow] = []
    for g in groups:
        disp = str(g["raw_title"])
        nk = str(g["norm_key"]).lower()
        if needle and needle not in nk and needle not in disp.lower():
            continue
        rows.append(
            SongAggregateRow(
                display_title=disp,
                play_count=int(g["play_count"]),
                last_played_at=str(g["last_at"]),
                first_played_at=str(g["first_at"]),
                station_sample=str(g["station_name"] or "")[:40],
            )
        )
        if len(rows) >= limit:
            break

    return rows


def title_matches_favorite(icy_title: str, favorite_line: str) -> bool:
    """True if stream metadata plausibly matches a user favorite (exact, or substring)."""
    i = normalize_song_key(icy_title)
    f = normalize_song_key(favorite_line)
    if not f or not i:
        return False
    if f == i:
        return True
    if len(f) >= 5 and f in i:
        return True
    if len(i) >= 5 and i in f:
        return True
    return False


def icy_matches_any_favorite(icy_title: str, favorites: list[str]) -> bool:
    if not (icy_title or "").strip():
        return False
    return any(title_matches_favorite(icy_title, fav) for fav in favorites if fav.strip())


def best_favorite_index_for_title(title: str, favorites: list[str]) -> int | None:
    """Smallest list index that matches title, or None."""
    best: int | None = None
    for idx, fav in enumerate(favorites):
        if not fav.strip():
            continue
        if title_matches_favorite(title, fav):
            if best is None or idx < best:
                best = idx
    return best


def icy_title_is_user_marked_ad(raw_title: str) -> bool:
    """True if this stream metadata title was marked as a false song / ad in song history."""
    raw = (raw_title or "").strip()
    if len(raw) < 3:
        return False
    nk = normalize_song_key(raw)
    if len(nk) < 3:
        return False
    init_schema()
    _ensure_marked_ad_table()
    with _lock:
        conn = sqlite3.connect(_db_path())
        try:
            row = conn.execute(
                "SELECT 1 FROM song_marked_ad_norm WHERE norm_key = ? LIMIT 1",
                (nk,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()


def list_user_marked_icy_ads(limit: int = 500) -> list[tuple[str, str, str]]:
    """Newest first: (norm_key, label_title, marked_at)."""
    lim = max(1, min(int(limit), 2000))
    init_schema()
    _ensure_marked_ad_table()
    with _lock:
        conn = sqlite3.connect(_db_path())
        try:
            rows = conn.execute(
                """
                SELECT norm_key, label_title, marked_at
                FROM song_marked_ad_norm
                ORDER BY datetime(marked_at) DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
            return [
                (str(a or ""), str(b or ""), str(c or ""))
                for a, b, c in rows
            ]
        finally:
            conn.close()


def delete_user_marked_icy_ad(norm_key: str) -> bool:
    """Remove ICY title from the ad blocklist (song logging allowed again)."""
    nk = (norm_key or "").strip()
    if len(nk) < 2:
        return False
    init_schema()
    _ensure_marked_ad_table()
    with _lock:
        conn = sqlite3.connect(_db_path())
        try:
            cur = conn.execute("DELETE FROM song_marked_ad_norm WHERE norm_key = ?", (nk,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def mark_norm_as_ad(norm_key: str, label_title: str) -> None:
    """
    User says this ICY string was a commercial, not music: drop play history for it,
    block future logging under this norm_key, and callers may add label_title to commercial learning.
    """
    nk = (norm_key or "").strip()
    if len(nk) < 2:
        return
    label = (label_title or "").strip() or nk
    ts = datetime.now(timezone.utc).isoformat()
    init_schema()
    _ensure_marked_ad_table()
    with _lock:
        conn = sqlite3.connect(_db_path())
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO song_marked_ad_norm (norm_key, label_title, marked_at)
                VALUES (?, ?, ?)
                """,
                (nk, label, ts),
            )
            conn.execute("DELETE FROM song_plays WHERE norm_key = ?", (nk,))
            conn.commit()
        finally:
            conn.close()
