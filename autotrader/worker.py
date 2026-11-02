"""Analysis worker: runs queued jobs through TradingAgents one at a time and records what each agent says.

Run it beside the GUI:  python -m autotrader.worker
LLM settings come from .env (TRADINGAGENTS_* variables), as for the CLI.
"""

import json
import logging
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
current = {"note": "idle"}  # what the heartbeat reports


def _heartbeat_loop() -> None:
    # Its own connection and thread, so the GUI sees the worker alive during a minutes-long LLM call.
    conn = db.connect()
    while True:
        db.beat(conn, "master", current["note"])
        time.sleep(5)


def run_job(conn, job) -> None:
    asset_type = detect_asset_type(job["ticker"])
    analysts = [a.value for a in filter_analysts_for_asset_type(
        [AnalystType(a) for a in job["analysts"].split(",")], asset_type)]
    if not analysts:
        raise ValueError(f"no analyst selected that can cover {asset_type.value}")

    stats = StatsCallbackHandler()
    graph = TradingAgentsGraph(analysts, config=DEFAULT_CONFIG.copy(), callbacks=[stats])
    init_state = graph.create_run_state(job["ticker"], job["trade_date"], asset_type.value)

    final_state: dict = {}
    for _messages, chunk in graph.stream_run(init_state, **graph.propagator.get_graph_args(callbacks=[stats])):
        if chunk:
            final_state.update(chunk)
        db.update(conn, job["id"], board=json.dumps(board_texts(final_state, analysts)), **stats.get_stats())

    graph.record_decision(job["ticker"], job["trade_date"], final_state)
    report_dir = graph.default_report_path(job["ticker"])
    graph.save_reports(final_state, job["ticker"], report_dir)
    db.update(conn, job["id"], status="done", finished_at=time.time(),
              rating=run_rating(final_state), report_dir=str(report_dir), **stats.get_stats())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = db.connect()
    if orphans := db.fail_orphans(conn):
        log.warning("marked %d job(s) left running by a previous worker as failed", orphans)
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
