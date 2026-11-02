"""Trading-bot control panel. Phase 0: queue analyses, watch each agent work, measure plan usage.

Run it beside the worker:  python -m autotrader.gui   ->  http://<this machine's IP>:8080
"""

import json
import os
import time
from datetime import date, datetime
from pathlib import Path

from nicegui import app, ui

from autotrader import db
from autotrader.board import statuses
from cli.display import ANALYST_AGENT_NAMES, ANALYST_ORDER
from cli.prompts import is_valid_ticker_input
from tradingagents.default_config import DEFAULT_CONFIG

REPORTS_ROOT = Path(DEFAULT_CONFIG["results_dir"]) / "reports"
STATUS_LOOK = {  # status -> (label, Quasar colour)
    "waiting": ("대기", "grey"), "working": ("작업 중", "blue"), "done": ("완료", "green"),
    "error": ("오류", "red"), "queued": ("대기열", "grey"), "running": ("실행 중", "blue"),
    "failed": ("실패", "red"),
}
ALIVE_SECONDS = 20  # the worker beats every 5 s

conn = db.connect()
REPORTS_ROOT.mkdir(parents=True, exist_ok=True)
app.add_static_files("/reports", REPORTS_ROOT)


def _badge(status: str) -> None:
    label, colour = STATUS_LOOK.get(status, (status, "grey"))
    ui.badge(label, color=colour)


def _tail(text: str, size: int = 180) -> str:
    text = " ".join(text.split())
    return text if len(text) <= size else "…" + text[-size:]


def _show_text(title: str, text: str) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        ui.label(title).classes("text-lg font-bold")
        ui.markdown(text or "_아직 내용이 없습니다._").classes("max-h-[70vh] overflow-auto")
        ui.button("닫기", on_click=dialog.close)
    dialog.open()


def _job_rows() -> list[dict]:
    rows = []
    for job in db.recent_jobs(conn):
        seconds = (job["finished_at"] or time.time()) - job["started_at"] if job["started_at"] else None
        rows.append({
            "id": job["id"], "ticker": job["ticker"], "trade_date": job["trade_date"],
            "status": STATUS_LOOK.get(job["status"], (job["status"],))[0], "rating": job["rating"] or "",
            "seconds": f"{seconds:.0f}" if seconds is not None else "",
            "llm_calls": job["llm_calls"] or 0,
            "tokens": (job["tokens_in"] or 0) + (job["tokens_out"] or 0),
            "created": datetime.fromtimestamp(job["created_at"]).strftime("%m-%d %H:%M"),
        })
    return rows


JOB_COLUMNS = [
    {"name": "id", "label": "#", "field": "id"},
    {"name": "ticker", "label": "종목", "field": "ticker"},
    {"name": "trade_date", "label": "기준일", "field": "trade_date"},
    {"name": "status", "label": "상태", "field": "status"},
    {"name": "rating", "label": "최종 의견", "field": "rating"},
    {"name": "seconds", "label": "소요(초)", "field": "seconds"},
    {"name": "llm_calls", "label": "LLM 호출", "field": "llm_calls"},
    {"name": "tokens", "label": "토큰", "field": "tokens"},
    {"name": "created", "label": "등록", "field": "created"},
]


