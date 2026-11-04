"""Offline scanner and process steps driven by an explicit clock."""

import time
from datetime import UTC, datetime
from decimal import Decimal as D

import pandas as pd
import pytest

from autotrader import binance, db, engine, guard, scanner, trading
from autotrader.binance import BinanceError
from autotrader.scanner import candidates as scan_candidates
from autotrader.test_trading import FakeClient

MIDNIGHT = datetime(2027, 1, 16, tzinfo=UTC).timestamp()
NOW = MIDNIGHT + 300


@pytest.fixture(autouse=True)
def yahoo(monkeypatch):
    history = pd.DataFrame({"Close": [1079]}, index=pd.to_datetime([NOW - 86400], unit="s", utc=True))
    monkeypatch.setattr(scanner.yf.Ticker, "history", lambda *args, **kwargs: history)
    return history


class ScanClient:
    def __init__(self, assets):
        self.assets, self.requested = assets, []

    def symbols(self, *, live=False):
        return [{"symbol": base + "USDT", "baseAsset": base, "marginAsset": "USDT", "status": "TRADING",
                 "underlyingType": "COIN", "underlyingSubType": ["Crypto"],
                 "contractType": "PERPETUAL", "onboardDate": (NOW - age * 86400) * 1000}
                for base, volume, slope, age in self.assets]

    def tickers(self):
        return [{"symbol": base + "USDT", "quoteVolume": str(volume)} for base, volume, slope, age in self.assets]

    def klines(self, symbol, interval, limit):
        self.requested.append(symbol)
        slope = next(slope for base, volume, slope, age in self.assets if base + "USDT" == symbol)
        if slope is None:
            raise BinanceError(-1000, "Unavailable")
        closes = [D(1000 + i * slope) for i in range(80)]
        return [[0, str(c), str(c + 1), str(c - 1), str(c), 0, (NOW - (80 - i) * 86400) * 1000]
                for i, c in enumerate(closes)] + [[0, "1", "1", "1", "999999", 0, (NOW + 86400) * 1000]]


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    monkeypatch.setattr(scanner, "candidates", lambda client: ([], []))
    return db.connect(tmp_path / "bot.db"), FakeClient(), clock


def done(conn, ticker="BTC-USD", rating="Hold", source="engine"):
    job_id = db.enqueue(conn, ticker, "2027-01-16", ["market", "social", "news"], source=source)
    db.update(conn, job_id, status="done", finished_at=time.time(), rating=rating)
    return db.get_job(conn, job_id)


def test_scanner_age_memes_and_completed_trend_ranking(monkeypatch, yahoo):
    monkeypatch.setattr(time, "time", lambda: NOW)
    bases = ["LTC", "BTC", "SOL", "ETH", "XRP", "ADA", "LINK"]
    client = ScanClient([(base, 100 - i, i, 90) for i, base in enumerate(bases)])
    client.assets += [(base, 1000, 99, age) for base, age in [("NEW", 89), ("DOGE", 100),
                                                            ("1000PEPE", 100), ("1000000SHIB", 100)]]
    symbols = client.symbols()
    for item in symbols[-3:]:
        item["underlyingSubType"] = ["Meme", "Crypto"]
    monkeypatch.setattr(client, "symbols", lambda **kwargs: symbols)
    monkeypatch.setattr(scanner.yf.Ticker, "history", lambda ticker, **kwargs:
                        yahoo.assign(Close=1000 + 79 * bases.index(ticker.ticker.removesuffix("-USD"))))
    selected, eligible = scanner.candidates(client)
    assert list(selected) == [s + "USDT" for s in ("LINK", "ADA", "XRP", "ETH", "SOL")]
    assert eligible == [base + "USDT" for base in bases]
    assert selected["LINKUSDT"] == {"direction": 1, "atr": 7, "price": 1474}
    assert set(client.requested) == {base + "USDT" for base, volume, slope, age in client.assets[:7]}
    assert scanner._score(ScanClient([("BTC", 100, -1, 100)]), "BTCUSDT", NOW)[-2:] == (-1, 2)


def test_scanner_takes_top_volume_before_exclusions(monkeypatch, yahoo):
    monkeypatch.setattr(time, "time", lambda: NOW)
    yahoo["Close"] = 8900
    client = ScanClient([(f"NEW{i}", 1000 - i, 1, 1) for i in range(20)] + [("BTC", 1, 100, 100)])
    assert not scanner.candidates(client)[0] and client.requested == []
    symbols = client.symbols()
    for item in symbols[:3]:
        item["status"] = "SETTLING"
    symbols[3]["marginAsset"], symbols[4]["contractType"] = "USDC", "CURRENT_QUARTER"
    monkeypatch.setattr(client, "symbols", lambda **kwargs: symbols)
    assert list(scanner.candidates(client)[0]) == ["BTCUSDT"]


