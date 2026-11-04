"""Analysis scoring uses live candles, independent of demo trading prices."""

import pytest

from autotrader import binance, db, feedback


@pytest.mark.parametrize("rating,ret,expected", [
    ("Buy", 1, "correct"), ("Buy", -1, "wrong"), ("Buy", 0.99, "inconclusive"),
    ("Overweight", 2, "correct"), ("Overweight", -2, "wrong"),
    ("Sell", -1, "correct"), ("Sell", 1, "wrong"), ("Sell", -0.99, "inconclusive"),
    ("Underweight", -2, "correct"), ("Underweight", 2, "wrong"),
    ("Hold", 1.99, "correct"), ("Hold", -1.99, "correct"),
    ("Hold", 2, "wrong"), ("Hold", -2, "wrong"), ("Hold", 0, "correct"),
])
def test_verdict(rating, ret, expected):
    assert feedback.verdict(rating, ret) == expected


class FakeClient:
    def __init__(self):
        self.calls = []

    def _request(self, method, path, params, *, base_url):
        assert (method, path, base_url) == ("GET", "/fapi/v1/klines", binance._BASE_URLS["live"])
        assert params["interval"] == "1h" and params["startTime"] % feedback.HOUR_MS == 0
        self.calls.append(params)
        if params["symbol"] == "ETHUSDT":
            raise ValueError("Unavailable history")
        return [[start, "100", "110", "90", str(101 + i), "1", start + feedback.HOUR_MS - 1]
                for i in range(params["limit"])
                for start in [params["startTime"] + i * feedback.HOUR_MS]]


def test_score(tmp_path, caplog):
    conn, client, now = db.connect(tmp_path / "bot.db"), FakeClient(), 201 * 3600
    for ticker, age, source, status, rating in [
        ("AAPL", 120 * 3600, "manual", "done", "Buy"),
        ("ETH-USD", 120 * 3600, "manual", "done", "Buy"),
        ("BTC-USD", 120 * 3600, "engine", "done", "Buy"),
        ("SOL-USD", 24 * 3600, "manual", "done", "Sell"),
        ("DOGE-USD", 24 * 3600 - 1801, "manual", "done", "Buy"),
        ("XRP-USD", 120 * 3600, "manual", "failed", "Buy"),
        ("BNB-USD", 120 * 3600, "manual", "done", None),
    ]:
        key = db.enqueue(conn, ticker, "2026-11-04", ["market"], source=source)
        db.update(conn, key, status=status, rating=rating, finished_at=now - age - 1800)
    assert feedback.score(conn, client, now) == 3
    rows = conn.execute("SELECT * FROM outcomes ORDER BY job_id, horizon_h").fetchall()
    assert [(r["symbol"], r["horizon_h"], r["verdict"]) for r in rows] == [
        ("BTCUSDT", 24, "correct"), ("BTCUSDT", 120, "correct"), ("SOLUSDT", 24, "wrong")]
    assert [(r["entry"], r["exit"], r["ret_pct"], r["scored_at"]) for r in rows] == [
        ("100", "124", 24, now), ("100", "220", 120, now), ("100", "124", 24, now)]
    assert feedback.score(conn, client, now) == 0
    assert [p["symbol"] for p in client.calls] == ["ETHUSDT", "BTCUSDT", "SOLUSDT", "ETHUSDT"]
    assert "Job 1: scoring failed" in caplog.text and "Job 2: scoring failed" in caplog.text


@pytest.mark.parametrize("finished_at,end", [(0, 24 * 3600), (1800, 25 * 3600)])
def test_entry_waits_for_next_open_and_full_horizon(tmp_path, finished_at, end):
    conn, client = db.connect(tmp_path / "bot.db"), FakeClient()
    key = db.enqueue(conn, "BTC-USD", "", ["market"])
    db.update(conn, key, status="done", rating="Buy", finished_at=finished_at)
    assert feedback.score(conn, client, end - 1) == 0 and not client.calls
    assert feedback.score(conn, client, end) == 1
    assert client.calls[0]["startTime"] == (end - 24 * 3600) * 1000


def test_candidate_now_and_delayed_windows(tmp_path, monkeypatch):
    conn, client = db.connect(tmp_path / "bot.db"), FakeClient()
    key = db.enqueue(conn, "BTC-USD", "", ["market"])
    db.update(conn, key, status="done", finished_at=5400)
    original = client._request
    def request(*args, **kwargs):
        bars = original(*args, **kwargs)
        for b in bars:
            b[1:5] = ["100", "105", "99", "102"] if b[0] < 7200000 else ["101", "105", "99", "104"]
        return bars
    monkeypatch.setattr(client, "_request", request)
    unfinished = db.enqueue(conn, "BTC-USD", "", ["market"])
    for job_id in (key, None, unfinished):
        conn.execute("INSERT INTO candidates (at,symbol,kind,direction,atr,price,job_id) "
                     "VALUES (1800,'BTCUSDT','daily',1,4,999,?)", (job_id,))
    assert feedback.score(conn, client, 120 * 3600 + 1800) == 0
    feedback.score(conn, client, 121 * 3600)
    feedback.score(conn, client, 122 * 3600)
    rows = conn.execute("SELECT * FROM candidates ORDER BY id").fetchall()
    assert [r["mech_now"] for r in rows] == pytest.approx([.975] * 3)
    assert rows[0]["mech_delay"] == pytest.approx(.72475) and all(r["mech_delay"] is None for r in rows[1:])
    assert [r["scored_at"] for r in rows] == [122 * 3600, 121 * 3600, 121 * 3600]
    assert {p["startTime"] for p in client.calls} == {3600000, 7200000}
    assert feedback.score(conn, client, 123 * 3600) == 0
