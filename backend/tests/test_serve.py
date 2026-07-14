from __future__ import annotations

import sys
import threading
from types import SimpleNamespace

import pytest

from app import image_worker as image_worker_module
from app import serve, worker as worker_module
from app.config import Settings


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
    wait_timeouts: list[float] = []
    settings = SimpleNamespace(worker_concurrency=2, image_timeout=180.0)

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
        def __init__(self, slot):
            self.slot = slot

        def poll(self):
            return None

        def terminate(self):
            events.append(f"terminate-{self.slot}")

        def wait(self, timeout):
            wait_timeouts.append(timeout)
            events.append(f"wait-{self.slot}")

        def kill(self):
            events.append(f"kill-{self.slot}")

    class FakeThread:
        def __init__(self, **kwargs):
            assert kwargs["target"] is serve.monitor_worker
            assert kwargs["daemon"] is True
            assert kwargs["name"] in {
                "frameflow-worker-monitor-1",
                "frameflow-worker-monitor-2",
                "frameflow-image-worker-monitor",
            }
            self.slot = (
                "image"
                if kwargs["name"] == "frameflow-image-worker-monitor"
                else kwargs["name"].rsplit("-", 1)[-1]
            )

        def start(self):
            events.append(f"monitor-{self.slot}")

    def fake_popen(command, *, env):
        assert env["FRAMEFLOW_DATABASE_INITIALIZED"] == "1"
        if command == [sys.executable, "-m", "app.image_worker"]:
            assert env["FRAMEFLOW_IMAGE_WORKER_ID"] == "test-host:image:1"
            events.append("popen-image")
            return FakeWorker("image")
        assert command == [sys.executable, "-m", "app.worker"]
        slot = len(
            [
                event
                for event in events
                if str(event).startswith("popen-") and event != "popen-image"
            ]
        ) + 1
        assert env["FRAMEFLOW_WORKER_ID"] == f"test-host:{slot}"
        events.append(f"popen-{slot}")
        return FakeWorker(slot)

    monkeypatch.setattr(serve.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(serve, "Database", FakeDatabase)
    monkeypatch.setattr(serve.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(serve.threading, "Thread", FakeThread)
    monkeypatch.setattr(serve.socket, "gethostname", lambda: "test-host")
    monkeypatch.setattr(serve.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(serve.uvicorn, "run", lambda *args, **kwargs: events.append("uvicorn"))

    serve.main()

    assert events == [
        "initialize",
        "dispose",
        "popen-1",
        "monitor-1",
        "popen-2",
        "monitor-2",
        "popen-image",
        "monitor-image",
        "uvicorn",
        "terminate-1",
        "terminate-2",
        "terminate-image",
        "wait-1",
        "wait-2",
        "wait-image",
    ]
    assert len(wait_timeouts) == 3
    assert all(200 <= timeout <= 210 for timeout in wait_timeouts)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0", 1), ("1", 1), ("4", 4), ("17", 16), ("invalid", 2)],
)
def test_worker_concurrency_is_bounded(monkeypatch, raw, expected):
    monkeypatch.setenv("FRAMEFLOW_WORKER_CONCURRENCY", raw)
    assert Settings.from_env().worker_concurrency == expected


@pytest.mark.parametrize(
    ("parent_marker", "expects_initialize"),
    [(None, True), ("FRAMEFLOW_DATABASE_INITIALIZED", False)],
)
def test_worker_skips_reinitialization_only_for_parent_started_processes(
    monkeypatch, parent_marker, expects_initialize
):
    events = []
    settings = object()

    class FakeDatabase:
        def __init__(self, received_settings):
            assert received_settings is settings

        def initialize(self):
            events.append("initialize")

    class FakeDurableWorker:
        def __init__(self, database, received_settings, worker_id):
            assert isinstance(database, FakeDatabase)
            assert received_settings is settings
            assert worker_id == "pool-slot-1"

        def stop(self):
            pass

        def run_forever(self):
            events.append("run")

    monkeypatch.delenv("FRAMEFLOW_DATABASE_INITIALIZED", raising=False)
    monkeypatch.delenv("FRAMEFLOW_RUNTIME_INITIALIZED", raising=False)
    if parent_marker:
        monkeypatch.setenv(parent_marker, "1")
    monkeypatch.setenv("FRAMEFLOW_WORKER_ID", "pool-slot-1")
    monkeypatch.setattr(worker_module.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(worker_module, "Database", FakeDatabase)
    monkeypatch.setattr(worker_module, "DurableWorker", FakeDurableWorker)
    monkeypatch.setattr(worker_module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(worker_module.logging, "basicConfig", lambda **_kwargs: None)

    worker_module.main()

    assert ("initialize" in events) is expects_initialize
    assert events[-1] == "run"


@pytest.mark.parametrize(
    ("parent_marker", "expects_initialize"),
    [(None, True), ("FRAMEFLOW_DATABASE_INITIALIZED", False)],
)
def test_image_worker_skips_reinitialization_only_for_parent_started_processes(
    monkeypatch, parent_marker, expects_initialize
):
    events = []
    settings = object()

    class FakeDatabase:
        def __init__(self, received_settings):
            assert received_settings is settings

        def initialize(self):
            events.append("initialize")

    class FakeImageWorker:
        def __init__(self, database, received_settings, worker_id):
            assert isinstance(database, FakeDatabase)
            assert received_settings is settings
            assert worker_id == "image-slot-1"

        def stop(self):
            pass

        def run_forever(self):
            events.append("run")

    monkeypatch.delenv("FRAMEFLOW_DATABASE_INITIALIZED", raising=False)
    monkeypatch.delenv("FRAMEFLOW_RUNTIME_INITIALIZED", raising=False)
    if parent_marker:
        monkeypatch.setenv(parent_marker, "1")
    monkeypatch.setenv("FRAMEFLOW_IMAGE_WORKER_ID", "image-slot-1")
    monkeypatch.setattr(image_worker_module.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(image_worker_module, "Database", FakeDatabase)
    monkeypatch.setattr(image_worker_module, "DurableImageWorker", FakeImageWorker)
    monkeypatch.setattr(image_worker_module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        image_worker_module.logging, "basicConfig", lambda **_kwargs: None
    )

    image_worker_module.main()

    assert ("initialize" in events) is expects_initialize
    assert events[-1] == "run"
