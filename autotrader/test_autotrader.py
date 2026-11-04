"""Run: python -m pytest autotrader -q"""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from autotrader import db
from autotrader.board import board_texts, by_stage, statuses


def test_board_follows_the_pipeline():
    state = {"market_report": "m", "sentiment_report": "",
             "investment_debate_state": {"bull_history": "bull", "bear_history": ""}}
    board = board_texts(state, ["market", "social"])
    assert [name for name, _ in board][:4] == ["Market Analyst", "Sentiment Analyst", "Bull Researcher", "Bear Researcher"]
    # sentiment still silent: it is working, nothing after it has started
    assert statuses(board, 2, "running")[:4] == ["done", "working", "done", "waiting"]

    state["sentiment_report"] = "s"
    board = board_texts(state, ["market", "social"])
    assert statuses(board, 2, "running")[:5] == ["done", "done", "done", "working", "waiting"]
    assert statuses(board, 2, "failed")[3] == "error"

    stages = by_stage(board, statuses(board, 2, "running"))
    assert [title for title, _, _ in stages] == ["분석", "리서치", "트레이딩", "리스크", "결정"]
    assert [name for name, *_ in stages[0][2]] == ["Market Analyst", "Sentiment Analyst"]  # only selected analysts
    assert stages[1][2][1][3] == "working"  # Bear Researcher
    assert sum(len(agents) for _, _, agents in stages) == len(board)  # every row lands in a column


def test_levels_are_read_from_rendered_proposals():
    from autotrader.worker import level
    plan = "**Action**: Sell\n\n**Entry Price**: not provided\n\n**Stop Loss**: 2219.09\n"
    assert level(plan, "Stop Loss") == 2219.09
    assert level(plan, "Entry Price") is None
    assert level("**Price Target**: $78,250.5", "Price Target") == 78250.5
    assert level("**Stop Loss**: 1e-05", "Stop Loss") == 0.00001
    assert level("**Stop Loss**: $7,825.05E+2", "Stop Loss") == 782505
    assert level("", "Stop Loss") is None


def test_jobs_are_claimed_once_and_orphans_resume_then_fail(tmp_path):
    conn = db.connect(tmp_path / "bot.db")
    first = db.enqueue(conn, "BTC-USD", "2026-11-03", ["market"])
    db.enqueue(conn, "ETH-USD", "2026-11-03", ["market"])
    assert db.claim_next(conn, "worker-1")["id"] == first
    assert db.claim_next(conn, "worker-1")["ticker"] == "ETH-USD"
    assert db.claim_next(conn, "worker-1") is None
    # worker died: both resume once
    assert db.requeue_orphans(conn, "worker-1") == 2
    assert db.claim_next(conn, "worker-1")["attempts"] == 2
    # died again on the first one: it is failed, not requeued
    assert db.requeue_orphans(conn, "worker-1") == 0
    usage = db.usage_since(conn, 0)
    assert (usage["failed"], usage["pending"]) == (1, 1)  # ETH still waits in the queue


def test_existing_database_gains_nullable_worker_column(tmp_path):
    path = tmp_path / "bot.db"
    with sqlite3.connect(path) as old:
        old.executescript(db.SCHEMA)
        old.execute("INSERT INTO jobs (ticker, trade_date, analysts, created_at) VALUES ('BTC-USD', '', 'market', 0)")
    conn = db.connect(path)
    assert db.get_job(conn, 1)["worker"] is None
    assert db.claim_next(conn, "worker-2")["worker"] == "worker-2"


def test_two_workers_claim_distinct_jobs_and_wait_for_normalized_ticker(tmp_path):
    conn, other = db.connect(tmp_path / "bot.db"), db.connect(tmp_path / "bot.db")
    first = db.enqueue(conn, "BTC-USD", "2026-11-03", ["market"])
    alias = db.enqueue(conn, "BTC-USDT", "2026-11-04", ["market"])
    unrelated = db.enqueue(conn, "ETH-USD", "2026-11-03", ["market"])
    job = db.claim_next(conn, "worker-1")
    assert (job["id"], job["worker"], job["attempts"]) == (first, "worker-1", 1)
    assert job["started_at"] is not None
    assert db.claim_next(other, "worker-2")["id"] == unrelated
    assert db.claim_next(conn, "worker-1") is None and db.claim_next(other, "worker-2") is None
    queued = db.get_job(conn, alias)
    assert (queued["status"], queued["worker"], queued["attempts"]) == ("queued", None, 0)
    assert not conn.in_transaction and not other.in_transaction
    db.update(conn, first, status="done")
    assert db.claim_next(other, "worker-2")["id"] == alias


