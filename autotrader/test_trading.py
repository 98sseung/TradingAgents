"""Risk and crash recovery tests; the fake exchange never uses the network."""

import json
from decimal import Decimal as D

import pytest

from autotrader import db, trading as t
from autotrader.binance import BinanceError

NOW = 1800000000


class FakeClient:
    def __init__(self):
        self.wallet, self.available, self.price = D(1000), D(1000), D(100)
        self.held, self.orders, self.income, self.posts = {}, {}, [], []
        self.crash, self.fail_stop, self.fail_cancel = False, False, False
        self.closes = [D(100)] * 16

    def positions(self):
        return list(self.held.values())

    def balance(self):
        return {"wallet": self.wallet, "available": self.available}

    def mark_price(self, symbol):
        return {"markPrice": str(self.price)}

    def filters(self, symbol):
        return {"step_size": D("0.001"), "tick_size": D("0.1"), "min_qty": D("0.001"), "min_notional": D(50)}

    def klines(self, symbol, interval, limit):
        return [[0, str(c), str(c + 2), str(c - 2), str(c), "1", NOW * 1000 - 1] for c in self.closes]

    def ensure_setup(self, symbol, leverage):
        assert leverage == 5

    def order_by_client_id(self, symbol, client_id, *, algo=False):
        if client_id not in self.orders:
            raise BinanceError(-2013, "Order does not exist")
        return self.orders[client_id]

    def open_orders(self, symbol):
        return [o for o in self.orders.values() if o["symbol"] == symbol and o.get("algoStatus") == "NEW"]

    def market_order(self, symbol, side, quantity, client_id, *, reduce_only=False):
        assert client_id not in self.orders, "duplicate market submission"
        self.posts.append(client_id)
        order = {"symbol": symbol, "clientOrderId": client_id, "status": "FILLED", "avgPrice": str(self.price)}
        self.orders[client_id] = order
        if reduce_only:
            self.held.pop(symbol, None)
        else:
            self.held[symbol] = {
                "symbol": symbol,
                "positionAmt": str(D(quantity) * (1 if side == "BUY" else -1)),
                "entryPrice": str(self.price),
                "unRealizedProfit": "0",
            }
            if self.crash:
                self.crash = False
                raise SystemExit("process died after fill")
        return order

    def protective_order(self, symbol, side, price, client_id, *, take_profit=False):
        if self.fail_stop and not take_profit:
            raise BinanceError(-4000, "stop rejected")
        assert self.orders.get(client_id, {}).get("algoStatus") != "NEW", "duplicate protection"
        self.posts.append(client_id)
        self.orders[client_id] = {"symbol": symbol, "clientAlgoId": client_id, "algoStatus": "NEW"}
        return self.orders[client_id]

    def cancel_all_orders(self, symbol):
        if self.fail_cancel:
            raise BinanceError(-1000, "cancel failed")
        for order in self.open_orders(symbol):
            order["algoStatus"] = "CANCELED"

    def income_history(self, since):
        return [r for r in self.income if r["time"] >= since]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(t.time, "time", lambda: NOW)
    monkeypatch.setattr(t.time, "sleep", lambda seconds: None)
    return db.connect(tmp_path / "bot.db"), FakeClient()


def job(conn, rating="Buy", **fields):
    job_id = db.enqueue(conn, "BTC-USD", "2027-01-15", ["market"])
    db.update(conn, job_id, status="done", rating=rating, finished_at=t.time.time(), **fields)
    return db.get_job(conn, job_id)


def decision(conn, client, rating="Buy", **fields):
    decision_id = t.decide(conn, client, job(conn, rating, **fields))
    return conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()


def opened(env, rating="Buy"):
    conn, client = env
    row = decision(conn, client, rating)
    t.execute(conn, client, row["id"])
    return conn.execute("SELECT * FROM trades WHERE decision_id = ?", (row["id"],)).fetchone()


