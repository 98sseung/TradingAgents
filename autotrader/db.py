"""The bot's SQLite store: analysis jobs the GUI queues and the worker runs.

The GUI and the worker are separate processes; this file is the only thing they share.
"""

import os
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("TRADING_BOT_DB") or Path.home() / ".trading-bot" / "bot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    analysts TEXT NOT NULL,                 -- comma-separated analyst keys
    status TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    board TEXT,                             -- JSON list of [agent name, text so far]
    rating TEXT,
    report_dir TEXT,
    error TEXT,
    llm_calls INTEGER,
    tool_calls INTEGER,
    tokens_in INTEGER,
    tokens_out INTEGER
);
CREATE TABLE IF NOT EXISTS heartbeat (name TEXT PRIMARY KEY, at REAL NOT NULL, note TEXT);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # the GUI reads while the worker writes
    conn.executescript(SCHEMA)
    return conn


def enqueue(conn, ticker: str, trade_date: str, analysts: list[str]) -> int:
    cur = conn.execute(
        "INSERT INTO jobs (ticker, trade_date, analysts, created_at) VALUES (?, ?, ?, ?)",
        (ticker, trade_date, ",".join(analysts), time.time()),
    )
    return cur.lastrowid


def claim_next(conn) -> sqlite3.Row | None:
    """Mark the oldest queued job running and return it, in one statement so two workers never share one."""
    return conn.execute(
        "UPDATE jobs SET status = 'running', started_at = ? "
        "WHERE id = (SELECT id FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1) RETURNING *",
        (time.time(),),
    ).fetchone()


def update(conn, job_id: int, **fields) -> None:
    columns = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(f"UPDATE jobs SET {columns} WHERE id = ?", (*fields.values(), job_id))


def fail_orphans(conn) -> int:
    """Jobs left 'running' by a worker that died can never finish; say so instead of showing them forever."""
    return conn.execute(
        "UPDATE jobs SET status = 'failed', error = 'worker stopped mid-run', finished_at = ? "
        "WHERE status = 'running'",
        (time.time(),),
    ).rowcount


def beat(conn, name: str, note: str = "") -> None:
    conn.execute(
        "INSERT INTO heartbeat (name, at, note) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET at = excluded.at, note = excluded.note",
        (name, time.time(), note),
    )


def heartbeat(conn, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM heartbeat WHERE name = ?", (name,)).fetchone()


def recent_jobs(conn, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def get_job(conn, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def usage_since(conn, since: float) -> sqlite3.Row:
    """Plan-usage totals for jobs created since ``since`` (epoch seconds): what phase 0 measures."""
    return conn.execute(
        "SELECT COUNT(*) AS jobs,"
        " SUM(status = 'done') AS done, SUM(status = 'failed') AS failed,"
        " SUM(status IN ('queued', 'running')) AS pending,"
        " COALESCE(SUM(llm_calls), 0) AS llm_calls,"
        " COALESCE(SUM(tokens_in), 0) AS tokens_in, COALESCE(SUM(tokens_out), 0) AS tokens_out,"
        " AVG(CASE WHEN status = 'done' THEN finished_at - started_at END) AS avg_seconds"
        " FROM jobs WHERE created_at >= ?",
        (since,),
    ).fetchone()
