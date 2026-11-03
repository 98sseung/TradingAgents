"""Independent Risk Guard. Run even when the engine, worker, or GUI is stopped."""

import time
from decimal import Decimal as D

from autotrader import db, trading
from autotrader.binance import BinanceClient

INTERVAL, RECONCILE_INTERVAL, EXPIRY_INTERVAL = 5, 30, 300


def _equity(conn, client):
    equity = D(client.balance()["wallet"]) + sum((D(p["unRealizedProfit"]) for p in client.positions()), D(0))
    trading._set(conn, "equity", equity)


def step(conn, client, now):
    healthy, failed = True, False
    tasks = [("limits", 0, trading.check_limits), ("equity", 0, _equity),
             ("reconcile", RECONCILE_INTERVAL, trading.reconcile),
             ("expiry", EXPIRY_INTERVAL, trading.close_expired)]
    for name, interval, run in tasks:
        key = "guard_" + name + "_at"
        if interval and now - float(trading._get(conn, key, "-inf")) < interval:
            continue
        if interval:
            trading._set(conn, key, now)
        try:
            run(conn, client)
        except Exception as exc:
            failed = True
            healthy = healthy and bool(interval)
            conn.execute("INSERT INTO events (at, level, source, message) VALUES (?, 'error', 'guard', ?)",
                         (now, f"{name} failed ({type(exc).__name__})"))
    note = "checks failed" if failed else "monitoring"
    if healthy:
        db.beat(conn, "guard", note)
    else:
        conn.execute("UPDATE heartbeat SET note = ? WHERE name = 'guard'", (note,))


def main():
    conn, client = db.connect(), BinanceClient()
    while True:
        try:
            step(conn, client, time.time())
        except Exception as exc:
            conn.execute("INSERT INTO events (at, level, source, message) VALUES (?, 'error', 'guard', ?)",
                         (time.time(), f"Guard step failed ({type(exc).__name__})"))
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
