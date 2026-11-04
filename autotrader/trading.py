"""Deterministic risk decisions, crash recovery, and the Binance trade ledger."""

import json
import time
from datetime import UTC, datetime
from decimal import Decimal as D

from autotrader.binance import BinanceError, round_price, round_quantity

LEVERAGE, MAX_POSITIONS = 5, 3
MARGIN_FRACTION, DAILY_LOSS, TOTAL_LOSS = D("0.30"), D("0.10"), D("0.30")
MAX_HOLD, COOLDOWN, MAX_AGE = 5 * 86400, 86400, 6 * 3600
ACCOUNTING_SETTLE = 300  # income from separate fills can become visible at different times
PERIOD, LONG_RSI, SHORT_RSI = 14, D(75), D(25)
MIN_ATR, MAX_ATR, STOP_ATR, TARGET_R = D("0.5"), D(3), D("1.5"), D(2)
MAX_STOP = D("0.6") / LEVERAGE  # isolated Nx liquidates a bit inside a 1/N move: the stop must trigger well before


def symbol_for(ticker):
    ticker = ticker.strip().upper()
    if ticker.endswith("-USD"):
        ticker = ticker[:-4] + "USDT"
    ticker = ticker.replace("-USDT", "USDT")
    if not ticker.endswith("USDT") or not ticker[:-4].isalnum():
        raise ValueError("Unsupported USDT ticker")
    return ticker


def _get(conn, key, default=None):
    row = conn.execute("SELECT value FROM control WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _set(conn, key, value):
    conn.execute(
        "INSERT INTO control VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
        "value = excluded.value, updated_at = excluded.updated_at",
        (key, str(value), time.time()),
    )


def _event(conn, level, message):
    conn.execute(
        "INSERT INTO events (at, level, source, message) VALUES (?, ?, ?, ?)", (time.time(), level, "trading", message)
    )


def _decision(conn, job_id, symbol, rating, action, reason, mark_price=None, qty=None, stop=None, target=None):
    return conn.execute(
        "INSERT INTO decisions (job_id, symbol, rating, action, reason, mark_price, qty, stop, target, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            symbol,
            rating,
            action,
            reason,
            *[str(v) if v is not None else None for v in (mark_price, qty, stop, target)],
            time.time(),
        ),
    ).lastrowid


def _position(client, symbol, tries=1):
    for attempt in range(tries):
        position = next((p for p in client.positions() if p["symbol"] == symbol and D(p["positionAmt"])), None)
        if position or attempt + 1 == tries:
            return position
        time.sleep(1)


def _trade(conn, symbol):
    return conn.execute("SELECT * FROM trades WHERE symbol = ? AND status = 'open'", (symbol,)).fetchone()


def _shock_open(conn, symbol=None):
    return conn.execute("SELECT 1 FROM trades t JOIN decisions d ON d.id = t.decision_id "
                        "JOIN jobs j ON j.id = d.job_id WHERE t.status = 'open' AND j.source = 'shock' "
                        "AND t.symbol = COALESCE(?, t.symbol)", (symbol,)).fetchone()


def _indicators(client, symbol):
    bars = [b for b in client.klines(symbol, "1d", 100) if b[6] < time.time() * 1000]
    if len(bars) < PERIOD + 1:
        raise ValueError("Insufficient completed daily candles")
    changes = [D(b[4]) - D(a[4]) for a, b in zip(bars, bars[1:], strict=False)]
    ranges = [
        max(D(b[2]) - D(b[3]), abs(D(b[2]) - D(a[4])), abs(D(b[3]) - D(a[4])))
        for a, b in zip(bars, bars[1:], strict=False)
    ]
    gain, loss, atr = (
        sum(values[:PERIOD]) / PERIOD
        for values in ([max(c, D(0)) for c in changes], [max(-c, D(0)) for c in changes], ranges)
    )
    for change, true_range in zip(changes[PERIOD:], ranges[PERIOD:], strict=True):
        gain, loss, atr = (
            (gain * (PERIOD - 1) + max(change, D(0))) / PERIOD,
            (loss * (PERIOD - 1) + max(-change, D(0))) / PERIOD,
            (atr * (PERIOD - 1) + true_range) / PERIOD,
        )
    rsi = 100 - 100 / (1 + gain / loss) if loss else D(100) if gain else D(50)
    if not atr.is_finite() or atr <= 0:
        raise ValueError("Invalid daily ATR")
    return rsi, atr


