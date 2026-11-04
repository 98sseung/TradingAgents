"""Start the whole bot with one command:  python -m autotrader

Runs guard, workers, engine and gui as separate processes (so a hung engine never stops the Risk Guard),
restarts any that crash, and stops all of them on Ctrl+C. Each one's output goes to ~/.trading-bot/logs/<name>.log.
"""

import os
import signal
import subprocess
import sys
import time

from autotrader import db

LOG_DIR = db.DB_PATH.parent / "logs"
QUICK_EXIT = 10  # a process that dies this fast is misconfigured (e.g. no GUI password); restarting won't help


def process_specs(environ) -> list[tuple[str, str, dict[str, str]]]:
    workers = db.worker_names(environ)
    return [
        ("guard", "autotrader.guard", {}),
        *[(name, "autotrader.worker", {"TRADING_BOT_WORKER_NAME": name}) for name in workers],
        ("engine", "autotrader.engine", {}),
        ("gui", "autotrader.gui", {}),
    ]


def start(name: str, module: str, extra_env: dict[str, str]) -> subprocess.Popen:
    with open(LOG_DIR / f"{name}.log", "a") as log:
        log_start = log.tell()  # capture before the child can write its first error
        process = subprocess.Popen([sys.executable, "-m", module], stdout=log, stderr=subprocess.STDOUT,
                                   env=os.environ | extra_env)
        process.started, process.log_start = time.monotonic(), log_start
        print(f"started {name:<6} pid {process.pid}  log {log.name}", flush=True)
    return process


def output_of(name: str, process: subprocess.Popen) -> str:
    """What this run of the process wrote, not earlier runs sharing the log."""
    with open(LOG_DIR / f"{name}.log", errors="replace") as log:
        log.seek(process.log_start)
        return log.read()[-2000:]


def check_processes(running, processes) -> None:
    for name, process in list(running.items()):
        if process.poll() is None:
            continue
        if time.monotonic() - process.started < QUICK_EXIT:
            print(f"{name} exited right after starting; not restarting it:\n{output_of(name, process)}", flush=True)
            del running[name]
        else:
            print(f"{name} stopped (exit {process.returncode}); restarting", flush=True)
            running[name] = start(name, *processes[name])


def main() -> None:
    try:
        processes = {name: (module, extra_env) for name, module, extra_env in process_specs(os.environ)}
    except ValueError as exc:
        sys.exit(str(exc))
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    running = {}
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # systemd/kill stop it like Ctrl+C
    signal.signal(signal.SIGINT, signal.default_int_handler)  # a shell that backgrounds us would otherwise ignore Ctrl+C
    try:
        for name, (module, extra_env) in processes.items():
            running[name] = start(name, module, extra_env)
            time.sleep(1)
            check_processes(running, processes)
        print("all running · Ctrl+C stops everything · GUI http://<this PC's IP>:8080", flush=True)
        while running:
            time.sleep(2)
            check_processes(running, processes)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        for process in running.values():
            process.terminate()
        for name, process in running.items():
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            print(f"stopped {name}", flush=True)


if __name__ == "__main__":
    main()
