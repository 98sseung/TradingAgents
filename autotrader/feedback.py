"""Score finished analyses once against subsequent live hourly candles."""

import json
import logging
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


def score(conn, client, now) -> int:
    written = 0
    jobs = conn.execute("SELECT * FROM jobs WHERE status = 'done' AND rating IS NOT NULL "
                        "AND finished_at <= ?", (now - 24 * 3600,)).fetchall()
    for job in jobs:
        try:
            pending = [h for h in HORIZONS if now >= job["finished_at"] + h * 3600
                       and not conn.execute("SELECT 1 FROM outcomes WHERE job_id = ? AND horizon_h = ?",
                                            (job["id"], h)).fetchone()]
            if not pending:
                continue
            symbol = trading.symbol_for(job["ticker"])
            start = int(job["finished_at"] // 3600) * HOUR_MS
            bars = client._request("GET", "/fapi/v1/klines", {
                "symbol": symbol, "interval": "1h", "startTime": start,
                "endTime": start + max(pending) * HOUR_MS - 1, "limit": max(pending),
            }, base_url=binance._BASE_URLS["live"])
            entry = D(next(b[1] for b in bars if b[0] == start))
            for horizon in pending:
                end = start + horizon * HOUR_MS - 1
                exit_price = D(next(b[4] for b in bars if b[6] == end))
                if not entry.is_finite() or entry <= 0 or not exit_price.is_finite() or exit_price <= 0:
                    raise ValueError("Invalid candle price")
                ret = (exit_price / entry - 1) * 100
                mech = (_mechanical(json.loads(job["shock"]), bars, entry, exit_price)
                        if job["source"] == "shock" and job["shock"] and horizon == 120 else None)
                written += conn.execute(
                    "INSERT INTO outcomes (job_id, horizon_h, symbol, rating, entry, exit, ret_pct, verdict, scored_at, "
                    "mech_atr) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(job_id, horizon_h) DO NOTHING",
                    (job["id"], horizon, symbol, job["rating"], str(entry), str(exit_price),
                     float(ret), verdict(job["rating"], ret), now, mech),
                ).rowcount
        except Exception as exc:
            log.warning("Job %s: scoring failed (%s)", job["id"], type(exc).__name__)
    return written