def _fresh(job):
    return (job["status"] == "done" and job["finished_at"] is not None
            and 0 <= time.time() - job["finished_at"] <= MAX_AGE)


def _evaluate(conn, client, job):
    job = dict(job)
    rating, mark, qty, stop, target = job.get("rating") or "", None, None, None, None
    action, reason, symbol = "none", "Rating does not request entry", job["ticker"]
    try:
        symbol = symbol_for(symbol)
        if not _fresh(job):
            raise ValueError("Stale or unfinished analysis")
        shock = json.loads(job["shock"]) if job.get("source") == "shock" else None
        if shock and rating not in (("Buy", "Overweight") if shock["direction"] == 1 else ("Sell",)):
            raise ValueError("LLM veto: rating disagrees with shock")
        position = _position(client, symbol)
        amount = D(position["positionAmt"]) if position else D(0)
        if amount:
            # Shock trades keep the tested fixed exits (stop, target, 5-day expiry); a later rating must not close them.
            fixed = _shock_open(conn, symbol)
            action = (
                "close"
                if not fixed
                and (amount > 0 and rating in ("Underweight", "Sell") or amount < 0 and rating in ("Buy", "Overweight"))
                else "hold"
            )
            reason, qty = ("Rating requests exit" if action == "close" else
                           "Shock trade keeps its fixed exits" if fixed else "Rating keeps position"), abs(amount)
            mark = D(client.mark_price(symbol)["markPrice"])
        elif rating in ("Buy", "Overweight", "Sell"):
            mark = D(client.mark_price(symbol)["markPrice"])
            _update_limits(conn, client)
            positions = client.positions()
            tracked = {r["symbol"] for r in conn.execute("SELECT symbol FROM trades WHERE status = 'open'")}
            gates = [
                (_get(conn, "kill") == "1", "KILL lock"),
                (_get(conn, "stop") == "1", "Manual stop"),
                (_get(conn, "daily_stop") == "1", "Daily loss limit"),
                (symbol in tracked, "Ledger requires reconciliation"),
                (len(tracked | {p["symbol"] for p in positions}) >= MAX_POSITIONS, "Maximum open positions"),
                (time.time() < float(_get(conn, "cooldown:" + symbol, 0)), "Stop cooldown"),
            ]
            for blocked, message in gates:
                if blocked:
                    raise ValueError(message)
            if shock:
                if time.time() - shock["detected_at"] > 30 * 60:
                    raise ValueError("Shock too old")
                if abs(mark - D(str(shock["price"]))) > D("0.5") * D(str(shock["atr"])):
                    raise ValueError("Price moved away from shock")
                if _shock_open(conn):
                    raise ValueError("Shock-origin position open")
                sides = {r["symbol"]: 1 if r["side"] == "LONG" else -1
                         for r in conn.execute("SELECT symbol, side FROM trades WHERE status = 'open'")}
                sides.update({p["symbol"]: 1 if D(p["positionAmt"]) > 0 else -1 for p in positions})
                if sum(side == shock["direction"] for side in sides.values()) >= 2:
                    raise ValueError("Two open positions share shock direction")
            rsi, atr = _indicators(client, symbol)
            direction = D(-1) if rating == "Sell" else D(1)
            if (direction > 0 and rsi > LONG_RSI) or (direction < 0 and rsi < SHORT_RSI):
                raise ValueError("RSI blocks entry")
            filters, balance = client.filters(symbol), client.balance()
            margin = D(balance["wallet"]) * MARGIN_FRACTION
            if margin > D(balance["available"]):
                raise ValueError("Insufficient available balance")
            qty = round_quantity(margin * LEVERAGE / mark, mark, filters)
            stop = D(0) if shock else D(str(job.get("stop_loss") or 0))
            if shock:
                atr = D(str(shock["atr"]))
                stop = mark - direction * D("1.5") * atr
            elif not stop.is_finite() or stop <= 0 or not MIN_ATR * atr <= direction * (mark - stop) <= MAX_ATR * atr:
                stop = mark - direction * STOP_ATR * atr
            stop = round_price(stop, filters["tick_size"])
            risk = direction * (mark - stop)
            if risk > mark * MAX_STOP:
                raise ValueError(f"Stop too far for {LEVERAGE}x leverage")
            target = D(0) if shock else D(str(job.get("price_target") or 0))
            if shock:
                target = mark + direction * 3 * atr
            elif not target.is_finite() or target <= 0 or direction * (target - mark) <= risk:
                target = mark + direction * TARGET_R * risk
            target = round_price(target, filters["tick_size"])
            if risk <= 0 or direction * (target - mark) <= risk:
                raise ValueError("Invalid stop or target after rounding")
            if client.open_orders(symbol):
                raise ValueError("Outstanding orders require reconciliation")
            action, reason = ("open_short" if direction < 0 else "open_long"), "Entry gates passed"
    except (ValueError, BinanceError, ArithmeticError) as exc:
        reason = (
            reason + "; mark unavailable"
            if action in ("close", "hold")
            else str(exc)
            if isinstance(exc, ValueError)
            else "Market data or account check failed"
        )
    return {
        "job_id": job["id"],
        "symbol": symbol,
        "rating": rating,
        "action": action,
        "reason": reason,
        "mark_price": mark,
        "qty": qty,
        "stop": stop,
        "target": target,
    }


