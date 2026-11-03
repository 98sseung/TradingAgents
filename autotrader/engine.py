"""Master scheduler. Run beside the analysis worker and the independent Risk Guard."""

import os
import time
from datetime import UTC, datetime
from decimal import Decimal as D

from autotrader import db, scanner, trading
from autotrader.binance import BinanceClient

INTERVAL, EVENT_INTERVAL, REANALYSIS_AGE, GUARD_AGE = 30, 300, 6 * 3600, 30
DAILY_BUDGET = int(os.environ.get("TRADING_BOT_DAILY_BUDGET", "12"))
MAX_ATTEMPTS = 3
ANALYSTS = ["market", "social", "news"]


def _event(conn, level, message):
    conn.execute("INSERT INTO events (at, level, source, message) VALUES (?, ?, 'engine', ?)",
                 (time.time(), level, message))


def _enqueue(conn, symbol, today, midnight, *, daily=False):
    ticker = symbol.removesuffix("USDT") + "-USD"
    if daily and conn.execute("SELECT 1 FROM jobs WHERE source = 'engine' AND ticker = ? "
                              "AND created_at >= ? AND created_at < ?",
                              (ticker, midnight, midnight + 86400)).fetchone():
        return
    queued = conn.execute("SELECT 1 FROM jobs WHERE source = 'engine' AND ticker = ? "
                          "AND status IN ('queued', 'running')", (ticker,)).fetchone()
    count = conn.execute("SELECT COUNT(*) FROM jobs WHERE source = 'engine' AND created_at >= ? "
                         "AND created_at < ?", (midnight, midnight + 86400)).fetchone()[0]
    if not queued and count >= DAILY_BUDGET:
        if trading._get(conn, "engine_budget_date") != today:
            _event(conn, "warn", "Daily analysis budget exhausted")
            trading._set(conn, "engine_budget_date", today)
    elif not queued:
        db.enqueue(conn, ticker, today, ANALYSTS, source="engine")


def _jobs(conn, client, now):
    for job in conn.execute("SELECT * FROM jobs j WHERE source = 'engine' AND status = 'done' "
                            "AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.job_id = j.id) ORDER BY id").fetchall():
        try:
            db.beat(conn, "engine", f"deciding job {job['id']}")
            trading.decide(conn, client, job)
        except Exception as exc:
            _event(conn, "error", f"Job {job['id']}: decision failed ({type(exc).__name__})")
    rows = conn.execute("SELECT d.* FROM decisions d JOIN jobs j ON j.id = d.job_id "
                        "WHERE j.source = 'engine' AND j.status = 'done' AND d.executed_at IS NULL "
                        "ORDER BY d.id").fetchall()
    for decision in rows:
        key, action = decision["id"], decision["action"]
        try:
            if action in ("open_long", "open_short"):
                recovering = trading._get(conn, f"tb{key}in") is not None
                heartbeat = db.heartbeat(conn, "guard")
                age = max(now, time.time()) - heartbeat["at"] if heartbeat else GUARD_AGE
                if not recovering and not 0 <= age < GUARD_AGE:
                    message = f"Decision {key}: entry waiting for a fresh Risk Guard heartbeat"
                    if not conn.execute("SELECT 1 FROM events WHERE source = 'engine' AND message = ?",
                                        (message,)).fetchone():
                        _event(conn, "warn", message)
                    continue
            if action in ("open_long", "open_short", "close"):
                db.beat(conn, "engine", f"executing decision {key}")
                trading.execute(conn, client, key)
            conn.execute("UPDATE decisions SET executed_at = ? WHERE id = ?", (time.time(), key))
        except Exception as exc:
            attempts = decision["attempts"] + 1
            conn.execute("UPDATE decisions SET attempts = ?, executed_at = ? WHERE id = ?",
                         (attempts, time.time() if attempts >= MAX_ATTEMPTS else None, key))
            if attempts == 1:
                _event(conn, "error", f"Decision {key}: execution failed ({type(exc).__name__})")
            if attempts >= MAX_ATTEMPTS:
                _event(conn, "warn", f"Decision {key}: gave up after {MAX_ATTEMPTS} failed attempts")


def step(conn, client, now):
    db.beat(conn, "engine", "scanning and processing jobs")
    date = datetime.fromtimestamp(now, UTC)
    today, midnight = date.date().isoformat(), date.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    symbols = [t["symbol"] for t in conn.execute("SELECT symbol FROM trades WHERE status = 'open'")]
    if now >= midnight + 300 and trading._get(conn, "engine_scan_date") != today:
        scanned = True
        try:
            selected = scanner.candidates(client)
            _event(conn, "info", "Daily scan: " + (", ".join(selected) or "none"))
        except Exception as exc:
            selected, scanned = [], False
            _event(conn, "error", f"Daily scan failed ({type(exc).__name__})")
        for symbol in dict.fromkeys(symbols + selected):
            try:
                _enqueue(conn, symbol, today, midnight, daily=True)
            except Exception as exc:
                scanned = False
                _event(conn, "error", f"{symbol}: enqueue failed ({type(exc).__name__})")
        if scanned:
            trading._set(conn, "engine_scan_date", today)
    if now - float(trading._get(conn, "engine_event_at", "-inf")) >= EVENT_INTERVAL:
        trading._set(conn, "engine_event_at", now)
        for symbol in symbols:
            try:
                last = conn.execute("SELECT MAX(created_at) FROM jobs WHERE source = 'engine' AND ticker = ?",
                                    (symbol.removesuffix("USDT") + "-USD",)).fetchone()[0]
                mark = conn.execute("SELECT mark_price FROM decisions WHERE symbol = ? AND mark_price IS NOT NULL "
                                    "ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
                if last is not None and now - last > REANALYSIS_AGE and mark:
                    atr = trading._indicators(client, symbol)[1]
                    if abs(D(client.mark_price(symbol)["markPrice"]) - D(mark[0])) >= 2 * atr:
                        _enqueue(conn, symbol, today, midnight)
            except Exception as exc:
                _event(conn, "error", f"{symbol}: event analysis failed ({type(exc).__name__})")
    _jobs(conn, client, now)
    db.beat(conn, "engine", "idle")


def main():
    conn, client = db.connect(), BinanceClient()
    while True:
        try:
            step(conn, client, time.time())
        except Exception as exc:
            _event(conn, "error", f"Engine step failed ({type(exc).__name__})")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