@pytest.mark.parametrize("owner, age, recover", [("worker-1", 0, False), ("worker-1", 61, True),
                                              ("worker-1", None, True), ("worker-2", 0, True), (None, None, False)])
def test_orphans_respect_liveness_and_restarted_ownership(tmp_path, monkeypatch, owner, age, recover):
    monkeypatch.setattr(db.time, "time", lambda: 1000)
    conn, other = db.connect(tmp_path / "bot.db"), db.connect(tmp_path / "bot.db")
    job_id = db.enqueue(conn, "BTC-USD", "2026-11-03", ["market"])
    db.claim_next(conn, owner or "worker-1")
    db.update(conn, job_id, worker=owner, board="saved")
    db.beat(conn, "worker", "idle")  # a live legacy worker still protects its unowned job
    if age is not None:
        conn.execute("INSERT INTO heartbeat VALUES (?, ?, 'idle')", (owner, 1000 - age))
    assert db.requeue_orphans(other, "worker-2") == int(recover)
    job = db.get_job(conn, job_id)
    assert (job["status"], job["worker"]) == (("queued", None) if recover else ("running", owner))
    assert job["board"] == "saved" and job["attempts"] == 1
    assert (job["started_at"] is None) == recover


@pytest.mark.parametrize("kind", ["http", "class", "message", "chain", "ordinary"])
def test_rate_limit_failure_keeps_error_and_adds_exactly_one_worker_event(tmp_path, monkeypatch, kind):
    from autotrader import worker
    conn = db.connect(tmp_path / "bot.db")
    db.enqueue(conn, "BTC-USD", "2026-11-03", ["market"])
    job = db.claim_next(conn, "worker-2")
    error = RuntimeError("rate limit exceeded" if kind == "message" else "upstream unavailable")
    if kind == "http":
        error.response = SimpleNamespace(status_code=429)
    elif kind == "class":
        error = type("RateLimitError", (Exception,), {})("try later")
    elif kind == "chain":
        error.__cause__ = RuntimeError("HTTP 429")

    def fail(conn, job):
        raise error

    monkeypatch.setattr(worker, "run_job", fail)
    worker.process_job(conn, job)
    failed = db.get_job(conn, job["id"])
    assert failed["status"] == "failed" and failed["finished_at"] is not None
    assert failed["error"] == f"{type(error).__name__}: {error}"
    events = conn.execute("SELECT * FROM events").fetchall()
    assert [(row["level"], row["source"]) for row in events] == ([] if kind == "ordinary" else [("warn", "worker-2")])