def decide(conn, client, job):
    return _decision(conn, **_evaluate(conn, client, job))


def _lookup(client, symbol, client_id, algo=False, tries=1):
    for attempt in range(tries):
        try:
            return client.order_by_client_id(symbol, client_id, algo=algo)
        except BinanceError as exc:
            if exc.code != -2013:
                raise
            if algo:
                found = next((o for o in client.open_orders(symbol) if o.get("clientAlgoId") == client_id), None)
                if found:
                    return found
            if attempt + 1 < tries:
                time.sleep(1)
    return None


def _submit(conn, client, symbol, client_id, place, algo=False):
    # submitted may have reached Binance; only confirmed protection can be re-armed.
    prior = _get(conn, client_id)
    order = _lookup(client, symbol, client_id, algo, 5 if prior else 1)
    if algo and order and order.get("algoStatus") in ("FINISHED", "TRIGGERED"):
        raise RuntimeError("Protective order already triggered")
    if order and (not algo or order.get("algoStatus") not in ("CANCELED", "EXPIRED", "REJECTED")):
        _set(conn, client_id, "confirmed")
        return order
    if prior == "submitted" or (prior and not order and not algo):
        raise RuntimeError("Order outcome unresolved: " + client_id)
    if prior:
        claimed = conn.execute(
            "UPDATE control SET value = 'submitted', updated_at = ? WHERE key = ? AND value = 'confirmed'",
            (time.time(), client_id),
        )
    else:
        claimed = conn.execute("INSERT OR IGNORE INTO control VALUES (?, ?, ?)", (client_id, "submitted", time.time()))
    if not claimed.rowcount:
        raise RuntimeError("Order submission already in progress")
    order = place()
    _set(conn, client_id, "confirmed")
    return order


def _protect(conn, client, trade):
    trade = dict(trade)
    direction = D(1) if trade["side"] == "LONG" else D(-1)
    entry, risk = D(trade["entry_price"]), direction * (D(trade["entry_price"]) - D(trade["stop"]))
    if risk <= 0:
        raise ValueError("Fill crossed the approved stop")
    proposed = conn.execute(
        "SELECT j.price_target, j.source FROM jobs j JOIN decisions d ON d.job_id = j.id WHERE d.id = ?",
        (trade["decision_id"],)
    ).fetchone()
    shock = proposed and proposed["source"] == "shock"
    proposed = D(str(proposed[0] or 0)) if proposed else D(0)
    if not shock and (not proposed.is_finite() or proposed <= 0 or direction * (proposed - entry) <= risk):
        trade["target"] = str(
            round_price(entry + direction * TARGET_R * risk, client.filters(trade["symbol"])["tick_size"])
        )
        conn.execute("UPDATE trades SET target = ? WHERE id = ?", (trade["target"], trade["id"]))
    for suffix, price in [("sl", trade["stop"]), ("tp", trade["target"])]:
        client_id = f"tb{trade['decision_id']}{suffix}"
        _submit(
            conn,
            client,
            trade["symbol"],
            client_id,
            lambda price=price, client_id=client_id, suffix=suffix: client.protective_order(
                trade["symbol"],
                "SELL" if trade["side"] == "LONG" else "BUY",
                price,
                client_id,
                take_profit=suffix == "tp",
            ),
            algo=True,
        )


