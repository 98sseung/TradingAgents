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
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY, job_id INTEGER, symbol TEXT NOT NULL, rating TEXT,
    action TEXT NOT NULL, reason TEXT NOT NULL, mark_price TEXT, qty TEXT, stop TEXT,
    target TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY, decision_id INTEGER UNIQUE, symbol TEXT NOT NULL, side TEXT NOT NULL,
    qty TEXT NOT NULL, entry_price TEXT NOT NULL, stop TEXT, target TEXT, opened_at REAL NOT NULL,
    closed_at REAL, exit_price TEXT, exit_reason TEXT, realized_pnl TEXT, fees TEXT, funding TEXT,
    status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS control (
    key TEXT PRIMARY KEY, value TEXT, updated_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, at REAL NOT NULL, level TEXT NOT NULL, source TEXT NOT NULL,
    message TEXT NOT NULL
);
"""
MAX_ATTEMPTS = 2  # a job that takes the worker down twice is failed, not retried forever
ADDED_COLUMNS = {
    "jobs": {
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "stop_loss": "REAL",     # the Trader's stop, when it gave one
        "price_target": "REAL",  # the Portfolio Manager's target, when it gave one
        "source": "TEXT NOT NULL DEFAULT 'manual'",  # 'engine' jobs may trade; 'manual' ones never do
    },
    "decisions": {"executed_at": "REAL", "attempts": "INTEGER NOT NULL DEFAULT 0"},
}


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # the GUI reads while the worker writes
    conn.executescript(SCHEMA)
    for table, columns in ADDED_COLUMNS.items():
        have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, decl in columns.items():  # a db made by an earlier version gains them in place
            if column not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return conn


def enqueue(conn, ticker: str, trade_date: str, analysts: list[str], source: str = "manual") -> int:
    """Queue an analysis. Only ``source='engine'`` jobs can lead to trades; GUI ones are for reading."""
    cur = conn.execute(
        "INSERT INTO jobs (ticker, trade_date, analysts, created_at, source) VALUES (?, ?, ?, ?, ?)",
        (ticker, trade_date, ",".join(analysts), time.time(), source),
    )
    return cur.lastrowid


def claim_next(conn) -> sqlite3.Row | None:
    """Mark the oldest queued job running and return it, in one statement so two workers never share one."""
    return conn.execute(
        "UPDATE jobs SET status = 'running', started_at = ?, attempts = attempts + 1 "
        "WHERE id = (SELECT id FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1) RETURNING *",
        (time.time(),),
    ).fetchone()


def update(conn, job_id: int, **fields) -> None:
    columns = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(f"UPDATE jobs SET {columns} WHERE id = ?", (*fields.values(), job_id))


def requeue_orphans(conn) -> int:
    """Jobs a dead worker left 'running': queue them again to resume, or fail them after MAX_ATTEMPTS."""
    conn.execute(
        "UPDATE jobs SET status = 'failed', error = 'worker stopped mid-run twice', finished_at = ? "
        "WHERE status = 'running' AND attempts >= ?",
        (time.time(), MAX_ATTEMPTS),
    )
    return conn.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'").rowcount


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