@pytest.mark.parametrize(
    "rating,flat,long,short",
    [
        ("Buy", "open_long", "hold", "close"),
        ("Overweight", "open_long", "hold", "close"),
        ("Hold", "none", "hold", "hold"),
        ("Underweight", "none", "close", "hold"),
        ("Sell", "open_short", "close", "hold"),
        ("REVIEW", "none", "hold", "hold"),
        ("other", "none", "hold", "hold"),
    ],
)
def test_mapping(env, rating, flat, long, short):
    conn, client = env
    assert [t.symbol_for(s) for s in ("BTC-USD", "BTC-USDT", "BTCUSDT")] == ["BTCUSDT"] * 3
    assert decision(conn, client, rating)["action"] == flat
    for amount, expected in [("1", long), ("-1", short)]:
        client.held["BTCUSDT"] = {"symbol": "BTCUSDT", "positionAmt": amount}
        row = decision(conn, client, rating)
        assert row["action"] == expected and row["reason"]


@pytest.mark.parametrize(
    "gate,reason",
    [
        ("stop", "Manual stop"),
        ("kill", "KILL"),
        ("daily", "Daily loss"),
        ("positions", "Maximum"),
        ("cooldown", "cooldown"),
        ("stale", "Stale"),
        ("rsi", "RSI"),
        ("available", "available"),
        ("candles", "candles"),
        ("small", "min notional"),
        ("short_rsi", "RSI"),
        ("ledger", "Ledger"),
        ("mark", "Market data"),
    ],
)
def test_entry_gates_do_not_block_closes(env, gate, reason):
    conn, client = env
    t.check_limits(conn, client)
    j = job(conn, "Sell" if gate == "short_rsi" else "Buy")
    if gate in ("stop", "kill"):
        t._set(conn, gate, "1")
    elif gate == "daily":
        client.wallet = D(900)
    elif gate == "positions":
        client.held = {s: {"symbol": s, "positionAmt": "1", "unRealizedProfit": "0"} for s in ("A", "B", "C")}
    elif gate == "cooldown":
        t._set(conn, "cooldown:BTCUSDT", NOW + 10)
    elif gate == "stale":
        db.update(conn, j["id"], finished_at=NOW - t.MAX_AGE - 1)
        j = db.get_job(conn, j["id"])
    elif gate == "rsi":
        client.closes = [D(i + 100) for i in range(16)]
    elif gate == "short_rsi":
        client.closes = [D(100 - i) for i in range(16)]
    elif gate == "ledger":
        opened(env)
        client.held.clear()
    elif gate == "mark":

        def fail(symbol):
            raise BinanceError(-1000, "Price unavailable")

        client.mark_price = fail
    elif gate == "available":
        client.available = D(1)
    elif gate == "candles":
        client.closes = [D(100)] * 14
    elif gate == "small":
        client.wallet = D(10)
        t._set(conn, "start_equity", 10)
        t._set(conn, "day_start", json.dumps({"date": "2027-01-15", "equity": "10"}))
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (t.decide(conn, client, j),)).fetchone()
    assert row["action"] == "none" and reason.lower() in row["reason"].lower()
    client.held["BTCUSDT"] = {"symbol": "BTCUSDT", "positionAmt": "1"}
    assert decision(conn, client, "Sell")["action"] == "close"


@pytest.mark.parametrize(
    "rating,stop,target,want_stop,want_target",
    [
        ("Buy", None, None, "94", "112"),
        ("Sell", None, None, "106", "88"),
        ("Buy", 96, 105, "96", "105"),
        ("Sell", 104, 95, "104", "95"),
        ("Buy", 101, 99, "94", "112"),
        ("Sell", 99, 101, "106", "88"),
        ("Buy", 99, 101, "94", "112"),
        ("Buy", 80, 104, "94", "112"),
        ("Buy", 96, 104, "96", "108"),
        ("Sell", 104, 96, "104", "92"),
    ],
)
def test_stop_target_rules(env, rating, stop, target, want_stop, want_target):
    conn, client = env
    row = decision(conn, client, rating, stop_loss=stop, price_target=target)
    assert D(row["stop"]) == D(want_stop) and D(row["target"]) == D(want_target)


def test_sizing(env):
    conn, client = env
    client.price, client.wallet = D(60000), D(100)
    assert D(decision(conn, client)["qty"]) == D("0.002")  # $100 x 30% x 5 = $150 at 60k, step 0.001