def _exit_fill(conn, client, trade, order=None):
    exit_id = _get(conn, f"exit:{trade['id']}")
    if order and order.get("status") == "FILLED" and D(order.get("avgPrice") or 0) > 0:
        return order
    triggered = None
    for attempt in range(5):
        if exit_id:
            order = _lookup(client, trade["symbol"], exit_id)
            if order and order.get("status") == "FILLED" and D(order.get("avgPrice") or 0) > 0:
                return order
        # A protective order may win the race against a requested market exit.
        for suffix in ("sl", "tp"):
            order = _lookup(client, trade["symbol"], f"tb{trade['decision_id']}{suffix}", True)
            if order and order.get("algoStatus") == "FINISHED":
                triggered = order
                if D(order.get("actualPrice") or 0) > 0:
                    return order
        if attempt < 4:
            time.sleep(1)
    return triggered


def _accounting(client, trade, end):
    rows = [
        r
        for r in client.income_history(int(trade["opened_at"] * 1000))
        if r["symbol"] == trade["symbol"] and r["time"] <= end
    ]
    realized = [r for r in rows if r["incomeType"] == "REALIZED_PNL"]
    commissions = [r for r in rows if r["incomeType"] == "COMMISSION"]
    # Entry commission can arrive before closing PnL and its matching commission.
    if not realized:
        return None
    for pnl in realized:
        if pnl.get("tradeId"):
            matched = any(fee.get("tradeId") == pnl["tradeId"] for fee in commissions)
        else:
            matched = any(fee["time"] == pnl["time"] for fee in commissions)
        if not matched:
            return None
    pnl, commission, funding = (
        sum((D(r["income"]) for r in rows if r["incomeType"] == kind), D(0))
        for kind in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE")
    )
    return str(pnl), str(-commission), str(funding)


def _finish(conn, client, trade, reason, order=None):
    closed_at = trade["closed_at"] if trade["closed_at"] is not None else time.time()
    conn.execute(
        "UPDATE trades SET status = 'closed', closed_at = ?, exit_reason = ? WHERE id = ?",
        (closed_at, reason, trade["id"]),
    )
    price, stopped = D(trade["exit_price"]) if trade["exit_price"] else None, False
    try:
        if price is None:
            fill = _exit_fill(conn, client, trade, order)
            if fill:
                protective = fill.get("algoStatus") == "FINISHED"
                stopped = protective and fill.get("clientAlgoId") == f"tb{trade['decision_id']}sl"
                candidate = D(fill.get("actualPrice" if protective else "avgPrice") or 0)
                if candidate > 0:
                    price = candidate
                    if fill.get("updateTime"):
                        closed_at = float(fill["updateTime"]) / 1000
        if price is not None:
            conn.execute(
                "UPDATE trades SET exit_price = ?, closed_at = ? WHERE id = ?", (str(price), closed_at, trade["id"])
            )
        direction = D(1) if trade["side"] == "LONG" else D(-1)
        if stopped or (
            reason == "stop_or_target" and price is not None and direction * (price - D(trade["stop"])) <= 0
        ):
            _set(conn, "cooldown:" + trade["symbol"], closed_at + COOLDOWN)
        next_trade = conn.execute(
            "SELECT MIN(opened_at) FROM trades WHERE symbol = ? AND id != ? AND opened_at >= ?",
            (trade["symbol"], trade["id"], trade["opened_at"]),
        ).fetchone()[0]
        end = (
            min(round(closed_at * 1000), round(next_trade * 1000) - 1)
            if next_trade is not None
            else round(closed_at * 1000)
        )
        totals = _accounting(client, trade, end)
    except BinanceError:
        _event(conn, "warn", trade["symbol"] + ": closed; exchange accounting still pending")
        return
    if totals is None or price is None:
        return
    base, exit_key = f"tb{trade['decision_id']}", f"exit:{trade['id']}"
    keys = [base + suffix for suffix in ("in", "sl", "tp", "out")]
    keys.extend([exit_key, _get(conn, exit_key)])
    conn.execute("SAVEPOINT finalize_trade")
    try:
        conn.execute("UPDATE trades SET realized_pnl = ?, fees = ?, funding = ? WHERE id = ?", (*totals, trade["id"]))
        conn.executemany("DELETE FROM control WHERE key = ?", [(key,) for key in keys if key])
    except Exception:
        conn.execute("ROLLBACK TO finalize_trade")
        raise
    finally:
        conn.execute("RELEASE finalize_trade")


