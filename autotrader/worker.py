"""Analysis worker: runs queued jobs through TradingAgents one at a time and records what each agent says.

Run it beside the GUI:  python -m autotrader.worker
LLM settings come from .env (TRADINGAGENTS_* variables), as for the CLI.
"""

import json
import logging
import re
import threading
import time

from autotrader import db
from autotrader.board import board_texts
from cli.models import AnalystType
from cli.prompts import detect_asset_type, filter_analysts_for_asset_type
from cli.stats_handler import StatsCallbackHandler
from tradingagents.agents.rating import run_rating
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

log = logging.getLogger("autotrader.worker")
# The proposal and decision are rendered by tradingagents/agents/schemas.py as "**Stop Loss**: 2219.09".
_LEVEL = r"\*\*{}\*\*:\s*\$?([0-9][0-9,]*\.?[0-9]*)"


def level(text: str, label: str) -> float | None:
    """A price the rendered proposal or decision states for ``label``; None when 'not provided'."""
    found = re.search(_LEVEL.format(re.escape(label)), text or "")
    return float(found.group(1).replace(",", "")) if found else None
current = {"note": "idle"}  # what the heartbeat reports


def _heartbeat_loop() -> None:
    # Its own connection and thread, so the GUI sees the worker alive during a minutes-long LLM call.
    conn = db.connect()
    while True:
        db.beat(conn, "worker", current["note"])
        time.sleep(5)


def run_job(conn, job) -> None:
    asset_type = detect_asset_type(job["ticker"])
    analysts = [a.value for a in filter_analysts_for_asset_type(
        [AnalystType(a) for a in job["analysts"].split(",")], asset_type)]
    if not analysts:
        raise ValueError(f"no analyst selected that can cover {asset_type.value}")

    stats = StatsCallbackHandler()
    # Checkpointed, so a job requeued after a worker crash resumes at its last finished agent instead of re-paying for it.
    graph = TradingAgentsGraph(analysts, config={**DEFAULT_CONFIG, "checkpoint_enabled": True}, callbacks=[stats])
    run = (job["ticker"], job["trade_date"], asset_type.value)
    init_state = graph.create_run_state(*run)
    args = graph.propagator.get_graph_args(callbacks=[stats])
    args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = graph.begin_checkpoint(*run)

    final_state: dict = {}
    try:
        for _messages, chunk in graph.stream_run(graph.checkpoint_input(init_state), **args):
            if chunk:
                final_state.update(chunk)
            db.update(conn, job["id"], board=json.dumps(board_texts(final_state, analysts)), **stats.get_stats())

        graph.record_decision(job["ticker"], job["trade_date"], final_state)
        graph.clear_checkpoint_on_success(*run)
    finally:
        graph.end_checkpoint()
    report_dir = graph.default_report_path(job["ticker"])
    graph.save_reports(final_state, job["ticker"], report_dir)
    db.update(conn, job["id"], status="done", finished_at=time.time(),
              rating=run_rating(final_state), report_dir=str(report_dir), **stats.get_stats(),
              stop_loss=level(final_state.get("trader_investment_plan"), "Stop Loss"),
              price_target=level(final_state.get("final_trade_decision"), "Price Target"))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = db.connect()
    if orphans := db.requeue_orphans(conn):
        log.warning("requeued %d job(s) a previous worker left running; they resume from their checkpoint", orphans)
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    log.info("worker ready; db at %s", db.DB_PATH)

    while True:
        job = db.claim_next(conn)
        if job is None:
            current["note"] = "idle"
            time.sleep(2)
            continue
        current["note"] = f"#{job['id']} {job['ticker']} {job['trade_date']}"
        log.info("running job %s", current["note"])
        try:
            run_job(conn, job)
            log.info("job #%s done", job["id"])
        except Exception as exc:  # one bad run (rate limit, data outage) must not stop the queue
            log.exception("job #%s failed", job["id"])
            db.update(conn, job["id"], status="failed", finished_at=time.time(),
                      error=f"{type(exc).__name__}: {exc}"[:2000])


if __name__ == "__main__":
    main()
