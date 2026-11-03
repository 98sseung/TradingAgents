"""Trading-bot control panel. Phase 0: queue analyses, watch each agent work, measure plan usage.

Run it beside the worker:  python -m autotrader.gui   ->  http://<this machine's IP>:8080
Look and colours: theme.css.
"""

import json
import os
import time
from datetime import date, datetime
from pathlib import Path

from nicegui import app, ui

from autotrader import db
from autotrader.board import by_stage, statuses
from cli.display import ANALYST_AGENT_NAMES, ANALYST_ORDER
from cli.prompts import is_valid_ticker_input
from tradingagents.default_config import DEFAULT_CONFIG

REPORTS_ROOT = Path(DEFAULT_CONFIG["results_dir"]) / "reports"
THEME = Path(__file__).with_name("theme.css")
FONTS = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800'
         '&family=JetBrains+Mono:wght@400;600&family=Noto+Sans+KR:wght@400;600;700&display=swap">')
AGENT_LOOK = {"waiting": ("대기", ""), "working": ("작업 중", "run"), "done": ("완료", "done"), "error": ("오류", "bad")}
JOB_LOOK = {"queued": ("대기열", ""), "running": ("실행 중", "run"), "done": ("완료", "done"), "failed": ("실패", "bad")}
RATINGS = {"Buy", "Overweight", "Hold", "Underweight", "Sell"}
QUICK_PICKS = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "BNB-USD"]
ALIVE_SECONDS = 20  # the worker beats every 5 s

conn = db.connect()
REPORTS_ROOT.mkdir(parents=True, exist_ok=True)
app.add_static_files("/reports", REPORTS_ROOT)


def _dot(kind: str) -> None:
    ui.element("span").classes(f"dot {kind}")


def _rating(rating: str | None) -> None:
    if rating:
        ui.label(rating).classes(f"rating r-{rating.lower() if rating in RATINGS else 'review'}")


def _duration(job) -> str:
    if not job["started_at"]:
        return "--:--"
    seconds = int((job["finished_at"] or time.time()) - job["started_at"])
    return f"{seconds // 60}:{seconds % 60:02d}"


def _latest(text: str, size: int = 200) -> str:
    """The end of what an agent has said, starting on a whole word: its latest point."""
    text = " ".join(text.replace("*", "").split())
    if len(text) <= size:
        return text
    tail = text[-size:]
    return "…" + tail[tail.find(" ") + 1:]


def _show_text(title: str, text: str) -> None:
    with ui.dialog() as dialog, ui.element("div").classes("panel w-[min(960px,95vw)] p-6"):
        with ui.row().classes("items-center justify-between w-full mb-3"):
            ui.label(title).classes("text-lg font-bold")
            ui.button(icon="close", on_click=dialog.close).props("flat round dense color=grey")
        with ui.scroll_area().classes("h-[70vh]"):
            ui.markdown(text or "_아직 내용이 없습니다._").classes("report-md")
    dialog.open()


def _today() -> float:
    return datetime.combine(date.today(), datetime.min.time()).timestamp()