def _close(conn, client, trade, decision_id, reason):
    symbol = trade["symbol"]
    if trade.get("id"):
        conn.execute("UPDATE trades SET exit_reason = ? WHERE id = ?", (reason, trade["id"]))
    try:
        client.cancel_all_orders(symbol)
    except BinanceError:
        _event(conn, "error", symbol + ": cancellation failed; attempting reduce-only exit")
    try:
        position = _position(client, symbol)
        order = None
        if position:
            amount = D(position["positionAmt"])
            exit_id = f"tb{decision_id}out"
            if trade.get("id"):
                exit_id = _get(conn, f"exit:{trade['id']}", exit_id)
                _set(conn, f"exit:{trade['id']}", exit_id)
            order = _submit(
                conn,
                client,
                symbol,
                exit_id,
                lambda: client.market_order(
                    symbol, "SELL" if amount > 0 else "BUY", abs(amount), exit_id, reduce_only=True
                ),
            )
        if _position(client, symbol):
            raise RuntimeError("Exit not confirmed flat: " + symbol)
        if trade.get("id"):
            _finish(conn, client, trade, reason, order)
    except (BinanceError, RuntimeError, ValueError, ArithmeticError):
        _event(conn, "error", symbol + ": exit unconfirmed; restoring protection")
        if trade.get("id"):
            try:
                _protect(conn, client, trade)
            except (BinanceError, RuntimeError, ValueError, ArithmeticError):
                _set(conn, "kill", "1")
                _event(conn, "error", symbol + ": exit and stop recovery failed; KILL locked")
        raise


def execute(conn, client, decision_id):
    decision = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    if decision is None:
        raise ValueError("Unknown decision")
    decision = dict(decision)
    symbol, action = decision["symbol"], decision["action"]
    trade = conn.execute("SELECT * FROM trades WHERE decision_id = ?", (decision_id,)).fetchone()
    if trade and trade["status"] == "closed":
        return
    if action == "close":
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (decision["job_id"],)).fetchone()
        if job is not None and not _fresh(job):
            _event(conn, "warn", symbol + ": close blocked during execution recheck: Stale or unfinished analysis")
            return
        trade = _trade(conn, symbol)
        if trade and trade["opened_at"] <= decision["created_at"]:
            _close(conn, client, dict(trade), decision_id, "rating")
    elif action in ("open_long", "open_short"):
        position = _position(client, symbol)
        intent = _get(conn, f"tb{decision_id}in")
        entry = (
            None
            if position and (intent or trade)
            else _lookup(client, symbol, f"tb{decision_id}in", tries=5 if intent else 1)
        )
        if not entry and not trade and not position:
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (decision["job_id"],)).fetchone()
            fresh = _evaluate(conn, client, job)
            if fresh["action"] != action:
                _event(conn, "warn", symbol + ": entry blocked during execution recheck: " + fresh["reason"])
                return
            for field in ("mark_price", "qty", "stop", "target"):
                decision[field] = str(fresh[field])
            conn.execute(
                "UPDATE decisions SET mark_price = ?, qty = ?, stop = ?, target = ? WHERE id = ?",
                (decision["mark_price"], decision["qty"], decision["stop"], decision["target"], decision_id),
            )
            client.ensure_setup(symbol, LEVERAGE)
            try:
                entry = _submit(
                    conn,
                    client,
                    symbol,
                    f"tb{decision_id}in",
                    lambda: client.market_order(
                        symbol, "BUY" if action == "open_long" else "SELL", decision["qty"], f"tb{decision_id}in"
                    ),
                )
            except (BinanceError, RuntimeError):
                if not _position(client, symbol):
                    _event(conn, "error", symbol + ": entry outcome unresolved; submission locked")
                    raise
            position = _position(client, symbol, 5)
        if not position:
            if trade:
                _finish(conn, client, trade, trade["exit_reason"] or "stop_or_target")
            elif entry:
                _set(conn, "kill", "1")
                _event(conn, "error", symbol + ": entry has no confirmed position; KILL locked")
            return
        if not entry and not _get(conn, f"tb{decision_id}in"):
            raise RuntimeError("Untracked position; refusing entry adoption")
        if not trade:
            conn.execute(
                "INSERT INTO trades (decision_id, symbol, side, qty, entry_price, stop, target, opened_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision_id,
                    symbol,
                    "LONG" if action == "open_long" else "SHORT",
                    str(abs(D(position["positionAmt"]))),
                    position["entryPrice"],
                    decision["stop"],
                    decision["target"],
                    float((entry or position).get("updateTime") or (entry or {}).get("time") or time.time() * 1000)
                    / 1000,
                ),
            )
            trade = _trade(conn, symbol)
        try:
            _protect(conn, client, trade)
        except (BinanceError, RuntimeError, ValueError):
            _event(conn, "error", symbol + ": protection failed; closing immediately")
            _close(conn, client, dict(trade), decision_id, "manual")


