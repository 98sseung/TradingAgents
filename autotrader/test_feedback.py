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
    conn, client, now = db.connect(tmp_path / "bot.db"), FakeClient(), 200 * 3600 + 1800
    for ticker, age, source, status, rating in [
        ("AAPL", 120 * 3600, "manual", "done", "Buy"),
        ("ETH-USD", 120 * 3600, "manual", "done", "Buy"),
        ("BTC-USD", 120 * 3600, "engine", "done", "Buy"),
        ("SOL-USD", 24 * 3600, "manual", "done", "Sell"),
        ("DOGE-USD", 24 * 3600 - 1, "manual", "done", "Buy"),
        ("XRP-USD", 120 * 3600, "manual", "failed", "Buy"),
        ("BNB-USD", 120 * 3600, "manual", "done", None),
    ]:
        key = db.enqueue(conn, ticker, "2026-11-04", ["market"], source=source)
        db.update(conn, key, status=status, rating=rating, finished_at=now - age)
    assert feedback.score(conn, client, now) == 3
    rows = conn.execute("SELECT * FROM outcomes ORDER BY job_id, horizon_h").fetchall()
    assert [(r["symbol"], r["horizon_h"], r["verdict"]) for r in rows] == [
        ("BTCUSDT", 24, "correct"), ("BTCUSDT", 120, "correct"), ("SOLUSDT", 24, "wrong")]
    assert [(r["entry"], r["exit"], r["ret_pct"], r["scored_at"]) for r in rows] == [
        ("100", "124", 24, now), ("100", "220", 120, now), ("100", "124", 24, now)]
    assert feedback.score(conn, client, now) == 0
    assert [p["symbol"] for p in client.calls] == ["ETHUSDT", "BTCUSDT", "SOLUSDT", "ETHUSDT"]
    assert "Job 1: scoring failed" in caplog.text and "Job 2: scoring failed" in caplog.text