@pytest.mark.parametrize("bad_close", [1079 / 25, "1133", None, RuntimeError("Yahoo unavailable")])
def test_scanner_yahoo_identity_rejects_and_backfills(monkeypatch, yahoo, bad_close, caplog):
    monkeypatch.setattr(time, "time", lambda: NOW)
    monkeypatch.setattr(scanner, "CANDIDATE_LIMIT", 2)
    client = ScanClient([(base, 100, 1, 100) for base in ("AIN", "BTC", "ETH", "SOL")])
    calls = []

    def history(ticker, **kwargs):
        calls.append(ticker.ticker)
        if ticker.ticker == "AIN-USD":
            if isinstance(bad_close, Exception):
                raise bad_close
            return yahoo.iloc[:0] if bad_close is None else yahoo.assign(Close=bad_close)
        return yahoo.assign(Close="1132.95")  # Exactly 5% above the completed Binance close.

    monkeypatch.setattr(scanner.yf.Ticker, "history", history)
    assert list(scanner.candidates(client)[0]) == ["BTCUSDT", "ETHUSDT"]
    assert calls == ["AIN-USD", "BTC-USD", "ETH-USD"]
    assert client.requested == ["AINUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert "AINUSDT: Yahoo identity unavailable" in caplog.text


def test_symbols_uses_the_exchange_cache(monkeypatch):
    client, calls, clock = binance.BinanceClient.__new__(binance.BinanceClient), [], [0]
    client.base_url = "demo"
    monkeypatch.setattr(binance, "_EXCHANGE_INFO", {})
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def request(*args, **kwargs):
        calls.append(args)
        return {"symbols": [{"symbol": "BTCUSDT"}]}

    monkeypatch.setattr(client, "_request", request)
    assert client.symbols() == client.symbols() == [{"symbol": "BTCUSDT"}]
    clock[0] = 3600
    client.symbols()
    assert calls == [("GET", "/fapi/v1/exchangeInfo")] * 2


def test_daily_scan_once_per_day_and_deduplicates(env, monkeypatch):
    conn, client, clock = env
    calls = []
    monkeypatch.setattr(scanner, "candidates", lambda client: calls.append(clock[0]) or ({"BTCUSDT": {"direction": 1, "atr": 4, "price": 100}}, ["BTCUSDT"]))
    clock[0] = MIDNIGHT + 299
    engine.step(conn, client, clock[0])
    assert calls == []
    clock[0] += 1
    engine.step(conn, client, clock[0])
    engine.step(conn, client, clock[0] + 30)
    assert len(calls) == 1
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert (row["ticker"], row["source"], row["analysts"]) == ("BTC-USD", "engine", "market,social,news")
    clock[0] += 86400
    engine.step(conn, client, clock[0])
    assert len(calls) == 2 and conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_budget_counts_failed_engine_jobs_not_manual_and_warns_once(env, monkeypatch):
    conn, client, clock = env
    db.update(conn, done(conn, "BAD-USD")["id"], status="failed")
    done(conn, source="manual")
    monkeypatch.setattr(engine, "DAILY_BUDGET", 2)
    monkeypatch.setattr(scanner, "candidates", lambda client: ({s: {"direction": 1, "atr": 4, "price": 100} for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")}, []))
    engine.step(conn, client, clock[0])
    trading._set(conn, "engine_scan_date", "")
    engine.step(conn, client, clock[0])
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE source = 'engine'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM events WHERE message LIKE '%budget%'").fetchone()[0] == 1
    rows = conn.execute("SELECT * FROM candidates ORDER BY id LIMIT 3").fetchall()
    assert len(rows) == 3 and rows[0]["job_id"] is not None
    assert all(r["job_id"] is None and r["skip_reason"] == "Daily analysis budget exhausted" for r in rows[1:])
    assert all((r["at"], r["direction"], r["atr"], r["price"], r["kind"]) == (NOW, 1, 4, 100, "daily") for r in rows)


def test_manual_never_decided_and_engine_decided_once(env):
    conn, client, clock = env
    done(conn, source="manual")
    job = done(conn)
    engine.step(conn, client, clock[0])
    engine.step(conn, client, clock[0] + 30)
    assert [row[0] for row in conn.execute("SELECT job_id FROM decisions")] == [job["id"]]


@pytest.mark.parametrize("age", [None, 30, -1])
def test_entry_waits_for_guard_then_resumes_same_decision(env, age):
    conn, client, clock = env
    done(conn, rating="Buy")
    if age is not None:
        db.beat(conn, "guard", "monitoring")
        conn.execute("UPDATE heartbeat SET at = ? WHERE name = 'guard'", (clock[0] - age,))
    engine.step(conn, client, clock[0])
    engine.step(conn, client, clock[0])
    assert not client.posts and conn.execute("SELECT COUNT(*) FROM events WHERE level = 'warn'").fetchone()[0] == 1
    db.beat(conn, "guard", "monitoring")
    engine.step(conn, client, clock[0])
    engine.step(conn, client, clock[0])
    assert client.posts == ["tb1in", "tb1sl", "tb1tp"]
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    assert conn.execute("SELECT executed_at, attempts FROM decisions").fetchone()[:] == (clock[0], 0)


def test_failing_job_and_existing_decision_survive_restart(env, monkeypatch):
    conn, client, clock = env
    bad, good = done(conn, "BAD-USD"), done(conn, rating="Buy")
    decide = trading.decide
    existing = decide(conn, client, good)

    def failing(conn, client, job):
        if job["id"] == bad["id"]:
            raise BinanceError(-1000, "Failure")
        return decide(conn, client, job)

    monkeypatch.setattr(trading, "decide", failing)
    db.beat(conn, "guard", "monitoring")
    client.crash = True
    with pytest.raises(SystemExit):
        engine.step(conn, client, clock[0])
    conn.execute("DELETE FROM heartbeat WHERE name = 'guard'")
    engine.step(conn, client, clock[0])
    assert client.posts == [f"tb{existing}in", f"tb{existing}sl", f"tb{existing}tp"]
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    assert conn.execute("SELECT * FROM events WHERE level = 'error' AND source = 'engine'").fetchone()


def test_event_reanalysis_obeys_atr_age_cadence_and_engine_source(env):
    conn, client, clock = env
    manual = done(conn, rating="Buy", source="manual")
    trading.execute(conn, client, trading.decide(conn, client, manual))
    old = done(conn, rating="Hold")
    db.update(conn, old["id"], status="failed", created_at=NOW - 21601)
    trading._set(conn, "engine_scan_date", "2027-01-16")
    client.price = D(108)
    engine.step(conn, client, clock[0])
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE source = 'engine'").fetchone()[0] == 2
    latest = conn.execute("SELECT MAX(id) FROM jobs").fetchone()[0]
    db.update(conn, latest, status="failed", created_at=NOW - 21601)
    clock[0] += 299
    engine.step(conn, client, clock[0])
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE source = 'engine'").fetchone()[0] == 2
    clock[0] += 1
    engine.step(conn, client, clock[0])
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE source = 'engine'").fetchone()[0] == 3


def test_guard_cadence_persists_and_equity_is_decimal(env, monkeypatch):
    conn, client, clock = env
    calls = []
    for name in ("check_limits", "reconcile", "close_expired"):
        monkeypatch.setattr(trading, name, lambda conn, client, name=name: calls.append(name))
    for elapsed in (0, 5, 29, 30, 299, 300):
        clock[0] = NOW + elapsed
        guard.step(conn, client, clock[0])
    assert calls.count("check_limits") == 6 and calls.count("reconcile") == 3
    assert calls.count("close_expired") == 2 and D(trading._get(conn, "equity")) == 1000
    assert db.heartbeat(conn, "guard")["at"] == clock[0]


def test_guard_error_does_not_skip_other_checks_or_next_step(env, monkeypatch):
    conn, client, clock = env
    original, calls = trading.check_limits, []

    def fails_once(conn, client):
        calls.append("limits")
        if len(calls) == 1:
            raise BinanceError(-1000, "Unavailable")
        original(conn, client)

    monkeypatch.setattr(trading, "check_limits", fails_once)
    guard.step(conn, client, clock[0])
    assert trading._get(conn, "equity") == "1000" and db.heartbeat(conn, "guard") is None
    clock[0] += 5
    guard.step(conn, client, clock[0])
    assert len(calls) == 2 and db.heartbeat(conn, "guard")["at"] == clock[0]
    assert conn.execute("SELECT * FROM events WHERE source = 'guard' AND level = 'error'").fetchone()


def test_scan_retry_does_not_requeue_finished_holdings_and_closes_ignore_stale_guard(env, monkeypatch):
    conn, client, clock = env
    manual = done(conn, rating="Buy", source="manual")
    trading.execute(conn, client, trading.decide(conn, client, manual))

    def unavailable(client):
        raise BinanceError(-1000, "Unavailable")

    monkeypatch.setattr(scanner, "candidates", unavailable)
    engine.step(conn, client, clock[0])
    job_id = conn.execute("SELECT id FROM jobs WHERE source = 'engine'").fetchone()[0]
    db.update(conn, job_id, status="done", rating="Hold", finished_at=clock[0])
    clock[0] += 30
    engine.step(conn, client, clock[0])
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE source = 'engine'").fetchone()[0] == 1
    assert not conn.execute("SELECT 1 FROM candidates").fetchone()
    done(conn, rating="Sell")
    engine.step(conn, client, clock[0])
    assert not client.held and client.posts[-1].endswith("out")


def test_scanner_symbol_failures_log_normally_with_one_scan_event(env, monkeypatch, caplog):
    conn, client, clock = env
    scan = ScanClient([("BAD", 20, None, 100), ("BTC", 10, 1, 100), ("BADVOL", "NaN", 1, 100),
                       ("BROKEN", 30, 1, 100)])
    symbols = scan.symbols()
    symbols[-1]["onboardDate"] = None
    monkeypatch.setattr(scan, "symbols", lambda **kwargs: symbols)
    monkeypatch.setattr(scanner, "candidates", scan_candidates)
    engine.step(conn, scan, clock[0])
    assert conn.execute("SELECT ticker FROM jobs").fetchone()[0] == "BTC-USD"
    assert len(caplog.records) == 3
    assert [r[0] for r in conn.execute("SELECT message FROM events WHERE source = 'engine'")] == ["Daily scan: BTCUSDT"]


def test_scanner_uses_classification_not_asset_names(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW)
    client = ScanClient([(base, 100, 1, 100) for base in ("DOGE", "NEW_MEME", "STOCK", "GOLD")])
    symbols = client.symbols()
    symbols[1]["underlyingSubType"] = ["Crypto", "Meme"]
    for symbol, kind in zip(symbols[2:], ("EQUITY", "COMMODITY"), strict=True):
        symbol["underlyingType"] = kind
        symbol["contractType"] = "TRADIFI_PERPETUAL"
    monkeypatch.setattr(client, "symbols", lambda **kwargs: symbols)
    assert list(scanner.candidates(client)[0]) == ["DOGEUSDT"]


def test_scanner_uses_live_metadata_but_venue_tradability(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW)
    assets = [(base, 100, 1, 100) for base in ("BTC", "DOGE", "1000PEPE", "NEW", "LIVEONLY", "HALTED")]
    client = ScanClient(assets)
    metadata = client.symbols()
    venue = [dict(s, underlyingType="EQUITY", underlyingSubType=["HOT"], onboardDate=0)
             for s in metadata if s["symbol"] != "LIVEONLYUSDT"]
    venue[-1]["status"] = "SETTLING"
    metadata[0]["status"] = "SETTLING"
    for item in metadata[1:3]:
        item["underlyingSubType"] = ["Meme", "Crypto"]
    metadata[3]["onboardDate"] = (NOW - 86400) * 1000
    monkeypatch.setattr(client, "symbols", lambda *, live=False: metadata if live else venue)
    assert list(scanner.candidates(client)[0]) == ["BTCUSDT"]
    assert client.requested == ["BTCUSDT"]


def test_execution_failure_is_bounded_and_does_not_block_later_decisions(env, monkeypatch):
    conn, client, clock = env
    bad, good = done(conn, "BAD-USD", "Buy"), done(conn, rating="Buy")
    execute = trading.execute
    calls = []

    def failing(conn, client, decision_id):
        job_id = conn.execute("SELECT job_id FROM decisions WHERE id = ?", (decision_id,)).fetchone()[0]
        if job_id == bad["id"]:
            calls.append(decision_id)
            raise BinanceError(-1000, "Unavailable")
        return execute(conn, client, decision_id)

    monkeypatch.setattr(trading, "execute", failing)
    for _ in range(4):
        db.beat(conn, "guard", "monitoring")
        engine.step(conn, client, clock[0])
        clock[0] += 30
    rows = conn.execute("SELECT * FROM decisions ORDER BY id").fetchall()
    assert len(calls) == 3 and rows[0]["attempts"] == 3 and rows[0]["executed_at"] is not None
    assert rows[1]["job_id"] == good["id"] and rows[1]["executed_at"] is not None
    assert conn.execute("SELECT COUNT(*) FROM events WHERE level = 'error'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM events WHERE message LIKE '%gave up%'").fetchone()[0] == 1


@pytest.mark.parametrize("task,interval", [("reconcile", 30), ("close_expired", 300)])
def test_guard_failed_task_stays_unhealthy_until_retry(env, monkeypatch, task, interval):
    conn, client, clock = env
    done(conn, rating="Buy")
    def fail(conn, client):
        raise ValueError("check failed")
    monkeypatch.setattr(trading, task, fail)
    for elapsed in (0, 5):
        clock[0] = NOW + elapsed
        guard.step(conn, client, clock[0])
        assert db.heartbeat(conn, "guard")["note"] == "checks failed"
        engine._jobs(conn, client, clock[0])
        assert not client.posts
    monkeypatch.setattr(trading, task, lambda *args: None)
    clock[0] = NOW + interval
    guard.step(conn, client, clock[0])
    engine._jobs(conn, client, clock[0])
    assert db.heartbeat(conn, "guard")["note"] == "monitoring" and len(client.posts) == 3
