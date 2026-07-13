from __future__ import annotations

import sys
import threading

from app import serve


def test_worker_monitor_terminates_parent_on_unexpected_exit(monkeypatch):
    calls = []

    class ExitedWorker:
        def wait(self):
            return 17

    monkeypatch.setattr(serve.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    monkeypatch.setattr(serve.os, "getpid", lambda: 1234)
    serve.monitor_worker(ExitedWorker(), threading.Event())
    assert calls == [(1234, serve.signal.SIGTERM)]


def test_worker_monitor_ignores_expected_shutdown(monkeypatch):
    calls = []
    stopping = threading.Event()
    stopping.set()

    class ExitedWorker:
        def wait(self):
            return 0

    monkeypatch.setattr(serve.os, "kill", lambda *args: calls.append(args))
    serve.monitor_worker(ExitedWorker(), stopping)
    assert calls == []


def test_runtime_is_initialized_before_worker_and_api(monkeypatch):
    events: list[object] = []
    settings = object()

    class FakeEngine:
        def dispose(self):
            events.append("dispose")

    class FakeDatabase:
        def __init__(self, received_settings):
            assert received_settings is settings
            self.engine = FakeEngine()

        def initialize(self):
            events.append("initialize")

    class FakeWorker:
        def poll(self):
            return 0

        def terminate(self):
            events.append("terminate")

        def wait(self, timeout):
            assert timeout == 10
            events.append("wait")

        def kill(self):
            events.append("kill")

    class FakeThread:
        def __init__(self, **kwargs):
            assert kwargs["target"] is serve.monitor_worker
            assert kwargs["daemon"] is True

        def start(self):
            pass

    def fake_popen(command):
        assert command == [sys.executable, "-m", "app.worker"]
        events.append("popen")
        return FakeWorker()

    monkeypatch.setattr(serve.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(serve, "Database", FakeDatabase)
    monkeypatch.setattr(serve.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(serve.threading, "Thread", FakeThread)
    monkeypatch.setattr(serve.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(serve.uvicorn, "run", lambda *args, **kwargs: events.append("uvicorn"))

    serve.main()

    assert events == ["initialize", "dispose", "popen", "uvicorn", "wait"]
