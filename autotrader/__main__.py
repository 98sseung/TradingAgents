"""Start the whole bot with one command:  python -m autotrader

Runs guard, worker, engine and gui as separate processes (so a hung engine never stops the Risk Guard),
restarts any that crash, and stops all of them on Ctrl+C. Each one's output goes to ~/.trading-bot/logs/<name>.log.
"""

import signal
import subprocess
import sys
import time

from autotrader import db

PROCESSES = ["guard", "worker", "engine", "gui"]  # guard first: the engine holds entries until it beats
LOG_DIR = db.DB_PATH.parent / "logs"
QUICK_EXIT = 10  # a process that dies this fast is misconfigured (e.g. no GUI password); restarting won't help


def start(name: str) -> subprocess.Popen:
    log = open(LOG_DIR / f"{name}.log", "a")  # noqa: SIM115  (the child owns it for its lifetime)
    process = subprocess.Popen([sys.executable, "-m", f"autotrader.{name}"], stdout=log, stderr=subprocess.STDOUT)
    process.started, process.log_start = time.monotonic(), log.tell()
    print(f"started {name:<6} pid {process.pid}  log {log.name}", flush=True)
    return process


def output_of(name: str, process: subprocess.Popen) -> str:
    """What this run of the process wrote, not earlier runs sharing the log."""
    with open(LOG_DIR / f"{name}.log", errors="replace") as log:
        log.seek(process.log_start)
        return log.read()[-2000:]


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    running = {}
    for name in PROCESSES:
        running[name] = start(name)
        time.sleep(1)
    print("all running · Ctrl+C stops everything · GUI http://<this PC's IP>:8080", flush=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # systemd/kill stop it like Ctrl+C
    signal.signal(signal.SIGINT, signal.default_int_handler)  # a shell that backgrounds us would otherwise ignore Ctrl+C
    try:
        while running:
            time.sleep(2)
            for name, process in list(running.items()):
                if process.poll() is None:
                    continue
                if time.monotonic() - process.started < QUICK_EXIT:
                    print(f"{name} exited right after starting; not restarting it:\n{output_of(name, process)}", flush=True)
                    del running[name]
                else:
                    print(f"{name} stopped (exit {process.returncode}); restarting", flush=True)
                    running[name] = start(name)
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
