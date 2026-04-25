"""Searchable Tkinter UI for the transcripts SQLite database."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import config

DEFAULT_DB = config.DATA_DIR / "transcripts.sqlite3"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transcripts'"
    ).fetchone()
    return row is not None


class TranscriptBrowserApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Radio Monitor — Transcript search")
        self.root.minsize(880, 520)
        self.root.geometry("960x600")

        self._db_path = tk.StringVar(value=str(DEFAULT_DB))

        top = ttk.Frame(self.root, padding=8)
        top.pack(fill=tk.X)

        ttk.Label(top, text="Database:").pack(side=tk.LEFT)
        self.path_entry = ttk.Entry(top, textvariable=self._db_path, width=72)
        self.path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(top, text="Browse…", command=self._browse_db).pack(side=tk.LEFT)

        row2 = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        row2.pack(fill=tk.X)

        ttk.Label(row2, text="Station:").pack(side=tk.LEFT)
        self._station = tk.StringVar(value="(all)")
        self.station_combo = ttk.Combobox(
            row2, textvariable=self._station, width=22, state="readonly"
        )
        self.station_combo.pack(side=tk.LEFT, padx=(4, 16))

        ttk.Label(row2, text="Contains:").pack(side=tk.LEFT)
        self._needle = tk.StringVar()
        self.needle_entry = ttk.Entry(row2, textvariable=self._needle, width=36)
        self.needle_entry.pack(side=tk.LEFT, padx=(4, 12))
        self.needle_entry.bind("<Return>", lambda _e: self._search())

        self._music_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="Music chunks only", variable=self._music_only).pack(
            side=tk.LEFT, padx=(0, 12)
        )

        ttk.Label(row2, text="Max rows:").pack(side=tk.LEFT)
        self._limit = tk.StringVar(value="500")
        ttk.Spinbox(row2, from_=50, to=5000, increment=50, width=6, textvariable=self._limit).pack(
            side=tk.LEFT, padx=(4, 12)
        )

        ttk.Button(row2, text="Search", command=self._search).pack(side=tk.LEFT)
        ttk.Button(row2, text="Reload stations", command=self._refresh_stations).pack(
            side=tk.LEFT, padx=(8, 0)
        )

        row3 = ttk.Frame(self.root, padding=(8, 0, 8, 4))
        row3.pack(fill=tk.X)
        ttk.Label(row3, text="From date (UTC, optional YYYY-MM-DD):").pack(side=tk.LEFT)
        self._since = tk.StringVar()
        ttk.Entry(row3, textvariable=self._since, width=14).pack(side=tk.LEFT, padx=4)
        ttk.Label(row3, text="To (optional):").pack(side=tk.LEFT, padx=(12, 0))
        self._until = tk.StringVar()
        ttk.Entry(row3, textvariable=self._until, width=14).pack(side=tk.LEFT, padx=4)

        mid = ttk.Frame(self.root, padding=(8, 4))
        mid.pack(fill=tk.BOTH, expand=True)

        cols = ("received_at", "station", "music", "preview")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=18)
        self.tree.heading("received_at", text="Received (UTC)")
        self.tree.heading("station", text="Station")
        self.tree.heading("music", text="Music")
        self.tree.heading("preview", text="Text (preview)")
        self.tree.column("received_at", width=160, stretch=False)
        self.tree.column("station", width=120, stretch=False)
        self.tree.column("music", width=50, stretch=False)
        self.tree.column("preview", width=560, stretch=True)

        sy = ttk.Scrollbar(mid, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sy.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sy.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<Double-1>", self._on_double_click)

        bot = ttk.Frame(self.root, padding=8)
        bot.pack(fill=tk.X)
        self._status = tk.StringVar(value="Open a database and press Search.")
        ttk.Label(bot, textvariable=self._status).pack(side=tk.LEFT)

        self._rows_by_item: dict[str, sqlite3.Row] = {}
        self._refresh_stations()

    def _browse_db(self) -> None:
        p = filedialog.askopenfilename(
            title="Transcripts database",
            filetypes=[("SQLite", "*.sqlite3 *.db *.sqlite"), ("All", "*")],
            initialdir=str(Path(self._db_path.get()).parent),
        )
        if p:
            self._db_path.set(p)
            self._refresh_stations()

    def _refresh_stations(self) -> None:
        path = Path(self._db_path.get().strip())
        names = ["(all)"]
        if path.is_file():
            try:
                conn = _connect(path)
                try:
                    if _table_exists(conn):
                        for (n,) in conn.execute(
                            "SELECT DISTINCT station_name FROM transcripts ORDER BY station_name"
                        ):
                            if n:
                                names.append(n)
                finally:
                    conn.close()
            except sqlite3.Error as e:
                messagebox.showerror("Database", str(e))
        self.station_combo["values"] = names
        if self._station.get() not in names:
            self._station.set("(all)")

    def _parse_limit(self) -> int:
        try:
            n = int(self._limit.get().strip())
            return max(10, min(10_000, n))
        except ValueError:
            return 500

    def _search(self) -> None:
        path = Path(self._db_path.get().strip())
        if not path.is_file():
            messagebox.showwarning("Database", f"File not found:\n{path}")
            return

        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self._rows_by_item.clear()

        conditions: list[str] = []
        params: list[object] = []

        st = self._station.get().strip()
        if st and st != "(all)":
            conditions.append("station_name = ?")
            params.append(st)

        needle = self._needle.get().strip()
        if needle:
            conditions.append("LOWER(text) LIKE ?")
            params.append(f"%{needle.lower()}%")

        if self._music_only.get():
            conditions.append("is_music = 1")

        since = self._since.get().strip()
        until = self._until.get().strip()
        if since:
            conditions.append("date(received_at) >= date(?)")
            params.append(since)
        if until:
            conditions.append("date(received_at) <= date(?)")
            params.append(until)

        limit = self._parse_limit()
        sql = (
            "SELECT id, received_at, station_name, text, is_music, icy_title "
            "FROM transcripts"
        )
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY received_at DESC LIMIT ?"
        params.append(limit)

        try:
            conn = _connect(path)
            try:
                if not _table_exists(conn):
                    messagebox.showinfo(
                        "Database",
                        "No `transcripts` table yet. Run the monitor and capture some speech first.",
                    )
                    return
                cur = conn.execute(sql, params)
                rows = cur.fetchall()
            finally:
                conn.close()
        except sqlite3.Error as e:
            messagebox.showerror("Query failed", str(e))
            return

        for row in rows:
            preview = (row["text"] or "").replace("\n", " ").replace("\r", " ")
            if len(preview) > 200:
                preview = preview[:197] + "…"
            music = "yes" if row["is_music"] else ""
            iid = str(row["id"])
            self.tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(
                    row["received_at"][:19] if row["received_at"] else "",
                    row["station_name"] or "",
                    music,
                    preview,
                ),
            )
            self._rows_by_item[iid] = row

        self._status.set(f"{len(rows)} row(s) (limit {limit}). Double-click for full text.")

    def _on_double_click(self, _evt: tk.Event) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        row = self._rows_by_item.get(iid)
        if row is None:
            return

        win = tk.Toplevel(self.root)
        win.title(f"Transcript #{row['id']} — {row['station_name']}")
        win.geometry("720x420")

        meta = ttk.Frame(win, padding=8)
        meta.pack(fill=tk.X)
        ttk.Label(meta, text=f"UTC: {row['received_at']}").pack(anchor=tk.W)
        icy = row["icy_title"] or ""
        if icy.strip():
            ttk.Label(meta, text=f"ICY: {icy}").pack(anchor=tk.W)

        body = ttk.Frame(win, padding=8)
        body.pack(fill=tk.BOTH, expand=True)
        txt = scrolledtext.ScrolledText(body, wrap=tk.WORD, height=16, font=("Segoe UI", 10))
        txt.pack(fill=tk.BOTH, expand=True)
        txt.insert("1.0", row["text"] or "")
        txt.configure(state=tk.DISABLED)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    app = TranscriptBrowserApp()
    if len(sys.argv) > 1:
        p = Path(sys.argv[1]).resolve()
        app._db_path.set(str(p))
        app._refresh_stations()
    app.run()


if __name__ == "__main__":
    main()
