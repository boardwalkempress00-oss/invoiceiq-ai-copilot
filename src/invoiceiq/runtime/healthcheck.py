"""Container health check: exits 0 only if both the backend and the frontend are healthy.

Used by the Docker HEALTHCHECK. (Azure Container Apps probes the public Streamlit port directly;
the supervisor's watchdog covers the internal backend.)
"""

from __future__ import annotations

import sys

from invoiceiq.runtime.supervisor import BACKEND_HEALTH_URL, FRONTEND_HEALTH_URL, http_ok


def main() -> int:
    results = {url: http_ok(url) for url in (BACKEND_HEALTH_URL, FRONTEND_HEALTH_URL)}
    for url, ok in results.items():
        print(f"{'ok  ' if ok else 'FAIL'} {url}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
