"""Launcher process naming and lifecycle without starting child processes."""

from types import SimpleNamespace

import pytest

from autotrader import __main__ as launcher


@pytest.mark.parametrize("count, expected", [(None, 1), ("1", 1), ("3", 3), ("9", 9)])
def test_worker_entries_from_environment(count, expected):
    environ = {} if count is None else {"TRADING_BOT_WORKERS": count}
    assert launcher.process_specs(environ) == [
        ("guard", "autotrader.guard", {}),
        *[(f"worker-{i}", "autotrader.worker", {"TRADING_BOT_WORKER_NAME": f"worker-{i}"})
          for i in range(1, expected + 1)],
        ("engine", "autotrader.engine", {}),
        ("gui", "autotrader.gui", {}),
    ]


@pytest.mark.parametrize("count", ["0", "-1", "invalid", "", "1.5"])
def test_invalid_worker_count_is_refused(count):
    with pytest.raises(ValueError, match="TRADING_BOT_WORKERS must be an integer >= 1"):
        launcher.process_specs({"TRADING_BOT_WORKERS": count})


def test_worker_launch_has_own_name_module_and_log(tmp_path, monkeypatch):
    calls = []

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        kwargs["stdout"].write("this run\n")
        kwargs["stdout"].flush()
        return SimpleNamespace(pid=len(calls))

    monkeypatch.setattr(launcher, "LOG_DIR", tmp_path)
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setenv("TRADING_BOT_WORKER_NAME", "wrong-parent-name")
    specs = launcher.process_specs({"TRADING_BOT_WORKERS": "2"})
    for spec in specs:
        (tmp_path / f"{spec[0]}.log").write_text("previous run\n")
        process = launcher.start(*spec)
        assert launcher.output_of(spec[0], process) == "this run\n"
    for (name, module, extra_env), (command, kwargs) in zip(specs, calls, strict=True):
        assert command == [launcher.sys.executable, "-m", module]
        assert kwargs["stdout"].name == str(tmp_path / f"{name}.log")
        assert kwargs["env"] == launcher.os.environ | extra_env
        assert kwargs["stdout"].closed
    assert launcher.os.environ["TRADING_BOT_WORKER_NAME"] == "wrong-parent-name"


def test_only_the_failed_worker_is_restarted(tmp_path, monkeypatch):
    calls, stopped = [], []
    names = ["guard", "worker-1", "worker-2", "engine", "gui"]

    def start(name, module, extra_env):
        calls.append(name)
        failed = name == "worker-1" and calls.count(name) == 1
        return SimpleNamespace(started=0, returncode=1 if failed else None,
                               poll=lambda: 1 if failed and len(calls) >= len(names) else None,
                               terminate=lambda: stopped.append(name), wait=lambda timeout: None)

    def sleep(seconds):
        if seconds == 2 and calls.count("worker-1") == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(launcher, "LOG_DIR", tmp_path)
    monkeypatch.setenv("TRADING_BOT_WORKERS", "2")
    monkeypatch.setattr(launcher, "start", start)
    monkeypatch.setattr(launcher.time, "monotonic", lambda: 20)
    monkeypatch.setattr(launcher.time, "sleep", sleep)
    monkeypatch.setattr(launcher.signal, "signal", lambda *args: None)
    launcher.main()
    assert calls == names + ["worker-1"]
    assert stopped == names


def test_quick_exit_shows_current_run_without_restart(tmp_path, monkeypatch, capsys):
    log = tmp_path / "worker-1.log"
    log.write_text("old error\ncurrent error\n")
    failed = SimpleNamespace(started=0, log_start=len("old error\n"), poll=lambda: 1)
    running = {"worker-1": failed}
    monkeypatch.setattr(launcher, "LOG_DIR", tmp_path)
    monkeypatch.setattr(launcher.time, "monotonic", lambda: 1)
    monkeypatch.setattr(launcher, "start", lambda *args: pytest.fail("quick exit was restarted"))
    launcher.check_processes(running, {"worker-1": ("autotrader.worker", {})})
    output = capsys.readouterr().out
    assert not running
    assert "current error" in output and "old error" not in output


@pytest.mark.parametrize("signum", [launcher.signal.SIGINT, launcher.signal.SIGTERM])
def test_signal_during_startup_stops_all_started_children(tmp_path, monkeypatch, signum):
    calls, stopped, handlers = [], [], {}

    def start(name, module, extra_env):
        calls.append(name)
        return SimpleNamespace(poll=lambda: None, terminate=lambda: stopped.append(name), wait=lambda timeout: None)

    def sleep(seconds):
        if len(calls) == 2:
            handlers[signum](signum, None)

    monkeypatch.setenv("TRADING_BOT_WORKERS", "8")
    monkeypatch.setattr(launcher, "LOG_DIR", tmp_path)
    monkeypatch.setattr(launcher, "start", start)
    monkeypatch.setattr(launcher.time, "sleep", sleep)
    monkeypatch.setattr(launcher.signal, "signal", lambda sig, handler: handlers.update({sig: handler}))
    launcher.main()
    assert calls == stopped == ["guard", "worker-1"]