@pytest.mark.parametrize("rating", ["Buy", "Sell"])
def test_execute_recovers_crash_and_closed_decision_never_reopens(env, monkeypatch, rating):
    conn, client = env
    row = decision(conn, client, rating)
    monkeypatch.setattr(t.time, "time", lambda: NOW + 60)
    client.crash = True
    with pytest.raises(SystemExit):
        t.execute(conn, client, row["id"])
    t.execute(conn, client, row["id"])
    t.execute(conn, client, row["id"])
    assert client.posts == [f"tb{row['id']}in", f"tb{row['id']}sl", f"tb{row['id']}tp"]
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    assert conn.execute("SELECT opened_at FROM trades").fetchone()[0] == NOW + 60
    client.held.clear()
    t.reconcile(conn, client)
    t.execute(conn, client, row["id"])
    assert not client.held and len(client.posts) == 3


def test_unresolved_entry_intent_never_resubmits(env):
    conn, client = env
    row = decision(conn, client)
    t._set(conn, f"tb{row['id']}in", "submitted")
    with pytest.raises(RuntimeError, match="unresolved"):
        t.execute(conn, client, row["id"])
    assert client.posts == []


def test_failed_stop_flattens_even_when_cancellation_fails(env):
    conn, client = env
    row = decision(conn, client)
    client.fail_stop = client.fail_cancel = True
    t.execute(conn, client, row["id"])
    assert not client.held and client.posts[-1] == f"tb{row['id']}out"
    assert conn.execute("SELECT COUNT(*) FROM events WHERE level = 'error'").fetchone()[0] >= 1
    assert conn.execute("SELECT status FROM trades").fetchone()[0] == "closed"


def test_reconcile_income_stop_cooldown_and_missing_stop(env):
    conn, client = env
    trade = opened(env)
    del client.orders[f"tb{trade['decision_id']}sl"]
    t.reconcile(conn, client)
    assert client.posts.count(f"tb{trade['decision_id']}sl") == 2
    client.orders[f"tb{trade['decision_id']}sl"].update(algoStatus="FINISHED", actualPrice="94")
    t._set(conn, f"exit:{trade['id']}", "tb99out")
    t._set(conn, "tb99out", "submitted")
    client.held.clear()
    client.income = [
        {"symbol": "BTCUSDT", "incomeType": kind, "income": value, "time": NOW * 1000}
        for kind, value in [("REALIZED_PNL", "-54"), ("COMMISSION", "-1"), ("FUNDING_FEE", "0.2")]
    ]
    client.income.append({"symbol": "ETHUSDT", "incomeType": "REALIZED_PNL", "income": "999", "time": NOW * 1000})
    t.reconcile(conn, client)
    closed = conn.execute("SELECT * FROM trades").fetchone()
    assert (closed["status"], closed["exit_reason"]) == ("closed", "stop_or_target")
    assert (D(closed["realized_pnl"]), D(closed["fees"]), D(closed["funding"])) == (D(-54), D(1), D("0.2"))
    assert "cooldown" in decision(conn, client)["reason"].lower()


def test_daily_latch_rollover_and_kill(env, monkeypatch):
    conn, client = env
    trade = opened(env)
    client.held["BTCUSDT"]["unRealizedProfit"] = "-100"
    t.check_limits(conn, client)
    client.held["BTCUSDT"]["unRealizedProfit"] = "0"
    t.check_limits(conn, client)
    assert t._get(conn, "daily_stop") == "1"
    t._set(conn, "stop", "1")
    monkeypatch.setattr(t.time, "time", lambda: NOW + 86400)
    t.check_limits(conn, client)
    assert t._get(conn, "daily_stop") == "0" and t._get(conn, "stop") == "1"
    client.held["BTCUSDT"]["unRealizedProfit"] = "-300"
    t.check_limits(conn, client)
    assert t._get(conn, "kill") == "1" and not client.held
    assert conn.execute("SELECT exit_reason FROM trades WHERE id = ?", (trade["id"],)).fetchone()[0] == "kill"
    assert "KILL" in decision(conn, client)["reason"]