def reconcile(conn, client):
    pending = conn.execute(
        "SELECT * FROM trades WHERE status = 'closed' AND "
        "(realized_pnl IS NULL OR exit_price IS NULL OR closed_at >= ?)",
        (time.time() - ACCOUNTING_SETTLE,),
    ).fetchall()
    positions = {p["symbol"]: p for p in client.positions()}
    trades = {t["symbol"]: t for t in conn.execute("SELECT * FROM trades WHERE status = 'open'")}
    for symbol in positions.keys() - trades.keys():
        _event(conn, "error", symbol + ": exchange position has no open trade")
    for symbol, trade in trades.items():
        if symbol not in positions:
            _finish(conn, client, trade, trade["exit_reason"] or "stop_or_target")
            client.cancel_all_orders(symbol)
        else:
            try:
                _protect(conn, client, trade)
            except (BinanceError, RuntimeError, ValueError):
                _event(conn, "error", symbol + ": stop recovery failed; closing immediately")
                _close(conn, client, dict(trade), trade["decision_id"], "manual")

    for trade in pending:
        _finish(conn, client, trade, trade["exit_reason"])


def close_expired(conn, client):
    for trade in conn.execute(
        "SELECT * FROM trades WHERE status = 'open' AND opened_at <= ?", (time.time() - MAX_HOLD,)
    ).fetchall():
        decision_id = _decision(conn, None, trade["symbol"], "", "close", "Maximum hold reached")
        try:
            _close(conn, client, dict(trade), decision_id, "max_hold")
        except (BinanceError, RuntimeError, ValueError):
            _event(conn, "error", trade["symbol"] + ": maximum-hold exit failed")


def flatten_all(conn, client, reason):
    for position in client.positions():
        symbol = position["symbol"]
        trade = _trade(conn, symbol)
        decision_id = _decision(conn, None, symbol, "", "close", reason)
        try:
            _close(conn, client, dict(trade) if trade else {"symbol": symbol}, decision_id, reason)
        except (BinanceError, RuntimeError, ValueError):
            _event(conn, "error", symbol + ": flatten failed; requires attention")
    for trade in conn.execute("SELECT * FROM trades WHERE status = 'open'").fetchall():
        if not _position(client, trade["symbol"]):
            _finish(conn, client, trade, reason)


def _update_limits(conn, client):
    equity = D(client.balance()["wallet"]) + sum((D(p["unRealizedProfit"]) for p in client.positions()), D(0))
    today = datetime.fromtimestamp(time.time(), UTC).date().isoformat()
    if _get(conn, "start_equity") is None:
        _set(conn, "start_equity", equity)
    day = json.loads(_get(conn, "day_start", "{}"))
    if day.get("date") != today:
        day = {"date": today, "equity": str(equity)}
        _set(conn, "day_start", json.dumps(day))
        _set(conn, "daily_stop", "0")
    if equity <= D(day["equity"]) * (1 - DAILY_LOSS) and _get(conn, "daily_stop") != "1":
        _set(conn, "daily_stop", "1")
        _event(conn, "warn", "Daily loss limit reached; entries stopped until next UTC day")
    if equity <= D(_get(conn, "start_equity")) * (1 - TOTAL_LOSS) and _get(conn, "kill") != "1":
        _set(conn, "kill", "1")
        _event(conn, "error", "Total loss limit reached; KILL locked")


def check_limits(conn, client):
    if _get(conn, "kill") != "1":
        _update_limits(conn, client)
    if _get(conn, "kill") == "1":
        flatten_all(conn, client, "kill")
