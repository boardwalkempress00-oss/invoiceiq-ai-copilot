"""Process supervisor for the single-container deployment.

Runs the FastAPI backend (127.0.0.1:8000, not reachable from outside the container) and the
Streamlit frontend (0.0.0.0:8501, the only public port). `tini` is PID 1 and forwards signals.

Behaviour:

* The backend starts first; Streamlit starts only once the backend reports healthy.
* If either process exits, the other is stopped and the supervisor exits non-zero, so the
  container platform restarts the whole container (fail fast rather than half-working).
* The backend's /health endpoint is polled. Container Apps probes can only reach the public
  port, so this watchdog is what detects a hung backend.
* SIGTERM/SIGINT trigger a graceful shutdown: frontend first, then backend, each given a grace
  period before SIGKILL.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType

logger = logging.getLogger("invoiceiq.supervisor")

# Liveness endpoint: independent of Table Storage and Azure OpenAI availability.
BACKEND_HEALTH_URL = "http://127.0.0.1:8000/health/live"
FRONTEND_HEALTH_URL = "http://127.0.0.1:8501/_stcore/health"


def http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except OSError:
        return False


@dataclass
class Service:
    name: str
    argv: Sequence[str]
    health_url: str | None = None
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None


class Supervisor:
    def __init__(
        self,
        services: Sequence[Service],
        *,
        startup_timeout: float = 60.0,
        health_interval: float = 15.0,
        failure_threshold: int = 3,
        grace_seconds: float = 20.0,
        poll_interval: float = 0.5,
        health_check: Callable[[str], bool] = http_ok,
        watch_health: Sequence[str] = ("backend",),
    ) -> None:
        self.services = list(services)
        self.startup_timeout = startup_timeout
        self.health_interval = health_interval
        self.failure_threshold = failure_threshold
        self.grace_seconds = grace_seconds
        self.poll_interval = poll_interval
        self.health_check = health_check
        self.watch_health = set(watch_health)
        self._stop = threading.Event()

    def request_stop(self, signum: int | None = None, _frame: FrameType | None = None) -> None:
        if signum is not None:
            logger.info("supervisor.signal", extra={"signal": signal.Signals(signum).name})
        self._stop.set()

    def _start(self, service: Service) -> None:
        logger.info("supervisor.start", extra={"service": service.name})
        service.process = subprocess.Popen(list(service.argv))

    def _wait_healthy(self, service: Service) -> bool:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            if not service.running:
                return False
            if service.health_url is None or self.health_check(service.health_url):
                return True
            self._stop.wait(self.poll_interval)
        return False

    def _exited(self) -> Service | None:
        return next((s for s in self.services if s.process and s.process.poll() is not None), None)

    def shutdown(self) -> None:
        """Stop services in reverse start order: SIGTERM, wait, then SIGKILL."""
        for service in reversed(self.services):
            if service.running:
                assert service.process is not None
                logger.info("supervisor.stop", extra={"service": service.name})
                service.process.terminate()
        deadline = time.monotonic() + self.grace_seconds
        for service in reversed(self.services):
            if service.process is None:
                continue
            try:
                service.process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                logger.warning("supervisor.kill", extra={"service": service.name})
                service.process.kill()
                service.process.wait()

    def run(self) -> int:
        try:
            for service in self.services:
                self._start(service)
                if not self._wait_healthy(service):
                    if self._stop.is_set():
                        return 0
                    logger.error("supervisor.startup_failed", extra={"service": service.name})
                    return 1
            logger.info("supervisor.ready")
            return self._monitor()
        finally:
            self.shutdown()

    def _monitor(self) -> int:
        failures = 0
        next_check = time.monotonic() + self.health_interval
        while not self._stop.is_set():
            if exited := self._exited():
                code = exited.process.returncode if exited.process else 1
                logger.error("supervisor.exited", extra={"service": exited.name, "code": code})
                return code or 1
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + self.health_interval
                unhealthy = [
                    s.name
                    for s in self.services
                    if s.name in self.watch_health
                    and s.health_url
                    and not self.health_check(s.health_url)
                ]
                failures = failures + 1 if unhealthy else 0
                if failures:
                    logger.warning(
                        "supervisor.unhealthy", extra={"services": unhealthy, "count": failures}
                    )
                if failures >= self.failure_threshold:
                    logger.error("supervisor.health_failed", extra={"services": unhealthy})
                    return 1
            self._stop.wait(self.poll_interval)
        logger.info("supervisor.stopping")
        return 0


def default_services() -> list[Service]:
    import invoiceiq.frontend

    app_path = Path(invoiceiq.frontend.__file__).parent / "app.py"
    python = sys.executable
    return [
        Service(
            "backend",
            [
                python, "-m", "uvicorn", "invoiceiq.api.main:app_factory", "--factory",
                "--host", "127.0.0.1", "--port", "8000",
                "--timeout-graceful-shutdown", "10", "--no-server-header",
            ],
            BACKEND_HEALTH_URL,
        ),
        Service(
            "frontend",
            [
                python, "-m", "streamlit", "run", str(app_path),
                "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true",
                "--server.fileWatcherType=none", "--browser.gatherUsageStats=false",
            ],
            FRONTEND_HEALTH_URL,
        ),
    ]  # fmt: skip


def main() -> int:
    from invoiceiq.logging_config import configure_logging

    configure_logging(os.getenv("LOG_LEVEL", "INFO"))
    supervisor = Supervisor(default_services())
    signal.signal(signal.SIGTERM, supervisor.request_stop)
    signal.signal(signal.SIGINT, supervisor.request_stop)
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())