def test_close_rating_max_hold_and_orphan(env):
    conn, client = env
    trade = opened(env)
    row = decision(conn, client, "Sell")
    t.execute(conn, client, row["id"])
    t.execute(conn, client, row["id"])
    assert conn.execute("SELECT exit_reason FROM trades WHERE id = ?", (trade["id"],)).fetchone()[0] == "rating"
    trade = opened(env)
    conn.execute("UPDATE trades SET opened_at = ? WHERE id = ?", (NOW - t.MAX_HOLD, trade["id"]))
    t.close_expired(conn, client)
    assert conn.execute("SELECT exit_reason FROM trades WHERE id = ?", (trade["id"],)).fetchone()[0] == "max_hold"
    client.held["BTCUSDT"] = {"symbol": "BTCUSDT", "positionAmt": "1"}
    before = list(client.posts)
    t.reconcile(conn, client)
    assert client.posts == before and conn.execute("SELECT * FROM events WHERE level = 'error'").fetchone()


def test_rearm_crash_keeps_submission_locked(env, monkeypatch):
    conn, client = env
    trade = opened(env)
    stop_id = f"tb{trade['decision_id']}sl"
    del client.orders[stop_id]
    original = client.protective_order

    def crash(*args, **kwargs):
        raise SystemExit("process died during rearm")

    monkeypatch.setattr(client, "protective_order", crash)
    with pytest.raises(SystemExit):
        t.reconcile(conn, client)
    assert t._get(conn, stop_id) == "submitted"
    monkeypatch.setattr(client, "protective_order", original)
    t.reconcile(conn, client)
    assert client.posts.count(stop_id) == 1 and not client.held


def test_flatten_finishes_an_already_flat_trade(env):
    conn, client = env
    opened(env)
    client.held.clear()
    t.flatten_all(conn, client, "kill")
    assert conn.execute("SELECT status, exit_reason FROM trades").fetchone()[:] == ("closed", "kill")


def test_exit_restores_stop_after_query_failure_and_kill_bypasses_balance(env, monkeypatch):
    conn, client = env
    opened(env)
    row = decision(conn, client, "Sell")
    positions = client.positions

    def unavailable(*args):
        raise BinanceError(-1000, "Account data unavailable")

    monkeypatch.setattr(client, "positions", unavailable)
    with pytest.raises(BinanceError):
        t.execute(conn, client, row["id"])
    assert client.held and len(client.open_orders("BTCUSDT")) == 2
    monkeypatch.setattr(client, "positions", positions)
    monkeypatch.setattr(client, "balance", unavailable)
    t._set(conn, "kill", "1")
    t.check_limits(conn, client)
    assert not client.held and conn.execute("SELECT exit_reason FROM trades").fetchone()[0] == "kill"


@pytest.mark.parametrize("visible_after", [3, None])
def test_delayed_income_backfills_and_cleans_exact_intents(env, monkeypatch, visible_after):
    conn, client = env
    trade = opened(env)
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    original_market, original_lookup = client.market_order, client.order_by_client_id
    attempts = []

    def market(*args, **kwargs):
        filled = original_market(*args, **kwargs)
        return {**filled, "status": "NEW", "avgPrice": "0"}

    def lookup(symbol, client_id, **kwargs):
        order = original_lookup(symbol, client_id, **kwargs)
        if client_id.endswith("out"):
            attempts.append(client_id)
            if visible_after is None or len(attempts) < visible_after:
                return {**order, "status": "NEW", "avgPrice": "0"}
        return order

    monkeypatch.setattr(client, "market_order", market)
    monkeypatch.setattr(client, "order_by_client_id", lookup)
    monkeypatch.setattr(t.time, "time", lambda: NOW + 10)
    client.price = D(104)
    t._set(conn, "tb123in", "submitted")
    t.flatten_all(conn, client, "manual")
    closed = conn.execute("SELECT * FROM trades WHERE id = ?", (trade["id"],)).fetchone()
    assert closed["status"] == "closed"
    assert closed["exit_price"] == (None if visible_after is None else "104")
    assert (closed["realized_pnl"], closed["fees"], closed["funding"]) == (None, None, None)
    assert len(attempts) == (5 if visible_after is None else 3)
    assert t._get(conn, f"tb{trade['decision_id']}in") == "confirmed"
    client.income = [
        {"symbol": "BTCUSDT", "incomeType": kind, "income": value, "time": (NOW + 10) * 1000}
        for kind, value in [("REALIZED_PNL", "36"), ("COMMISSION", "-2"), ("FUNDING_FEE", "-0.5")]
    ]
    client.income.append(
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "999", "time": (NOW + 11) * 1000}
    )
    monkeypatch.setattr(t.time, "time", lambda: NOW + 20)
    visible_after = 0
    t.reconcile(conn, client)
    closed = conn.execute("SELECT * FROM trades WHERE id = ?", (trade["id"],)).fetchone()
    assert (D(closed["realized_pnl"]), D(closed["fees"]), D(closed["funding"])) == (D(36), D(2), D("-0.5"))
    assert closed["closed_at"] == NOW + 10
    assert [r[0] for r in conn.execute("SELECT key FROM control WHERE key GLOB 'tb*'")] == ["tb123in"]
    assert t._get(conn, f"exit:{trade['id']}") is None
    before = list(client.posts)
    t.execute(conn, client, trade["decision_id"])
    assert client.posts == before


