"""Frozen shock rules, exercised without network or LLM calls."""

import json
import time
from decimal import Decimal as D

import pytest

from autotrader import db, engine, feedback, scanner, trading
from autotrader.test_engine import MIDNIGHT, NOW
from autotrader.test_trading import FakeClient, job


@pytest.fixture
def env(tmp_path, monkeypatch):
    conn, client, clock, prices = db.connect(tmp_path / "bot.db"), FakeClient(), [NOW], {"SOLUSDT": 102, "ETHUSDT": 102}
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(engine, "_shock_ratios", {})
    monkeypatch.setattr(scanner, "candidates", lambda client: ([], list(prices)))
    monkeypatch.setattr(scanner, "_yahoo_matches", lambda *args: True)
    monkeypatch.setattr(trading, "_indicators", lambda *args: (D(50), D(2)))
    daily = client.klines
    def klines(symbol, interval, limit):
        return daily(symbol, interval, limit) if interval != "5m" else [
            [start, "100", "101", "99", str(prices.get(symbol, 101) if i == 59 else 999 if i == 60 else 100), 1,
             start + 299999] for i in range(61) for start in [int(clock[0] // 300 - 60 + i) * 300000]]
    monkeypatch.setattr(client, "klines", klines)
    return conn, client, clock, prices


def test_crossing_once_largest_and_shared_budget(env, monkeypatch):
    conn, client, clock, prices = env
    prices.update(SOLUSDT=104, ETHUSDT=104)
    engine.step(conn, client, clock[0])
    assert not db.recent_jobs(conn) and json.loads(trading._get(conn, "engine_universe")) == list(prices)
    prices.update(SOLUSDT=102, ETHUSDT=102)
    clock[0] += 300
    engine.step(conn, client, clock[0])
    prices.update(SOLUSDT=103, ETHUSDT=104)
    clock[0] += 300
    engine.step(conn, client, clock[0])
    row = db.recent_jobs(conn)[0]
    assert (row["ticker"], row["source"]) == ("ETH-USD", "shock")
    assert json.loads(row["shock"]) == {"direction": 1, "atr": 2, "ratio": 2, "price": 104, "detected_at": clock[0], "btc_move_pct": 1}
    clock[0] += 300
    engine.step(conn, client, clock[0])
    assert len(db.recent_jobs(conn)) == 1
    monkeypatch.setattr(engine, "DAILY_BUDGET", 1)
    engine._enqueue(conn, "BTCUSDT", "2027-01-16", MIDNIGHT)
    assert len(db.recent_jobs(conn)) == 1


@pytest.mark.parametrize("gate,reason", [("day", "UTC day"), ("24h", "last 24h"), ("held", "held"),
    ("shock", "Shock-origin"), ("budget", "budget"), ("cooldown", "cooldown"), ("yahoo", "Yahoo identity")])
def test_detection_skip_reasons(env, monkeypatch, gate, reason):
    conn, client, clock, prices = env
    trading._set(conn, "engine_universe", '["SOLUSDT"]')
    engine._shock_ratios["SOLUSDT"], prices["SOLUSDT"] = D(1), 104
    if gate in ("day", "24h", "shock", "budget"):
        key = db.enqueue(conn, "SOL-USD" if gate == "24h" else "BTC-USD", "2027-01-16", ["market"],
                         source="engine" if gate == "budget" else "shock")
        if gate in ("24h", "shock"):
            db.update(conn, key, created_at=NOW - (600 if gate == "24h" else 172800))
        if gate == "shock":
            dec = trading._decision(conn, key, "BTCUSDT", "Buy", "open_long", "test")
            conn.execute("INSERT INTO trades (decision_id,symbol,side,qty,entry_price,opened_at) VALUES (?, 'BTCUSDT','LONG','1','100',?)", (dec, NOW))
        monkeypatch.setattr(engine, "DAILY_BUDGET", 1 if gate == "budget" else 12)
    if gate == "held":
        client.held["SOLUSDT"] = {"symbol": "SOLUSDT", "positionAmt": "1"}
    if gate == "cooldown":
        trading._set(conn, "cooldown:SOLUSDT", NOW + 1)
    if gate == "yahoo":
        monkeypatch.setattr(scanner, "_yahoo_matches", lambda *args: False)
    engine._shocks(conn, client, NOW, "2027-01-16", MIDNIGHT)
    assert reason in conn.execute("SELECT message FROM events WHERE level = 'info'").fetchone()[0]
    assert not conn.execute("SELECT 1 FROM jobs WHERE shock IS NOT NULL").fetchone()


@pytest.mark.parametrize("direction,rating,action", [(1, "Buy", "open_long"), (1, "Overweight", "open_long"),
    (-1, "Sell", "open_short"), (1, "Sell", "none"), (-1, "Buy", "none"), (1, "Hold", "none")])
def test_veto_fixed_levels_and_recheck(env, direction, rating, action):
    conn, client, clock, prices = env
    j = job(conn, rating, source="shock", shock=json.dumps({"direction": direction, "atr": 4}), stop_loss=99, price_target=101)
    db.update(conn, j["id"], finished_at=NOW)
    row = trading._evaluate(conn, client, db.get_job(conn, j["id"]))
    assert row["action"] == action
    if action == "none":
        assert row["reason"] == "LLM veto: rating disagrees with shock"
        return
    assert (row["stop"], row["target"]) == (100 - direction * 6, 100 + direction * 12)
    db.beat(conn, "guard")
    engine._jobs(conn, client, NOW)
    assert conn.execute("SELECT stop,target FROM trades").fetchone()[:] == (str(row["stop"]), str(row["target"]))
    flip = {**dict(j), "source": "engine", "shock": None, "rating": "Sell" if direction > 0 else "Buy"}
    assert trading._evaluate(conn, client, flip)["action"] == "hold"  # daily re-analysis cannot close a shock trade
    client.held.clear()
    assert "Shock-origin" in trading._evaluate(conn, client, {**dict(j), "ticker": "SOL-USD", "finished_at": NOW})["reason"]
    conn.execute("UPDATE trades SET status = 'closed'")
    db.update(conn, j["id"], ticker="SOL-USD")
    pending = trading.decide(conn, client, db.get_job(conn, j["id"]))
    client.held = {s: {"symbol": s, "positionAmt": str(direction), "unRealizedProfit": "0"} for s in ("A", "B")}
    trading.execute(conn, client, pending)
    assert "Two open" in conn.execute("SELECT message FROM events ORDER BY id DESC LIMIT 1").fetchone()[0] and len(client.posts) == 3


@pytest.mark.parametrize("direction,high,low,close,result", [(1, 112, 99, 100, 3), (1, 112, 94, 100, -1.5),
    (1, 103, 97, 102, .5), (-1, 101, 88, 100, 3), (-1, 106, 88, 100, -1.5), (-1, 103, 97, 98, .5)])
def test_mechanical_scoring(env, monkeypatch, direction, high, low, close, result):
    conn, client, clock, prices = env
    j = job(conn, "Hold" if direction == 1 else "Sell", source="shock", shock=json.dumps({"direction": direction, "atr": 4}))
    start = int(NOW // 3600) * 3600000
    bars = [[t, "100", str(high), str(low), str(close), 1, t + 3599999] for i in range(120) for t in [start + i * 3600000]]
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: bars, raising=False)
    db.update(conn, j["id"], finished_at=NOW)
    assert feedback.score(conn, client, NOW + 120 * 3600) == 2
    assert conn.execute("SELECT mech_atr FROM outcomes WHERE horizon_h = 120").fetchone()[0] == pytest.approx(result - .025)
    assert conn.execute("SELECT mech_atr FROM outcomes WHERE horizon_h = 24").fetchone()[0] is None


def test_stop_beyond_liquidation_distance_blocks_entry(env):
    conn, client, clock, prices = env
    j = job(conn, "Buy", source="shock", shock=json.dumps({"direction": 1, "atr": 20}))  # 1.5 ATR = 30% at mark 100
    db.update(conn, j["id"], finished_at=NOW)
    assert trading._evaluate(conn, client, db.get_job(conn, j["id"]))["reason"] == "Stop too far for 3x leverage"
