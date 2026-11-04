"""Score finished analyses once against subsequent live hourly candles."""

import logging
import math
from decimal import Decimal as D

from autotrader import binance, trading

log = logging.getLogger(__name__)
HORIZONS = (24, 120)
HOUR_MS = 3_600_000


def verdict(rating, ret_pct):
    expect = {"Buy": 1, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -1}[rating]
    if expect == 0:
        return "correct" if abs(ret_pct) < 2 else "wrong"
    if abs(ret_pct) < 1:
        return "inconclusive"
    return "correct" if expect * ret_pct > 0 else "wrong"


def _mechanical(facts, bars, entry, exit_price):
    direction, atr = D(facts["direction"]), D(str(facts["atr"]))
    stop, target = entry - direction * D("1.5") * atr, entry + direction * 3 * atr
    for bar in bars:
        high, low = D(bar[2]), D(bar[3])
        if (low <= stop if direction > 0 else high >= stop):
            exit_price = stop
            break
        if (high >= target if direction > 0 else low <= target):
            exit_price = target
            break
    return float((direction * (exit_price - entry) - entry * D("0.001")) / atr)


def _start(at):
    return math.ceil(at / 3600) * HOUR_MS


def _window(client, symbol, at, horizon, now):
    start, end = _start(at), _start(at) + horizon * HOUR_MS
    if now * 1000 < end:
        return None
    bars = client._request("GET", "/fapi/v1/klines", {
        "symbol": symbol, "interval": "1h", "startTime": start,
        "endTime": end - 1, "limit": horizon,
    }, base_url=binance._BASE_URLS["live"])
    if ([b[0] for b in bars] != list(range(start, end, HOUR_MS))
            or any(b[6] != b[0] + HOUR_MS - 1 for b in bars)):
        raise ValueError("Incomplete hourly candles")
    entry, exit_price = D(bars[0][1]), D(bars[-1][4])
    if not entry.is_finite() or entry <= 0 or not exit_price.is_finite() or exit_price <= 0:
        raise ValueError("Invalid candle price")
    return bars, entry, exit_price


def _score_candidates(conn, client, now):
    written = 0
    rows = conn.execute("SELECT c.*, j.finished_at FROM candidates c LEFT JOIN jobs j ON j.id = c.job_id "
                        "WHERE c.scored_at IS NULL AND c.at <= ?", (now - 120 * 3600,)).fetchall()
    for row in rows:
        try:
            # Both entries use a complete 120h window, even when the analysis took hours.
            if row["finished_at"] is not None and now * 1000 < _start(row["finished_at"]) + 120 * HOUR_MS:
                continue
            immediate = _window(client, row["symbol"], row["at"], 120, now)
            if immediate is None:
                continue
            delayed = (_window(client, row["symbol"], row["finished_at"], 120, now)
                       if row["finished_at"] is not None else None)
            mech_now = _mechanical(row, *immediate)
            mech_delay = _mechanical(row, *delayed) if delayed is not None else None
            written += conn.execute("UPDATE candidates SET mech_now = ?, mech_delay = ?, scored_at = ? "
                                    "WHERE id = ? AND scored_at IS NULL",
                                    (mech_now, mech_delay, now, row["id"])).rowcount
        except Exception as exc:
            log.warning("Candidate %s: scoring failed (%s)", row["id"], type(exc).__name__)
    return written


def score(conn, client, now) -> int:
    written = 0
    jobs = conn.execute("SELECT * FROM jobs WHERE status = 'done' AND rating IS NOT NULL "
                        "AND finished_at <= ?", (now - 24 * 3600,)).fetchall()
    for job in jobs:
        try:
            pending = [h for h in HORIZONS if now * 1000 >= _start(job["finished_at"]) + h * HOUR_MS
                       and not conn.execute("SELECT 1 FROM outcomes WHERE job_id = ? AND horizon_h = ?",
                                            (job["id"], h)).fetchone()]
            if not pending:
                continue
            symbol = trading.symbol_for(job["ticker"])
            bars, entry, _ = _window(client, symbol, job["finished_at"], max(pending), now)
            for horizon in pending:
                exit_price = D(bars[horizon - 1][4])
                if not exit_price.is_finite() or exit_price <= 0:
                    raise ValueError("Invalid candle price")
                ret = (exit_price / entry - 1) * 100
                written += conn.execute(
                    "INSERT INTO outcomes (job_id, horizon_h, symbol, rating, entry, exit, ret_pct, verdict, scored_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(job_id, horizon_h) DO NOTHING",
                    (job["id"], horizon, symbol, job["rating"], str(entry), str(exit_price),
                     float(ret), verdict(job["rating"], ret), now),
                ).rowcount
        except Exception as exc:
            log.warning("Job %s: scoring failed (%s)", job["id"], type(exc).__name__)
    return written + _score_candidates(conn, client, now)