@ui.page("/")
def index() -> None:
    selected = {"job": None}  # None follows the newest job

    ui.label("Trading Bot — 0단계: 분석·측정").classes("text-2xl font-bold")

    @ui.refreshable
    def master_panel() -> None:
        beat = db.heartbeat(conn, "master")
        alive = beat is not None and time.time() - beat["at"] < ALIVE_SECONDS
        usage = db.usage_since(conn, datetime.combine(date.today(), datetime.min.time()).timestamp())
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center gap-3"):
                ui.label("Master").classes("text-lg font-bold")
                _badge("working" if alive else "error")
                ui.label(beat["note"] if alive else "워커가 꺼져 있습니다: python -m autotrader.worker")
            avg = f"{usage['avg_seconds']:.0f}초" if usage["avg_seconds"] else "-"
            ui.label(
                f"오늘: 분석 {usage['done'] or 0}건 완료 · 실패 {usage['failed'] or 0} · 대기 {usage['pending'] or 0}"
                f" · LLM 호출 {usage['llm_calls']} · 토큰 {usage['tokens_in'] + usage['tokens_out']:,}"
                f" · 평균 {avg}"
            ).classes("text-sm text-gray-600")

    with ui.card().classes("w-full"):
        ui.label("새 분석").classes("text-lg font-bold")
        with ui.row().classes("items-end gap-4 w-full"):
            tickers = ui.input("종목 (쉼표로 구분)", value="BTC-USD, ETH-USD").classes("w-80")
            trade_date = ui.input("기준일 (YYYY-MM-DD)", value=date.today().isoformat()).classes("w-44")
            checks = {key: ui.checkbox(ANALYST_AGENT_NAMES[key], value=key != "fundamentals")
                      for key in ANALYST_ORDER}

            def add_jobs() -> None:
                symbols = [t.strip().upper() for t in tickers.value.split(",") if t.strip()]
                analysts = [key for key, box in checks.items() if box.value]
                try:
                    date.fromisoformat(trade_date.value.strip())
                except ValueError:
                    ui.notify("기준일은 YYYY-MM-DD 형식이어야 합니다", type="negative")
                    return
                bad = [s for s in symbols if not is_valid_ticker_input(s)]
                if not symbols or bad or not analysts:
                    ui.notify(f"종목과 분석가를 확인하세요 {bad or ''}", type="negative")
                    return
                for symbol in symbols:
                    db.enqueue(conn, symbol, trade_date.value.strip(), analysts)
                selected["job"] = None
                ui.notify(f"{len(symbols)}건을 대기열에 추가했습니다 (코인은 펀더멘털 분석가가 자동 제외됩니다)")
                refresh()

            ui.button("대기열에 추가", on_click=add_jobs)

    master_panel()

    @ui.refreshable
    def agent_board() -> None:
        job = db.get_job(conn, selected["job"]) if selected["job"] else next(iter(db.recent_jobs(conn, 1)), None)
        if job is None:
            ui.label("아직 분석이 없습니다. 위에서 종목을 추가하세요.")
            return
        with ui.row().classes("items-center gap-3"):
            ui.label(f"#{job['id']} {job['ticker']} · {job['trade_date']}").classes("text-lg font-bold")
            _badge(job["status"])
            if job["rating"]:
                ui.badge(f"최종 의견: {job['rating']}", color="purple")
            if job["report_dir"] and (Path(job["report_dir"]) / "complete_report.html").exists():
                ui.link("HTML 리포트", f"/reports/{Path(job['report_dir']).name}/complete_report.html",
                        new_tab=True)
        if job["error"]:
            ui.label(f"오류: {job['error']}").classes("text-red-600 text-sm")
        board = json.loads(job["board"]) if job["board"] else []
        n_analysts = sum(name in ANALYST_AGENT_NAMES.values() for name, _ in board)
        with ui.row().classes("gap-3"):
            for (name, text), status in zip(board, statuses(board, n_analysts, job["status"]), strict=True):
                with ui.card().classes("w-72 cursor-pointer").on("click", lambda n=name, t=text: _show_text(n, t)):
                    with ui.row().classes("items-center justify-between w-full"):
                        ui.label(name).classes("font-bold")
                        _badge(status)
                    ui.label(_tail(text) if text else "").classes("text-xs text-gray-600")

    with ui.card().classes("w-full"):
        ui.label("에이전트 보드 (카드를 누르면 전체 내용)").classes("text-lg font-bold")
        agent_board()

    def pick(event) -> None:
        selected["job"] = event.args[1]["id"]
        agent_board.refresh()

    with ui.card().classes("w-full"):
        ui.label("분석 기록 (행을 누르면 보드에 표시)").classes("text-lg font-bold")
        table = ui.table(rows=_job_rows(), columns=JOB_COLUMNS, row_key="id", pagination=15).classes("w-full")
        table.on("rowClick", pick)

    def refresh() -> None:
        master_panel.refresh()
        agent_board.refresh()
        table.rows = _job_rows()
        table.update()

    ui.timer(2.0, refresh)


def main() -> None:
    # shortcut: no login, so anyone on the LAN can queue analyses; add auth before phase 1 puts trading controls here.
    # TRADING_BOT_HOST=127.0.0.1 keeps it on this machine only.
    host = os.environ.get("TRADING_BOT_HOST", "0.0.0.0")
    ui.run(host=host, port=8080, title="Trading Bot", reload=False, show=False, language="ko-KR")


if __name__ in {"__main__", "__mp_main__"}:
    main()
