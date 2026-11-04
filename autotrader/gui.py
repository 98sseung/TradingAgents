"""Trading-bot control panel. Phase 0: queue analyses, watch each agent work, measure plan usage.

Run it beside the worker:  python -m autotrader.gui   ->  http://<this machine's IP>:8080
Look and colours: theme.css.
"""

import asyncio
import hmac
import json
import os
import secrets
import sys
import time
from datetime import date, datetime
from pathlib import Path

from fastapi.responses import RedirectResponse

# Session files go beside the bot's db, not into whatever directory the GUI was started from (the repo).
os.environ.setdefault("NICEGUI_STORAGE_PATH", str(Path.home() / ".trading-bot" / "nicegui"))
from nicegui import app, ui  # noqa: E402  (reads NICEGUI_STORAGE_PATH at import)
from starlette.middleware.base import BaseHTTPMiddleware

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
ALIVE_SECONDS = 60  # the slowest process (engine) beats every 30 s
PROCESSES = [("engine", "Master"), ("worker", "Worker"), ("guard", "Risk Guard")]  # heartbeat name, label

conn = db.connect()
REPORTS_ROOT.mkdir(parents=True, exist_ok=True)
app.add_static_files("/reports", REPORTS_ROOT)
SESSION_SECRET_FILE = db.DB_PATH.parent / "gui_session_secret"


class LoginRequired(BaseHTTPMiddleware):
    """Every page and report file needs a signed-in session; only the login page and NiceGUI's assets are open."""

    async def dispatch(self, request, call_next):
        path = request.url.path
        if not app.storage.user.get("authenticated") and path != "/login" and not path.startswith("/_nicegui"):
            return RedirectResponse("/login")
        return await call_next(request)


app.add_middleware(LoginRequired)


@ui.page("/login")
def login() -> RedirectResponse | None:
    if app.storage.user.get("authenticated"):
        return RedirectResponse("/")
    ui.add_head_html(FONTS)
    ui.add_css(THEME)

    async def try_login() -> None:
        if hmac.compare_digest(password.value.encode(), os.environ["TRADING_BOT_GUI_PASSWORD"].encode()):
            app.storage.user["authenticated"] = True
            ui.navigate.to("/")
        else:
            await asyncio.sleep(1)  # slows guessing
            ui.notify("비밀번호가 틀렸습니다", type="negative")

    with ui.element("div").classes("min-h-screen w-full grid place-items-center p-4"), \
            ui.element("div").classes("panel p-8 w-full max-w-sm flex flex-col gap-4"):
        with ui.row().classes("items-center gap-2"):
            ui.icon("candlestick_chart", size="28px").classes("text-sky-400")
            ui.label("TRADING BOT").classes("brand text-xl")
        password = ui.input("비밀번호", password=True, password_toggle_button=True).props("outlined autofocus")
        password.on("keydown.enter", try_login)
        ui.button("로그인", icon="login", on_click=try_login).props("unelevated").classes("w-full")
    return None


def _session_secret() -> str:
    """Signs session cookies; kept on disk so a restart does not sign everyone out."""
    if not SESSION_SECRET_FILE.exists():
        SESSION_SECRET_FILE.write_text(secrets.token_hex(32))
        SESSION_SECRET_FILE.chmod(0o600)
    return SESSION_SECRET_FILE.read_text().strip()


def _dot(kind: str) -> None:
    ui.element("span").classes(f"dot {kind}")


def _worker_status(conn, now):
    beats = {row["name"]: row for row in conn.execute("SELECT * FROM heartbeat WHERE name GLOB 'worker-[0-9]*'")
             if row["name"][7:].isdigit() and int(row["name"][7:]) >= 1}
    names = set(db.worker_names()) | {name for name, beat in beats.items() if 0 <= now - beat["at"] < ALIVE_SECONDS}
    workers = []
    for name in sorted(names, key=lambda name: int(name[7:])):
        beat = beats.get(name)
        alive = beat is not None and 0 <= now - beat["at"] < ALIVE_SECONDS
        workers.append((name, alive, (beat["note"] or "idle") if alive else "offline"))
    return tuple(workers)


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


