"""Master scheduler. Run beside the analysis worker and the independent Risk Guard."""

import json
import os
import time
from datetime import UTC, datetime
from decimal import Decimal as D

from autotrader import db, feedback, scanner, trading
from autotrader.binance import BinanceClient

INTERVAL, EVENT_INTERVAL, REANALYSIS_AGE, GUARD_AGE = 30, 300, 6 * 3600, 30
DAILY_BUDGET = int(os.environ.get("TRADING_BOT_DAILY_BUDGET", "12"))
MAX_ATTEMPTS = 3
ANALYSTS = ["market", "social", "news"]
_shock_ratios = {}


def _event(conn, level, message):
    conn.execute("INSERT INTO events (at, level, source, message) VALUES (?, ?, 'engine', ?)",
                 (time.time(), level, message))


def _budget_count(conn, midnight):
    return conn.execute("SELECT COUNT(*) FROM jobs WHERE source IN ('engine', 'shock') AND created_at >= ? "
                        "AND created_at < ?", (midnight, midnight + 86400)).fetchone()[0]


def _enqueue(conn, symbol, today, midnight, *, daily=False):
    ticker = symbol.removesuffix("USDT") + "-USD"
    if daily and conn.execute("SELECT 1 FROM jobs WHERE source = 'engine' AND ticker = ? "
                              "AND created_at >= ? AND created_at < ?",
                              (ticker, midnight, midnight + 86400)).fetchone():
        return
    queued = conn.execute("SELECT 1 FROM jobs WHERE source = 'engine' AND ticker = ? "
                          "AND status IN ('queued', 'running')", (ticker,)).fetchone()
    count = _budget_count(conn, midnight)
    if not queued and count >= DAILY_BUDGET:
        if trading._get(conn, "engine_budget_date") != today:
            _event(conn, "warn", "Daily analysis budget exhausted")
            trading._set(conn, "engine_budget_date", today)
    elif not queued:
        db.enqueue(conn, ticker, today, ANALYSTS, source="engine")


def _four_hour(client, symbol, now):
    bars = [b for b in client.klines(symbol, "5m", 60) if b[6] < now * 1000]
    latest = bars[-1]
    previous = next(b for b in bars if b[0] == latest[0] - 48 * 300_000)
    return D(latest[4]), D(previous[4])


def _shocks(conn, client, now, today, midnight):
    crossings = []
    for symbol in json.loads(trading._get(conn, "engine_universe", "[]")):
        try:
            price, previous = _four_hour(client, symbol, now)
            atr = trading._indicators(client, symbol)[1]
            ratio = (price - previous) / atr
            last = _shock_ratios.get(symbol)
            _shock_ratios[symbol] = ratio
            if last is not None and abs(last) < D("1.5") <= abs(ratio):
                crossings.append((abs(ratio), symbol, ratio, atr, price))
        except (IndexError, StopIteration):
            continue  # No completed pair exactly four hours apart yet.
        except Exception as exc:
            _event(conn, "error", f"{symbol}: shock check failed ({type(exc).__name__})")
    if not crossings:
        return
    _, symbol, ratio, atr, price = max(crossings)
    ticker = symbol.removesuffix("USDT") + "-USD"
    try:
        positions = {p["symbol"] for p in client.positions()}
        positions.update(t["symbol"] for t in conn.execute("SELECT symbol FROM trades WHERE status = 'open'"))
        gates = [
            (conn.execute("SELECT 1 FROM jobs WHERE source = 'shock' AND created_at >= ? AND created_at < ?",
                          (midnight, midnight + 86400)).fetchone(), "Shock job already created this UTC day"),
            (conn.execute("SELECT 1 FROM jobs WHERE source = 'shock' AND ticker = ? AND created_at >= ?",
                          (ticker, now - 86400)).fetchone(), "Symbol had a shock job in the last 24h"),
            (symbol in positions, "Symbol is held"),
            (now < float(trading._get(conn, "cooldown:" + symbol, 0)), "Stop cooldown"),
            (trading._shock_open(conn), "Shock-origin position open"),
            (len(positions) >= trading.MAX_POSITIONS, "Maximum open positions"),
            (_budget_count(conn, midnight) >= DAILY_BUDGET, "Daily analysis budget exhausted"),
        ]
        for blocked, reason in gates:
            if blocked:
                _event(conn, "info", f"{symbol}: shock skipped: {reason}")
                return
        daily = [b for b in client.klines(symbol, "1d", 100) if b[6] < now * 1000][-1]
        if not scanner._yahoo_matches(symbol, D(daily[4]), daily[6]):
            _event(conn, "info", f"{symbol}: shock skipped: Yahoo identity mismatch")
            return
        btc_price, btc_previous = _four_hour(client, "BTCUSDT", now)
        facts = {"direction": 1 if ratio > 0 else -1, "atr": float(atr), "ratio": float(ratio),
                 "detected_at": now, "price": float(price),
                 "btc_move_pct": float((btc_price / btc_previous - 1) * 100)}
        db.enqueue(conn, ticker, today, ANALYSTS, source="shock", shock=json.dumps(facts))
    except Exception as exc:
        _event(conn, "error", f"{symbol}: shock enqueue failed ({type(exc).__name__})")


def _jobs(conn, client, now):
    for job in conn.execute("SELECT * FROM jobs j WHERE source IN ('engine', 'shock') AND status = 'done' "
                            "AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.job_id = j.id) ORDER BY id").fetchall():
        try:
            db.beat(conn, "engine", f"deciding job {job['id']}")
            trading.decide(conn, client, job)
        except Exception as exc:
            _event(conn, "error", f"Job {job['id']}: decision failed ({type(exc).__name__})")
    rows = conn.execute("SELECT d.* FROM decisions d JOIN jobs j ON j.id = d.job_id "
                        "WHERE j.source IN ('engine', 'shock') AND j.status = 'done' AND d.executed_at IS NULL "
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
            selected, eligible = scanner.candidates(client)
            trading._set(conn, "engine_universe", json.dumps(eligible))
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
        _shocks(conn, client, now, today, midnight)
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
    if now - float(trading._get(conn, "engine_feedback_at", "-inf")) >= 3600:
        trading._set(conn, "engine_feedback_at", now)
        try:
            feedback.score(conn, client, now)
        except Exception as exc:
            _event(conn, "error", f"Analysis scoring failed ({type(exc).__name__})")
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
