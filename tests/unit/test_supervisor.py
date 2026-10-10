"""Supervisor tests using short-lived dummy processes (no network, no Docker)."""

from __future__ import annotations

import sys
import threading
import time

from invoiceiq.runtime import healthcheck
from invoiceiq.runtime.supervisor import Service, Supervisor, default_services

PY = sys.executable
SLEEP = [PY, "-c", "import time; time.sleep(60)"]
IGNORE_TERM = [
    PY, "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
]  # fmt: skip


def fast(services, **kwargs) -> Supervisor:
    options = {"startup_timeout": 5, "health_interval": 0.1, "poll_interval": 0.05,
               "grace_seconds": 2, "health_check": lambda url: True}  # fmt: skip
    options.update(kwargs)
    return Supervisor(services, **options)


def test_exit_of_one_service_stops_the_other_and_propagates_code():
    backend = Service("backend", [PY, "-c", "import time, sys; time.sleep(0.3); sys.exit(3)"])
    frontend = Service("frontend", SLEEP)
    assert fast([backend, frontend]).run() == 3
    assert not frontend.running


def test_clean_exit_of_a_service_is_still_a_failure():
    backend = Service("backend", [PY, "-c", "pass"])
    frontend = Service("frontend", SLEEP)
    supervisor = fast([backend, frontend])
    # backend exits immediately, which fails its startup wait (no longer running)
    assert supervisor.run() == 1


def test_stop_request_shuts_down_gracefully():
    services = [Service("backend", SLEEP), Service("frontend", SLEEP)]
    supervisor = fast(services)
    threading.Timer(0.4, supervisor.request_stop).start()
    started = time.monotonic()
    assert supervisor.run() == 0
    assert time.monotonic() - started < 3
    assert not any(s.running for s in services)


def test_process_ignoring_sigterm_is_killed_after_grace_period():
    services = [Service("backend", IGNORE_TERM)]
    supervisor = fast(services, grace_seconds=0.5)
    threading.Timer(0.3, supervisor.request_stop).start()
    assert supervisor.run() == 0
    assert not services[0].running


def test_frontend_waits_for_backend_health():
    calls: list[str] = []

    def health(url: str) -> bool:
        calls.append(url)
        return len(calls) >= 3  # healthy on the third check

    backend = Service("backend", SLEEP, health_url="http://backend/health")
    frontend = Service("frontend", SLEEP)
    supervisor = fast([backend, frontend], health_check=health, health_interval=60)
    threading.Timer(0.6, supervisor.request_stop).start()
    assert supervisor.run() == 0
    assert calls[:3] == ["http://backend/health"] * 3


def test_startup_health_timeout_fails():
    backend = Service("backend", SLEEP, health_url="http://backend/health")
    frontend = Service("frontend", SLEEP)
    supervisor = fast([backend, frontend], startup_timeout=0.3, health_check=lambda u: False)
    assert supervisor.run() == 1
    assert frontend.process is None  # never started
    assert not backend.running


def test_watchdog_restarts_container_after_repeated_backend_failures():
    state = {"healthy": True}
    backend = Service("backend", SLEEP, health_url="http://backend/health")
    supervisor = fast([backend], failure_threshold=3, health_check=lambda u: state["healthy"])
    threading.Timer(0.3, lambda: state.update(healthy=False)).start()
    assert supervisor.run() == 1
    assert not backend.running


def test_default_services_bind_backend_privately_and_frontend_publicly():
    backend, frontend = default_services()
    assert backend.argv[backend.argv.index("--host") + 1] == "127.0.0.1"
    assert "--server.address=0.0.0.0" in frontend.argv
    assert "--server.port=8501" in frontend.argv
    assert frontend.argv[4].endswith("frontend/app.py")


def test_healthcheck_requires_both_services(monkeypatch, capsys):
    monkeypatch.setattr(healthcheck, "http_ok", lambda url: "8000" in url)
    assert healthcheck.main() == 1
    monkeypatch.setattr(healthcheck, "http_ok", lambda url: True)
    assert healthcheck.main() == 0


def test_watchdog_uses_dependency_free_liveness_endpoint():
    backend, _ = default_services()
    assert backend.health_url == "http://127.0.0.1:8000/health/live"
