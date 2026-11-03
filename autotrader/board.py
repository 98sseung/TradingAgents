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


# The pipeline columns the GUI draws: (title, icon, the agents in it, each with a one-word role).
STAGES = [
    ("분석", "query_stats", {"Market Analyst": "기술적 분석", "Sentiment Analyst": "여론",
                            "News Analyst": "뉴스·거시", "Fundamentals Analyst": "재무"}),
    ("리서치", "forum", {"Bull Researcher": "강세 논거", "Bear Researcher": "약세 논거",
                        "Research Manager": "투자 계획"}),
    ("트레이딩", "swap_horiz", {"Trader": "매매 제안"}),
    ("리스크", "shield", {"Aggressive Analyst": "공격적", "Conservative Analyst": "보수적",
                         "Neutral Analyst": "중립"}),
    ("결정", "gavel", {"Portfolio Manager": "최종 결정"}),
]


def by_stage(board: list[list[str]], status_list: list[str]) -> list[tuple[str, str, list[tuple[str, str, str, str]]]]:
    """Board rows grouped into STAGES: [(title, icon, [(name, role, text, status), ...]), ...]."""
    rows = {name: (text, status) for (name, text), status in zip(board, status_list, strict=True)}
    return [(title, icon, [(name, role, *rows[name]) for name, role in agents.items() if name in rows])
            for title, icon, agents in STAGES]


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