@ui.page("/")
def index() -> None:
    ui.add_head_html(FONTS)
    ui.add_css(THEME)
    ui.colors(primary="#38bdf8")
    selected = {"job": None}  # None follows the newest job
    seen: dict[str, object] = {}  # last drawn state per section, so a refresh with nothing new skips the redraw

    def changed(section: str, state) -> bool:
        if seen.get(section) == state:
            return False
        seen[section] = state
        return True

    # ── top bar ──
    with ui.element("div").classes("topbar w-full"), \
            ui.row().classes("max-w-[1920px] mx-auto px-4 md:px-6 py-3 items-center gap-3 no-wrap"):
            ui.icon("candlestick_chart", size="28px").classes("text-sky-400")
            ui.label("TRADING BOT").classes("brand text-xl whitespace-nowrap")
            ui.label("PHASE 0").classes("chip")
            ui.space()

            @ui.refreshable
            def master_chip() -> None:
                beat = db.heartbeat(conn, "master")
                alive = beat is not None and time.time() - beat["at"] < ALIVE_SECONDS
                with ui.element("div").classes("chip"):
                    _dot("ok" if alive else "bad")
                    ui.label(f"Master · {beat['note']}" if alive else "Master · 워커 꺼짐")
                if not alive:
                    ui.tooltip("python -m autotrader.worker 로 워커를 켜세요")

            master_chip()

    with ui.element("div").classes("max-w-[1920px] mx-auto w-full p-4 md:p-6 grid grid-cols-1 xl:grid-cols-12 gap-4"):
        # ── left: new analysis + history ──
        with ui.element("div").classes("xl:col-span-3 flex flex-col gap-4"):
            with ui.element("div").classes("panel p-5 flex flex-col gap-3"):
                ui.label("새 분석").classes("panel-title")
                tickers = ui.input("종목 (쉼표로 구분)", value="BTC-USD, ETH-USD").props("outlined dense").classes("w-full")
                with ui.row().classes("gap-1"):
                    for pick_symbol in QUICK_PICKS:
                        def add_symbol(s=pick_symbol) -> None:
                            current = [t.strip() for t in tickers.value.split(",") if t.strip()]
                            tickers.value = ", ".join(current + [s] if s not in current else current)
                        ui.button(pick_symbol.split("-")[0], on_click=add_symbol).props(
                            'outline rounded no-caps size=sm padding="2px 12px"')
                trade_date = ui.input("기준일", value=date.today().isoformat()).props("outlined dense type=date").classes("w-full")
                ui.label("분석가").classes("text-xs text-slate-400 mt-1")
                with ui.element("div").classes("grid grid-cols-2 gap-x-2"):
                    checks = {key: ui.checkbox(ANALYST_AGENT_NAMES[key].replace(" Analyst", ""),
                                               value=key != "fundamentals").props("dense")
                              for key in ANALYST_ORDER}

                def add_jobs() -> None:
                    symbols = [t.strip().upper() for t in tickers.value.split(",") if t.strip()]
                    analysts = [key for key, box in checks.items() if box.value]
                    try:
                        date.fromisoformat((trade_date.value or "").strip())
                    except ValueError:
                        ui.notify("기준일을 선택하세요", type="negative")
                        return
                    bad = [s for s in symbols if not is_valid_ticker_input(s)]
                    if not symbols or bad or not analysts:
                        ui.notify(f"종목과 분석가를 확인하세요 {bad or ''}", type="negative")
                        return
                    for symbol in symbols:
                        db.enqueue(conn, symbol, trade_date.value.strip(), analysts)
                    selected["job"] = None
                    ui.notify(f"{len(symbols)}건을 대기열에 추가했습니다 · 코인은 펀더멘털 분석가 자동 제외", type="positive")
                    refresh()

                ui.button("대기열에 추가", icon="playlist_add", on_click=add_jobs).props("unelevated").classes("w-full mt-1")

            with ui.element("div").classes("panel p-3 flex flex-col"):
                ui.label("분석 기록").classes("panel-title px-2 pt-2 pb-1")

                @ui.refreshable
                def job_list() -> None:
                    jobs = db.recent_jobs(conn)
                    if not jobs:
                        ui.label("아직 분석이 없습니다").classes("text-sm text-slate-500 px-2 py-4")
                    newest = jobs[0]["id"] if jobs else None
                    for job in jobs:
                        active = job["id"] == (selected["job"] or newest)
                        with ui.element("div").classes("job w-full" + (" active" if active else "")).on(
                                "click", lambda j=job["id"]: pick(j)):
                            with ui.row().classes("items-center justify-between w-full no-wrap"):
                                with ui.row().classes("items-center gap-2 no-wrap"):
                                    _dot(JOB_LOOK.get(job["status"], ("", ""))[1])
                                    ui.label(job["ticker"]).classes("font-semibold mono")
                                _rating(job["rating"])
                            ui.label(f"#{job['id']} · {job['trade_date']} · {_duration(job)} · LLM {job['llm_calls'] or 0}"
                                     ).classes("text-xs text-slate-500 mono")

                with ui.element("div").classes("max-h-[52vh] overflow-y-auto flex flex-col gap-1"):
                    job_list()

        # ── right: KPIs + agent pipeline ──
        with ui.element("div").classes("xl:col-span-9 flex flex-col gap-4"):

            @ui.refreshable
            def kpis() -> None:
                usage = db.usage_since(conn, _today())
                avg = usage["avg_seconds"]
                tiles = [
                    ("task_alt", "오늘 완료", usage["done"] or 0, "text-emerald-400"),
                    ("error_outline", "실패", usage["failed"] or 0, "text-rose-400"),
                    ("hourglass_top", "대기·실행", usage["pending"] or 0, "text-amber-400"),
                    ("bolt", "LLM 호출", usage["llm_calls"], "text-sky-400"),
                    ("token", "토큰", f"{(usage['tokens_in'] + usage['tokens_out']) / 1000:,.1f}k", "text-violet-400"),
                    ("timer", "평균 소요", f"{int(avg) // 60}:{int(avg) % 60:02d}" if avg else "--:--", "text-slate-300"),
                ]
                with ui.element("div").classes("grid grid-cols-2 md:grid-cols-3 2xl:grid-cols-6 gap-3"):
                    for icon, label, value, colour in tiles:
                        with ui.element("div").classes("panel kpi"):
                            with ui.row().classes("items-center gap-2 no-wrap"):
                                ui.icon(icon, size="18px").classes(colour)
                                ui.label(label).classes("kpi-label")
                            ui.label(str(value)).classes("kpi-value mono mt-1")

            kpis()

            @ui.refreshable
            def board_view() -> None:
                job = db.get_job(conn, selected["job"]) if selected["job"] else next(iter(db.recent_jobs(conn, 1)), None)
                with ui.element("div").classes("panel p-5 flex flex-col gap-4"):
                    if job is None:
                        with ui.column().classes("items-center w-full py-16 gap-2"):
                            ui.icon("hub", size="48px").classes("text-slate-600")
                            ui.label("왼쪽에서 종목을 추가하면 에이전트들이 여기서 일합니다").classes("text-slate-400")
                        return
                    board = json.loads(job["board"]) if job["board"] else []
                    n_analysts = sum(name in ANALYST_AGENT_NAMES.values() for name, _ in board)
                    status_list = statuses(board, n_analysts, job["status"])
                    done = status_list.count("done")

                    with ui.row().classes("items-center gap-3 w-full"):
                        ui.label(job["ticker"]).classes("text-2xl font-extrabold mono")
                        with ui.element("div").classes("chip"):
                            _dot(JOB_LOOK.get(job["status"], ("", ""))[1])
                            ui.label(JOB_LOOK.get(job["status"], (job["status"],))[0])
                        ui.label(f"{job['trade_date']} · #{job['id']} · {_duration(job)}").classes("text-sm text-slate-400 mono")
                        ui.space()
                        _rating(job["rating"])
                        if job["report_dir"] and (Path(job["report_dir"]) / "complete_report.html").exists():
                            ui.button("리포트", icon="open_in_new").props(
                                f'outline dense rounded href="/reports/{Path(job["report_dir"]).name}/complete_report.html"'
                                ' target="_blank"')
                    if board:
                        ui.linear_progress(done / len(board), show_value=False, size="4px").props("rounded color=primary")
                    if job["error"]:
                        with ui.row().classes("items-center gap-2 text-rose-400 text-sm no-wrap"):
                            ui.icon("warning")
                            ui.label(job["error"]).classes("break-all")

                    with ui.element("div").classes("grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3"):
                        for number, (title, icon, agents) in enumerate(by_stage(board, status_list), start=1):
                            with ui.element("div").classes("flex flex-col gap-2 min-w-0"):
                                with ui.element("div").classes("stage-head"):
                                    ui.label(str(number)).classes("stage-num")
                                    ui.icon(icon, size="16px")
                                    ui.label(title)
                                for name, role, text, status in agents:
                                    label, dot = AGENT_LOOK[status]
                                    with ui.element("div").classes(f"agent st-{status}").on(
                                            "click", lambda n=name, t=text: _show_text(n, t)):
                                        ui.label(name.replace(" Analyst", "").replace(" Researcher", "")
                                                 ).classes("agent-name")
                                        with ui.row().classes("items-center justify-between w-full no-wrap gap-1"):
                                            ui.label(role).classes("agent-role truncate")
                                            with ui.row().classes("items-center gap-1 no-wrap shrink-0"):
                                                _dot(dot)
                                                ui.label(label).classes("text-[0.7rem] text-slate-400 whitespace-nowrap")
                                        if text:
                                            ui.label(_latest(text)).classes("agent-text")

            board_view()

    def pick(job_id: int) -> None:
        selected["job"] = job_id
        refresh()

    def refresh() -> None:
        beat = db.heartbeat(conn, "master")
        if changed("master", (beat is not None and time.time() - beat["at"] < ALIVE_SECONDS, beat and beat["note"])):
            master_chip.refresh()
        if changed("kpis", tuple(db.usage_since(conn, _today()))):
            kpis.refresh()
        jobs = db.recent_jobs(conn)
        if changed("jobs", (selected["job"], tuple(tuple(j) for j in jobs))):
            job_list.refresh()
            board_view.refresh()

    ui.timer(2.0, refresh)


def main() -> None:
    # shortcut: no login, so anyone on the LAN can queue analyses; add auth before phase 1 puts trading controls here.
    # TRADING_BOT_HOST=127.0.0.1 keeps it on this machine only.
    host = os.environ.get("TRADING_BOT_HOST", "0.0.0.0")
    ui.run(host=host, port=int(os.environ.get("TRADING_BOT_PORT", "8080")), title="Trading Bot",
           reload=False, show=False, language="ko-KR", dark=True)


if __name__ in {"__main__", "__mp_main__"}:
    main()