ACTION_LOOK = {"open_long": ("롱 진입", "r-buy"), "open_short": ("숏 진입", "r-sell"), "close": ("청산", "r-underweight"),
               "hold": ("유지", "r-review"), "none": ("진입 안 함", "r-review")}


def _action(action: str) -> None:
    label, look = ACTION_LOOK.get(action, (action, "r-review"))
    ui.label(label).classes(f"rating {look} whitespace-nowrap")


def _side(side: str) -> None:
    ui.label(side).classes("rating whitespace-nowrap " + ("r-buy" if side == "LONG" else "r-sell"))


def _age(since: float) -> str:
    hours = (time.time() - since) / 3600
    return f"{hours:.0f}시간" if hours < 48 else f"{hours / 24:.1f}일"


def _control(key: str) -> str | None:
    row = conn.execute("SELECT value FROM control WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_control(key: str, value: str) -> None:
    # The engine and risk guard read these keys; the GUI only ever writes stop/kill.
    conn.execute("INSERT INTO control (key, value, updated_at) VALUES (?, ?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                 (key, value, time.time()))
    conn.execute("INSERT INTO events (at, level, source, message) VALUES (?, 'warn', 'gui', ?)",
                 (time.time(), f"{key} set to {value} from the GUI"))


def _confirm(title: str, body: str, on_yes) -> None:
    with ui.dialog() as dialog, ui.element("div").classes("panel p-6 w-[min(480px,92vw)] flex flex-col gap-4"):
        ui.label(title).classes("text-lg font-bold")
        ui.label(body).classes("text-sm text-slate-300")
        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("취소", on_click=dialog.close).props("flat color=grey")

            def yes() -> None:
                on_yes()
                dialog.close()
            ui.button("확인", on_click=yes).props("unelevated color=negative")
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
            ui.label("PHASE 1").classes("chip")
            live = os.environ.get("TRADING_BOT_MODE", "demo") == "live"
            ui.label("LIVE" if live else "DEMO").classes("chip " + ("text-rose-400" if live else "text-sky-400"))
            ui.space()

            @ui.refreshable
            def control_bar() -> None:
                if _control("kill") == "1":
                    ui.label("KILL · 잠김").classes("chip text-rose-400")
                    ui.button("잠금 해제", icon="lock_open", on_click=lambda: _confirm(
                        "잠금을 해제할까요?", "자동매매가 다시 신규 진입을 할 수 있게 됩니다.",
                        lambda: _set_control("kill", "0"))).props("outline dense rounded color=grey")
                    return
                stopped = _control("stop") == "1"
                if stopped:
                    ui.label("STOP · 신규 진입 중단").classes("chip text-amber-400")
                ui.button("재개" if stopped else "STOP", icon="play_arrow" if stopped else "pause",
                          on_click=lambda: _set_control("stop", "0" if stopped else "1")
                          ).props("outline dense rounded color=amber").tooltip("신규 진입만 멈춤 · 기존 포지션의 손절·익절은 유지")
                ui.button("KILL", icon="dangerous", on_click=lambda: _confirm(
                    "KILL 할까요?", "모든 미체결 주문을 취소하고, 모든 포지션을 시장가로 청산한 뒤, 잠금 해제 전까지 주문을 막습니다.",
                    lambda: _set_control("kill", "1"))).props("unelevated dense rounded color=negative")

            control_bar()

            @ui.refreshable
            def master_chip() -> None:
                for name, label in PROCESSES:
                    if name == "worker":
                        workers = _worker_status(conn, time.time())
                        alive = sum(running for _, running, _ in workers)
                        with ui.element("div").classes("chip"):
                            _dot("ok" if alive else "bad")
                            ui.label(f"{label} {alive}/{len(workers)}")
                            ui.tooltip("\n".join(f"{name}: {note or 'idle'}" for name, _, note in workers)
                                       or "python -m autotrader.worker").classes("whitespace-pre-line")
                        continue
                    beat = db.heartbeat(conn, name)
                    alive = beat is not None and time.time() - beat["at"] < ALIVE_SECONDS
                    with ui.element("div").classes("chip"):
                        _dot("ok" if alive else "bad")
                        ui.label(f"{label} · {beat['note'] or '정상'}" if alive else f"{label} · 꺼짐")
                    ui.tooltip(f"python -m autotrader.{name}")

            master_chip()

            def logout() -> None:
                app.storage.user.clear()
                ui.navigate.to("/login")

            ui.button(icon="logout", on_click=logout).props("flat round dense color=grey").tooltip("로그아웃")

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
                                    if job["source"] == "engine":
                                        ui.label("자동").classes("chip text-sky-400").tooltip("엔진이 넣은 분석 · 매매로 이어질 수 있음")
                                _rating(job["rating"])
                            ui.label(f"#{job['id']} · {job['trade_date']} · {_duration(job)} · LLM {job['llm_calls'] or 0}"
                                     ).classes("text-xs text-slate-500 mono")
                            if job["status"] == "running" and job["worker"]:
                                ui.label(job["worker"]).classes("text-xs text-slate-500 mono")

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
                    ("account_balance_wallet", "자산 USDT", f"{float(_control('equity') or 0):,.2f}"
                     if _control("equity") else "--", "text-emerald-300"),
                ]
                with ui.element("div").classes("grid grid-cols-2 md:grid-cols-4 2xl:grid-cols-7 gap-3"):
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
                        if job["status"] == "running" and job["worker"]:
                            ui.label(job["worker"]).classes("text-xs text-slate-500 mono")
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
                    decision = conn.execute("SELECT * FROM decisions WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                                            (job["id"],)).fetchone()
                    if decision:
                        with ui.row().classes("items-center gap-2 text-sm no-wrap"):
                            ui.icon("rule", size="18px").classes("text-slate-400")
                            ui.label("리스크 게이트").classes("text-slate-400 whitespace-nowrap")
                            _action(decision["action"])
                            ui.label(decision["reason"]).classes("text-slate-300 break-all")

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

            with ui.element("div").classes("grid grid-cols-1 2xl:grid-cols-2 gap-4"):
                with ui.element("div").classes("panel p-5 flex flex-col gap-3 min-w-0"):
                    ui.label("포지션").classes("panel-title")

                    @ui.refreshable
                    def positions() -> None:
                        open_trades = conn.execute("SELECT * FROM trades WHERE status = 'open' ORDER BY id DESC").fetchall()
                        closed = conn.execute(
                            "SELECT * FROM trades WHERE status = 'closed' ORDER BY closed_at DESC LIMIT 5").fetchall()
                        if not open_trades and not closed:
                            ui.label("아직 거래가 없습니다").classes("text-sm text-slate-500")
                        for trade in open_trades:
                            with ui.element("div").classes("job w-full"):
                                with ui.row().classes("items-center gap-2 w-full no-wrap"):
                                    _side(trade["side"])
                                    ui.label(trade["symbol"]).classes("font-semibold mono")
                                    ui.space()
                                    ui.label(f"보유 {_age(trade['opened_at'])}").classes("text-xs text-slate-400")
                                ui.label(f"수량 {trade['qty']} · 진입 {trade['entry_price']} · 손절 {trade['stop']}"
                                         f" · 목표 {trade['target']}").classes("text-xs text-slate-400 mono")
                        if closed:
                            ui.label("최근 청산").classes("text-xs text-slate-500 mt-2")
                        for trade in closed:
                            with ui.row().classes("items-center gap-2 w-full no-wrap px-3 text-sm"):
                                _side(trade["side"])
                                ui.label(trade["symbol"]).classes("mono")
                                ui.label(trade["exit_reason"] or "").classes("text-xs text-slate-500")
                                ui.space()
                                if trade["realized_pnl"] is None:  # Binance posts income a little after the fill
                                    ui.label("손익 집계 중").classes("text-xs text-slate-500")
                                    continue
                                # fees are stored as a positive cost
                                pnl = float(trade["realized_pnl"]) - float(trade["fees"] or 0) + float(trade["funding"] or 0)
                                ui.label(f"{pnl:+.2f} USDT").classes(
                                    "mono " + ("text-emerald-400" if pnl >= 0 else "text-rose-400"))

                    positions()

                with ui.element("div").classes("panel p-5 flex flex-col gap-3 min-w-0"):
                    ui.label("결정 · 이벤트").classes("panel-title")

                    @ui.refreshable
                    def activity() -> None:
                        rows = conn.execute(
                            "SELECT 'decision' AS kind, created_at AS at, symbol, rating, action, reason AS text, NULL AS level"
                            " FROM decisions UNION ALL"
                            " SELECT 'event', at, source, NULL, NULL, message, level FROM events"
                            " ORDER BY at DESC LIMIT 15").fetchall()
                        if not rows:
                            ui.label("아직 기록이 없습니다").classes("text-sm text-slate-500")
                        for row in rows:
                            with ui.row().classes("items-start gap-2 w-full no-wrap text-sm"):
                                ui.label(datetime.fromtimestamp(row["at"]).strftime("%m-%d %H:%M")
                                         ).classes("text-xs text-slate-500 mono whitespace-nowrap pt-0.5")
                                if row["kind"] == "decision":
                                    ui.label(row["symbol"]).classes("mono whitespace-nowrap")
                                    _action(row["action"])
                                else:
                                    ui.icon({"error": "error", "warn": "warning"}.get(row["level"], "info"), size="16px"
                                            ).classes({"error": "text-rose-400", "warn": "text-amber-400"}.get(
                                                row["level"], "text-sky-400"))
                                    ui.label(row["symbol"]).classes("text-slate-400 whitespace-nowrap")
                                ui.label(row["text"]).classes("text-slate-300 break-all")

                    activity()

    def pick(job_id: int) -> None:
        selected["job"] = job_id
        refresh()

    def refresh() -> None:
        beats = [db.heartbeat(conn, name) for name, _ in PROCESSES if name != "worker"]
        process_state = tuple((b is not None and time.time() - b["at"] < ALIVE_SECONDS, b and b["note"])
                              for b in beats)
        if changed("master", (process_state, _worker_status(conn, time.time()))):
            master_chip.refresh()
        if changed("kpis", (tuple(db.usage_since(conn, _today())), _control("equity"))):
            kpis.refresh()
        jobs = db.recent_jobs(conn)
        last_decision = conn.execute("SELECT MAX(id) FROM decisions").fetchone()[0]
        if changed("jobs", (selected["job"], tuple(tuple(j) for j in jobs), last_decision)):
            job_list.refresh()
            board_view.refresh()
        if changed("control", tuple(tuple(r) for r in conn.execute("SELECT key, value FROM control"))):
            control_bar.refresh()
        if changed("trades", tuple(tuple(r) for r in conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 20"))):
            positions.refresh()
        last_event = conn.execute("SELECT MAX(id) FROM events").fetchone()[0]
        if changed("activity", (last_decision, last_event)):
            activity.refresh()

    ui.timer(2.0, refresh)


def main() -> None:
    if len(os.environ.get("TRADING_BOT_GUI_PASSWORD", "")) < 8:
        sys.exit("Set TRADING_BOT_GUI_PASSWORD (8+ characters) in .env before starting the GUI.")
    # shortcut: plain HTTP, so the password crosses the LAN unencrypted; put it behind HTTPS/VPN before exposing it further.
    # TRADING_BOT_HOST=127.0.0.1 keeps it on this machine only.
    host = os.environ.get("TRADING_BOT_HOST", "0.0.0.0")
    ui.run(host=host, port=int(os.environ.get("TRADING_BOT_PORT", "8080")), title="Trading Bot",
           reload=False, show=False, language="ko-KR", dark=True, storage_secret=_session_secret())


if __name__ in {"__main__", "__mp_main__"}:
    main()