@pytest.mark.parametrize("name", [None, "worker-3"])
def test_worker_name_heartbeat_and_periodic_orphan_sweep(tmp_path, monkeypatch, name):
    from autotrader import worker
    conn = db.connect(tmp_path / "bot.db")
    monkeypatch.delenv("TRADING_BOT_WORKER_NAME", raising=False)
    if name:
        monkeypatch.setenv("TRADING_BOT_WORKER_NAME", name)
    expected, sweeps, clock = name or "worker-1", [], [0]
    connect, sweep = db.connect, db.requeue_orphans
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bot.db")
    monkeypatch.setattr(db, "connect", lambda: connect(tmp_path / "bot.db"))
    monkeypatch.setattr(db, "requeue_orphans", lambda conn, who: sweeps.append(who) or sweep(conn, who))
    monkeypatch.setattr(worker.threading, "Thread", lambda **kwargs: SimpleNamespace(
        start=lambda: None, join=lambda timeout: None))
    def finish(conn, job):
        assert worker.current["note"] == f"#{job['id']} BTC-USD 2026-11-03"
        assert job["worker"] == expected
        db.update(conn, job["id"], status="done")
        clock[0] = 31
    def stop(seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(worker, "run_job", finish)
    monkeypatch.setattr(worker, "time", SimpleNamespace(time=db.time.time, monotonic=lambda: clock[0], sleep=stop))
    job_id = db.enqueue(conn, "BTC-USD", "2026-11-03", ["market"])
    with pytest.raises(KeyboardInterrupt):
        worker.main()
    assert sweeps == [expected, expected] and worker.current["note"] == "idle"
    assert db.heartbeat(conn, expected)["note"] == "idle" and db.get_job(conn, job_id)["status"] == "done"


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("shock", [False, True])
def test_checkpoint_identity_and_resume_are_preserved(tmp_path, monkeypatch, interrupted, shock):
    from autotrader import worker
    conn, calls, handlers = db.connect(tmp_path / "bot.db"), [], []
    facts = {"direction": -1, "atr": 4, "ratio": -2, "price": 100, "detected_at": 0, "btc_move_pct": 1.25}
    db.enqueue(conn, "BTC-USDT", "2026-11-03", ["market"], shock=json.dumps(facts) if shock else None)
    monkeypatch.setattr(worker, "BinanceClient", lambda: SimpleNamespace(
        klines=lambda *args: [[0, "1", "110", "90", "100", 0, 1]] * 6 + [[0, 0, 0, 0, 0, 0, float("inf")]],
        _session=SimpleNamespace(close=lambda: None)))
    job = db.claim_next(conn, "worker-1")
    def stream(state, **args):
        assert state is None and args["config"]["configurable"]["thread_id"] == "saved-thread"
        handlers[0].on_llm_start({}, [])
        yield None, {"market_report": "saved report"}
        if interrupted:
            handlers[0].on_llm_start({}, [])
            raise RuntimeError("interrupted")

    def graph(analysts, config, callbacks):
        assert config["checkpoint_enabled"] is True
        handlers.extend(callbacks)
        return SimpleNamespace(
            create_run_state=lambda *run, portfolio: calls.append(("create", (*run, portfolio))) or {},
            propagator=SimpleNamespace(get_graph_args=lambda **kwargs: {}),
            begin_checkpoint=lambda *run, portfolio: calls.append(("begin", (*run, portfolio))) or "saved-thread",
            checkpoint_input=lambda state: None, stream_run=stream, record_decision=lambda *args: None,
            clear_checkpoint_on_success=lambda *run, portfolio: calls.append(("clear", (*run, portfolio))),
            end_checkpoint=lambda: calls.append(("end", None)),
            default_report_path=lambda ticker: tmp_path / ticker, save_reports=lambda *args: None)
    monkeypatch.setattr(worker, "TradingAgentsGraph", graph)
    monkeypatch.setattr(worker, "run_rating", lambda state: "Hold")
    if interrupted:
        with pytest.raises(RuntimeError, match="interrupted"):
            worker.run_job(conn, job)
    else:
        worker.run_job(conn, job)
        assert db.get_job(conn, job["id"])["status"] == "done"
    assert calls[0][0] == "create" and calls[1][0] == "begin" and calls[-1] == ("end", None)
    portfolio = calls[0][1][-1]
    assert all(args[:-1] == ("BTC-USDT", "2026-11-03", "crypto") and args[-1] is portfolio
               for name, args in calls if name != "end")
    mandate = portfolio.render("BTC-USDT")
    assert "opens a short" in mandate and "5 days" in mandate
    assert portfolio.fingerprint() != worker.TradingMandate().fingerprint() if shock else not portfolio.mandate_text
    if shock:
        assert all(text in mandate for text in ("1970-01-01 09:00 KST", "-7.41%", "-2.00 daily ATRs", "down/short", "+1.25%"))
        assert mandate.count("high 110, low 90, close 100") == 6
    assert any(name == "clear" for name, _ in calls) == (not interrupted)
    assert "saved report" in db.get_job(conn, job["id"])["board"]
    assert db.get_job(conn, job["id"])["llm_calls"] == (2 if interrupted else 1)