def test_partial_income_does_not_finalize_entry_commission(env, monkeypatch):
    conn, client = env
    trade = opened(env)
    client.income = [
        {"symbol": "BTCUSDT", "incomeType": "COMMISSION", "income": "-1", "time": NOW * 1000, "tradeId": "entry"}
    ]
    monkeypatch.setattr(t.time, "time", lambda: NOW + 10)
    t.flatten_all(conn, client, "manual")
    assert conn.execute("SELECT realized_pnl FROM trades").fetchone()[0] is None
    client.income.append(
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "0", "time": (NOW + 10) * 1000, "tradeId": "exit"}
    )
    t.reconcile(conn, client)
    assert conn.execute("SELECT fees FROM trades").fetchone()[0] is None
    client.income.append(
        {"symbol": "BTCUSDT", "incomeType": "COMMISSION", "income": "-1", "time": (NOW + 10) * 1000, "tradeId": "exit"}
    )
    t.reconcile(conn, client)
    row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade["id"],)).fetchone()
    assert (D(row["realized_pnl"]), D(row["fees"]), D(row["funding"])) == (D(0), D(2), D(0))
    client.income.append({"symbol": "BTCUSDT", "incomeType": "FUNDING_FEE", "income": "0.2", "time": (NOW + 5) * 1000})
    t.reconcile(conn, client)
    assert D(conn.execute("SELECT funding FROM trades").fetchone()[0]) == D("0.2")


def test_income_outage_keeps_trade_closed_without_new_protection(env, monkeypatch):
    conn, client = env
    opened(env)

    def unavailable(since):
        raise BinanceError(-1000, "Income unavailable")

    monkeypatch.setattr(client, "income_history", unavailable)
    t.flatten_all(conn, client, "manual")
    row = conn.execute("SELECT * FROM trades").fetchone()
    assert row["status"] == "closed" and row["realized_pnl"] is None
    assert not client.held and not client.open_orders("BTCUSDT")


def test_income_window_uses_exchange_fill_time(env, monkeypatch):
    conn, client = env
    opened(env)
    original = client.market_order

    def market(*args, **kwargs):
        return {**original(*args, **kwargs), "updateTime": (NOW + 2) * 1000}

    monkeypatch.setattr(client, "market_order", market)
    client.price = D(104)
    client.income = [
        {"symbol": "BTCUSDT", "incomeType": kind, "income": value, "time": (NOW + 2) * 1000}
        for kind, value in [("REALIZED_PNL", "36"), ("COMMISSION", "-2")]
    ]
    t.flatten_all(conn, client, "manual")
    row = conn.execute("SELECT * FROM trades").fetchone()
    assert row["closed_at"] == NOW + 2 and D(row["realized_pnl"]) == D(36)


def test_stale_analysis_cannot_close_even_after_decision(env, monkeypatch):
    conn, client = env
    opened(env)
    j = job(conn, "Sell")
    key = t.decide(conn, client, j)
    monkeypatch.setattr(t.time, "time", lambda: NOW + t.MAX_AGE + 1)
    assert t._evaluate(conn, client, j)["action"] == "none"
    t.execute(conn, client, key)
    assert client.held and len(client.posts) == 3
