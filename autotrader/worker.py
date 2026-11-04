"""Analysis worker: runs queued jobs through TradingAgents one at a time and records what each agent says.

Run it beside the GUI:  python -m autotrader.worker
LLM settings come from .env (TRADINGAGENTS_* variables), as for the CLI.
"""

import json
import logging
import os
import re
import sqlite3
import threading
import time

from autotrader import db
from autotrader.board import board_texts
from cli.models import AnalystType
from cli.prompts import detect_asset_type, filter_analysts_for_asset_type
from cli.stats_handler import StatsCallbackHandler
from tradingagents.agents.rating import run_rating
from tradingagents.dataflows.files import locked
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

log = logging.getLogger("autotrader.worker")
# The proposal and decision are rendered by tradingagents/agents/schemas.py as "**Stop Loss**: 2219.09".
_LEVEL = r"\*\*{}\*\*:\s*\$?([0-9][0-9,]*\.?[0-9]*)"
current = {"note": "idle"}  # what the heartbeat reports


def level(text: str, label: str) -> float | None:
    """A price the rendered proposal or decision states for ``label``; None when 'not provided'."""
    found = re.search(_LEVEL.format(re.escape(label)), text or "")
    return float(found.group(1).replace(",", "")) if found else None


def worker_name() -> str:
    name = os.environ.get("TRADING_BOT_WORKER_NAME", "worker-1")
    if not re.fullmatch(r"worker-[1-9][0-9]*", name):
        raise ValueError("TRADING_BOT_WORKER_NAME must be worker-<positive integer>")
    return name


def _heartbeat_loop(name, stop) -> None:
    # Its own connection keeps the heartbeat alive during a long LLM call.
    conn = db.connect()
    try:
        while not stop.is_set():
            try:
                db.beat(conn, name, current["note"])
            except sqlite3.Error:
                log.exception("heartbeat write failed")
            stop.wait(5)
    finally:
        conn.close()


def run_job(conn, job) -> None:
    ticker = job["ticker"]  # keep the checkpoint identity used by earlier worker runs
    asset_type = detect_asset_type(ticker)
    analysts = [a.value for a in filter_analysts_for_asset_type(
        [AnalystType(a) for a in job["analysts"].split(",")], asset_type)]
    if not analysts:
        raise ValueError(f"no analyst selected that can cover {asset_type.value}")

    stats = StatsCallbackHandler()
    # Checkpointed, so a job requeued after a worker crash resumes at its last finished agent instead of re-paying for it.
    graph = TradingAgentsGraph(analysts, config={**DEFAULT_CONFIG, "checkpoint_enabled": True}, callbacks=[stats])
    run = (ticker, job["trade_date"], asset_type.value)
    init_state = graph.create_run_state(*run)
    args = graph.propagator.get_graph_args(callbacks=[stats])
    args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = graph.begin_checkpoint(*run)

    final_state: dict = {}
    try:
        for _messages, chunk in graph.stream_run(graph.checkpoint_input(init_state), **args):
            if chunk:
                final_state.update(chunk)
            if not db.update(conn, job["id"], owner=job["worker"],
                             board=json.dumps(board_texts(final_state, analysts)), **stats.get_stats()):
                raise RuntimeError("job ownership lost")

        graph.record_decision(ticker, job["trade_date"], final_state)
        graph.clear_checkpoint_on_success(*run)
    finally:
        graph.end_checkpoint()
        db.update(conn, job["id"], owner=job["worker"], **stats.get_stats())
    report_dir = graph.default_report_path(ticker)
    graph.save_reports(final_state, ticker, report_dir)
    db.update(conn, job["id"], owner=job["worker"], status="done", finished_at=time.time(),
              rating=run_rating(final_state), report_dir=str(report_dir), **stats.get_stats(),
              stop_loss=level(final_state.get("trader_investment_plan"), "Stop Loss"),
              price_target=level(final_state.get("final_trade_decision"), "Price Target"))


def _rate_limited(exc: Exception) -> bool:
    pending, seen = [exc], set()
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        response = getattr(error, "response", None)
        if (any(cls.__name__.endswith("RateLimitError") for cls in type(error).__mro__)
                or getattr(error, "status_code", None) == 429
                or getattr(error, "code", None) == 429
                or getattr(response, "status_code", None) == 429
                or getattr(response, "status", None) == 429
                or re.search(
                    r"\brate[\s_-]*limit|\bHTTP\s+429\b|\b429\s+(?:Client Error|Too Many Requests)\b",
                    str(error), re.IGNORECASE)):
            return True
        pending.extend(cause for cause in (error.__cause__, error.__context__) if cause is not None)
    return False


def process_job(conn, job) -> None:
    started, status = time.monotonic(), "done"
    try:
        run_job(conn, job)
    except Exception as exc:
        status = "failed"
        log.exception("job #%s failed", job["id"])
        limited = _rate_limited(exc)
        error = f"{type(exc).__name__}: {exc}"
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = db.update(conn, job["id"], owner=job["worker"], status="failed", finished_at=time.time(),
                                error=error[:2000])
            if changed and limited:
                message = f"rate limited: job {job['id']} {job['ticker']}: " + " ".join(str(exc).split())[:160]
                conn.execute("INSERT INTO events (at, level, source, message) VALUES (?, 'warn', ?, ?)",
                             (time.time(), job["worker"], message))
    finally:
        finished = db.get_job(conn, job["id"])
        log.info("job %s %s %s in %.1fs llm=%s", job["id"], job["ticker"], status,
                 time.monotonic() - started, finished["llm_calls"] or 0)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    name = worker_name()
    conn = db.connect()
    stop = threading.Event()
    # A restarted name owns its old jobs; prevent two live processes using that same name.
    with locked(db.DB_PATH.parent / name, wait=False) as acquired:
        if not acquired:
            conn.close()
            raise RuntimeError(f"{name} is already running")
        current["note"] = "idle"
        db.beat(conn, name, current["note"])
        if orphans := db.requeue_orphans(conn, name):
            log.warning("requeued %d job(s) a dead worker left running; they resume from their checkpoint", orphans)
        heartbeat = threading.Thread(target=_heartbeat_loop, args=(name, stop), daemon=True)
        heartbeat.start()
        last_sweep = time.monotonic()
        log.info("%s ready; db at %s", name, db.DB_PATH)
        try:
            while True:
                # Sweep between jobs so our active analysis is never requeued as an orphan.
                if time.monotonic() - last_sweep >= 30:
                    if orphans := db.requeue_orphans(conn, name):
                        log.warning("requeued %d orphaned job(s); they resume from their checkpoint", orphans)
                    last_sweep = time.monotonic()
                job = db.claim_next(conn, name)
                if job is None:
                    current["note"] = "idle"
                    time.sleep(2)
                    continue
                current["note"] = f"#{job['id']} {job['ticker']} {job['trade_date']}"
                log.info("running job %s", current["note"])
                try:
                    process_job(conn, job)
                finally:
                    current["note"] = "idle"
        finally:
            stop.set()
            heartbeat.join(timeout=2)
            conn.close()


if __name__ == "__main__":
    main()
