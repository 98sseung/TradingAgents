"""Simultaneous local queue claims; no analysis or provider calls."""

import multiprocessing
from pathlib import Path

import pytest

from autotrader import db


def _claim(path, name, barrier, results):
    conn = db.connect(Path(path))
    try:
        barrier.wait(timeout=15)
        job = db.claim_next(conn, name)
        results.put((job["id"], job["worker"]) if job else None)
    finally:
        conn.close()


def test_processes_never_claim_the_same_job_or_instrument(tmp_path):
    path = tmp_path / "bot.db"
    conn = db.connect(path)
    first = db.enqueue(conn, "BTC-USD", "2026-11-03", ["market"])
    alias = db.enqueue(conn, "BTCUSDT", "2026-11-03", ["market"])
    other = db.enqueue(conn, "ETH-USD", "2026-11-03", ["market"])
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    children = [ctx.Process(target=_claim, args=(str(path), f"worker-{n}", barrier, results)) for n in (1, 2)]
    try:
        for child in children:
            child.start()
        claimed = [results.get(timeout=20) for _ in children]
        for child in children:
            child.join(timeout=20)
            assert child.exitcode == 0
        assert {row[0] for row in claimed} == {first, other}
        assert {row[1] for row in claimed} == {"worker-1", "worker-2"}
        db.update(conn, first, status="done")
        assert db.claim_next(conn, "worker-3")["id"] == alias
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
        results.close()
        conn.close()


@pytest.mark.parametrize("ticker,alias", [("1000PEPE-USD", "1000PEPEUSDT"), ("XAUUSD+", "GC=F")])
def test_aliases_wait_and_previous_owners_cannot_finish_reassigned_jobs(tmp_path, ticker, alias):
    conn = db.connect(tmp_path / "bot.db")
    first = db.enqueue(conn, ticker, "2026-11-03", ["market"])
    second = db.enqueue(conn, alias, "2026-11-03", ["market"])
    db.claim_next(conn, "worker-1")
    assert db.claim_next(conn, "worker-2") is None
    assert db.requeue_orphans(conn, "worker-2") == 1
    assert db.claim_next(conn, "worker-2")["id"] == first
    assert db.update(conn, first, owner="worker-1", status="done") == 0
    assert db.update(conn, first, owner="worker-2", status="done") == 1
    assert db.claim_next(conn, "worker-3")["id"] == second
