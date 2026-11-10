"""SQLite read-ledger: every message ever read, when, and what was decided.

The ledger is the source of truth for "have I read this?". A message is in
it exactly once (primary key = Gmail message id) with the time it was read.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    msg_id        TEXT PRIMARY KEY,
    thread_id     TEXT NOT NULL,
    internal_ts   INTEGER NOT NULL,
    folder        TEXT NOT NULL,
    from_name     TEXT,
    from_email    TEXT,
    subject       TEXT,
    from_me       INTEGER NOT NULL DEFAULT 0,
    is_reply      INTEGER NOT NULL DEFAULT 0,
    category      TEXT NOT NULL,
    needs_action  INTEGER NOT NULL DEFAULT 0,
    action        TEXT,
    deadline      TEXT,
    company       TEXT,
    summary       TEXT,
    classifier    TEXT,              -- rule:<name> | llm:<backend> | private | fallback
    meeting_links TEXT,              -- JSON list
    events        TEXT,              -- JSON list of calendar events
    excerpt       TEXT,              -- first part of the new text, local only
    truncated     INTEGER NOT NULL DEFAULT 0,
    handled_at    TEXT,              -- set by `sweep.py done`
    handled_note  TEXT,
    read_at       TEXT NOT NULL,     -- UTC ISO timestamp of when the sweep read it
    run_id        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, internal_ts);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(internal_ts);

CREATE TABLE IF NOT EXISTS runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    window_from  INTEGER NOT NULL,   -- epoch seconds
    window_to    INTEGER NOT NULL,
    listed       INTEGER,
    already_read INTEGER,
    newly_read   INTEGER,
    failed       INTEGER,
    status       TEXT                -- ok | incomplete
);

CREATE TABLE IF NOT EXISTS failures (
    msg_id     TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_error TEXT,
    attempts   INTEGER NOT NULL DEFAULT 1
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # --- runs -----------------------------------------------------------
    def start_run(self, window_from: int, window_to: int) -> int:
        cur = self.db.execute(
            "INSERT INTO runs(started_at, window_from, window_to) VALUES (?,?,?)",
            (utc_now(), window_from, window_to),
        )
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, listed: int, already: int, new: int, failed: int) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at=?, listed=?, already_read=?, newly_read=?, "
            "failed=?, status=? WHERE run_id=?",
            (utc_now(), listed, already, new, failed, "ok" if failed == 0 else "incomplete", run_id),
        )
        self.db.commit()

    def last_ok_window_to(self) -> int | None:
        row = self.db.execute(
            "SELECT window_to FROM runs WHERE status='ok' ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        return row["window_to"] if row else None

    def recent_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,)
        ).fetchall()

    # --- messages -------------------------------------------------------
    def known_ids(self, ids: list[str]) -> set[str]:
        known: set[str] = set()
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            rows = self.db.execute(
                f"SELECT msg_id FROM messages WHERE msg_id IN ({marks})", chunk
            ).fetchall()
            known.update(r["msg_id"] for r in rows)
        return known

    def record(self, row: dict) -> None:
        row = dict(row)
        for key in ("meeting_links", "events"):
            row[key] = json.dumps(row.get(key) or [], ensure_ascii=False)
        row.setdefault("read_at", utc_now())
        cols = ",".join(row)
        marks = ",".join("?" * len(row))
        self.db.execute(f"INSERT OR REPLACE INTO messages({cols}) VALUES ({marks})", list(row.values()))
        self.db.execute("DELETE FROM failures WHERE msg_id=?", (row["msg_id"],))
        self.db.commit()

    def record_failure(self, msg_id: str, error: str) -> None:
        self.db.execute(
            "INSERT INTO failures(msg_id, first_seen, last_error) VALUES (?,?,?) "
            "ON CONFLICT(msg_id) DO UPDATE SET last_error=excluded.last_error, attempts=attempts+1",
            (msg_id, utc_now(), error[:500]),
        )
        self.db.commit()

    def pending_failures(self) -> list[str]:
        return [r["msg_id"] for r in self.db.execute("SELECT msg_id FROM failures")]

    def get(self, msg_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM messages WHERE msg_id=?", (msg_id,)).fetchone()

    def unclassified(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM messages WHERE category='unclassified' ORDER BY internal_ts"
        ).fetchall()

    def apply_verdict(self, msg_id: str, fields: dict) -> bool:
        allowed = ("category", "needs_action", "action", "deadline", "company", "summary", "classifier")
        sets = ", ".join(f"{k}=?" for k in allowed if k in fields)
        values = [fields[k] for k in allowed if k in fields]
        cur = self.db.execute(f"UPDATE messages SET {sets} WHERE msg_id=?", values + [msg_id])
        self.db.commit()
        return cur.rowcount > 0

    def mark_handled(self, msg_id: str, note: str = "") -> bool:
        cur = self.db.execute(
            "UPDATE messages SET handled_at=?, handled_note=? WHERE msg_id=?",
            (utc_now(), note, msg_id),
        )
        self.db.commit()
        return cur.rowcount > 0

    # --- queries used by the report -------------------------------------
    def open_items(self) -> list[sqlite3.Row]:
        """Incoming messages that need action and have not been answered.

        "Answered" is decided by the thread, not by guesswork: an item is
        closed when a later message from me exists in the same thread, or
        when it was marked handled by hand.
        """
        return self.db.execute(
            """
            SELECT m.* FROM messages m
            WHERE m.needs_action = 1 AND m.from_me = 0 AND m.handled_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM messages later
                  WHERE later.thread_id = m.thread_id AND later.from_me = 1
                    AND later.internal_ts > m.internal_ts)
              AND NOT EXISTS (
                  SELECT 1 FROM messages newer
                  WHERE newer.thread_id = m.thread_id AND newer.from_me = 0
                    AND newer.needs_action = 1 AND newer.internal_ts > m.internal_ts)
            ORDER BY m.internal_ts DESC
            """
        ).fetchall()

    def awaiting_them(self, older_than_ts: int, newer_than_ts: int) -> list[sqlite3.Row]:
        """Threads where my message is the last word and nobody has replied."""
        return self.db.execute(
            """
            SELECT m.* FROM messages m
            WHERE m.from_me = 1 AND m.internal_ts BETWEEN ? AND ?
              AND m.internal_ts = (SELECT MAX(internal_ts) FROM messages t
                                   WHERE t.thread_id = m.thread_id)
              AND EXISTS (SELECT 1 FROM messages t WHERE t.thread_id = m.thread_id AND t.from_me = 0)
            ORDER BY m.internal_ts DESC
            """,
            (newer_than_ts, older_than_ts),
        ).fetchall()

    def with_events(self, since_ts: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM messages WHERE events != '[]' AND internal_ts >= ? ORDER BY internal_ts",
            (since_ts,),
        ).fetchall()

    def run_messages(self, run_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM messages WHERE run_id=? ORDER BY internal_ts", (run_id,)
        ).fetchall()

    def read_since(self, read_at_iso: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM messages WHERE read_at >= ? ORDER BY internal_ts", (read_at_iso,)
        ).fetchall()

    def count_in_window(self, ts_from: int, ts_to: int) -> int:
        return self.db.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE internal_ts BETWEEN ? AND ?", (ts_from, ts_to)
        ).fetchone()["n"]
