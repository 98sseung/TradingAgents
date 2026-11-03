"""Run: python -m pytest autotrader -q"""

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
    assert level("", "Stop Loss") is None


def test_jobs_are_claimed_once_and_orphans_resume_then_fail(tmp_path):
    conn = db.connect(tmp_path / "bot.db")
    first = db.enqueue(conn, "BTC-USD", "2026-11-03", ["market"])
    db.enqueue(conn, "ETH-USD", "2026-11-03", ["market"])
    assert db.claim_next(conn)["id"] == first
    assert db.claim_next(conn)["ticker"] == "ETH-USD"
    assert db.claim_next(conn) is None
    # worker died: both resume once
    assert db.requeue_orphans(conn) == 2
    assert db.claim_next(conn)["attempts"] == 2
    # died again on the first one: it is failed, not requeued
    assert db.requeue_orphans(conn) == 0
    usage = db.usage_since(conn, 0)
    assert (usage["failed"], usage["pending"]) == (1, 1)  # ETH still waits in the queue
