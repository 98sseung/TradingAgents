"""The agent board: what each agent has said so far, read off a run's merged state."""

from cli.display import ANALYST_AGENT_NAMES, ANALYST_ORDER, ANALYST_REPORT_MAP


def _debate(state: dict, key: str, field: str) -> str:
    return (state.get(key) or {}).get(field, "")


# After the analysts, the graph runs these in order.
LATER_STAGES = [
    ("Bull Researcher", lambda s: _debate(s, "investment_debate_state", "bull_history")),
    ("Bear Researcher", lambda s: _debate(s, "investment_debate_state", "bear_history")),
    ("Research Manager", lambda s: s.get("investment_plan", "")),
    ("Trader", lambda s: s.get("trader_investment_plan", "")),
    ("Aggressive Analyst", lambda s: _debate(s, "risk_debate_state", "aggressive_history")),
    ("Conservative Analyst", lambda s: _debate(s, "risk_debate_state", "conservative_history")),
    ("Neutral Analyst", lambda s: _debate(s, "risk_debate_state", "neutral_history")),
    ("Portfolio Manager", lambda s: s.get("final_trade_decision", "")),
]


def board_texts(state: dict, analysts: list[str]) -> list[list[str]]:
    """[[agent name, text so far], ...] for the selected analysts, then every later stage."""
    rows = [[ANALYST_AGENT_NAMES[a], state.get(ANALYST_REPORT_MAP[a]) or ""]
            for a in ANALYST_ORDER if a in analysts]
    return rows + [[name, read(state) or ""] for name, read in LATER_STAGES]


def statuses(board: list[list[str]], n_analysts: int, job_status: str) -> list[str]:
    """One of waiting | working | done | error per board row.

    The analysts work together, so every silent one is working; after them the
    first silent stage is the one working. A failed job marks that one error.
    """
    active = "working" if job_status == "running" else "error" if job_status == "failed" else "waiting"
    result = []
    for i, (_, text) in enumerate(board):
        if text:
            result.append("done")
        elif i < n_analysts or all(text for _, text in board[:i]):
            result.append(active)
        else:
            result.append("waiting")
    return result
