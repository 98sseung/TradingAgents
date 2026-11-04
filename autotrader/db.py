"""The bot's SQLite store: analysis jobs the GUI queues and the worker runs.

The GUI and the worker are separate processes; this file is the only thing they share.
"""

import os
import sqlite3
import time
from pathlib import Path

from tradingagents.dataflows.symbols import normalize_symbol

DB_PATH = Path(os.environ.get("TRADING_BOT_DB") or Path.home() / ".trading-bot" / "bot.db")
WORKER_TIMEOUT = 60

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
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY, at REAL, symbol TEXT, kind TEXT CHECK (kind IN ('daily', 'shock')),
    direction INTEGER CHECK (direction IN (1, -1)), atr REAL, price REAL,
    job_id INTEGER, skip_reason TEXT, mech_now REAL, mech_delay REAL, scored_at REAL
);
CREATE TABLE IF NOT EXISTS outcomes (
    job_id INTEGER, horizon_h INTEGER, symbol TEXT, rating TEXT, entry TEXT, exit TEXT,
    ret_pct REAL, verdict TEXT, scored_at REAL, PRIMARY KEY (job_id, horizon_h)
);
"""
MAX_ATTEMPTS = 2  # a job that takes the worker down twice is failed, not retried forever
ADDED_COLUMNS = {
    "jobs": {
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "stop_loss": "REAL",     # the Trader's stop, when it gave one
        "price_target": "REAL",  # the Portfolio Manager's target, when it gave one
        "source": "TEXT NOT NULL DEFAULT 'manual'",  # engine/shock jobs may trade; manual ones never do
        "shock": "TEXT",         # frozen detection facts, JSON
        "worker": "TEXT",
    },
    "decisions": {"executed_at": "REAL", "attempts": "INTEGER NOT NULL DEFAULT 0"},
    "outcomes": {"mech_atr": "REAL"},
}


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # the GUI reads while the worker writes
    conn.executescript(SCHEMA)
    with conn:
        conn.execute("BEGIN IMMEDIATE")  # workers may start against the same older database
        for table, columns in ADDED_COLUMNS.items():
            have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, decl in columns.items():  # a db made by an earlier version gains them in place
                if column not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return conn


def enqueue(conn, ticker: str, trade_date: str, analysts: list[str], source: str = "manual", shock=None) -> int:
    """Queue an analysis. Engine/shock jobs may trade; GUI ones are for reading."""
    cur = conn.execute(
        "INSERT INTO jobs (ticker, trade_date, analysts, created_at, source, shock) VALUES (?, ?, ?, ?, ?, ?)",
        (ticker, trade_date, ",".join(analysts), time.time(), source, shock),
    )
    return cur.lastrowid


def worker_names(environ=None) -> list[str]:
    environ = os.environ if environ is None else environ
    try:
        count = int(environ.get("TRADING_BOT_WORKERS", "1"))
        if count < 1:
            raise ValueError
    except ValueError:
        raise ValueError("TRADING_BOT_WORKERS must be an integer >= 1") from None
    return [f"worker-{n}" for n in range(1, count + 1)]


def instrument(ticker: str) -> str:
    ticker = normalize_symbol(ticker)
    for suffix in ("-USDT", "USDT", "-USDC", "USDC"):
        if ticker.endswith(suffix):
            return ticker[:-len(suffix)] + "-USD"
    return ticker


def claim_next(conn, worker: str) -> sqlite3.Row | None:
    """Claim the oldest eligible job, excluding aliases of any running ticker."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        running = {instrument(row["ticker"]) for row in conn.execute(
            "SELECT ticker FROM jobs WHERE status = 'running'")}
        job = next((row for row in conn.execute(
            "SELECT id, ticker FROM jobs WHERE status = 'queued' ORDER BY created_at, id")
            if instrument(row["ticker"]) not in running), None)
        if job is None:
            return None
        return conn.execute(
            "UPDATE jobs SET status = 'running', started_at = ?, attempts = attempts + 1, worker = ? "
            "WHERE id = ? RETURNING *",
            (time.time(), worker, job["id"]),
        ).fetchone()


def update(conn, job_id: int, *, owner: str | None = None, **fields) -> int:
    columns = ", ".join(f"{name} = ?" for name in fields)
    where = "id = ?" + (" AND worker = ? AND status = 'running'" if owner else "")
    params = (*fields.values(), job_id, owner) if owner else (*fields.values(), job_id)
    return conn.execute(f"UPDATE jobs SET {columns} WHERE {where}", params).rowcount


def requeue_orphans(conn, worker: str) -> int:
    """Recover this worker's old jobs and jobs without a live owner."""
    now = time.time()
    eligible = ("status = 'running' AND (worker = ? OR NOT EXISTS "
                "(SELECT 1 FROM heartbeat h WHERE h.name = COALESCE(jobs.worker, 'worker') AND h.at >= ?))")
    params = (worker, now - WORKER_TIMEOUT)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE jobs SET status = 'failed', error = 'worker stopped mid-run twice', finished_at = ? "
            f"WHERE attempts >= ? AND {eligible}",
            (now, MAX_ATTEMPTS, *params),
        )
        return conn.execute(
            f"UPDATE jobs SET status = 'queued', worker = NULL, started_at = NULL WHERE {eligible}", params,
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
