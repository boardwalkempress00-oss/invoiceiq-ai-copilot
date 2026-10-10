"""Frontend fixtures: an ApiClient wired to the real FastAPI app in-process (mock AI)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from invoiceiq.api.main import create_app
from invoiceiq.configuration import Settings
from invoiceiq.frontend.api_client import ApiClient

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "src" / "invoiceiq" / "frontend"
APP_PATH = FRONTEND_DIR / "app.py"


def make_api_client(**settings: object) -> ApiClient:
    app = create_app(Settings(_env_file=None, **settings))  # type: ignore[arg-type]
    return ApiClient(base_url="http://testserver", http=TestClient(app))


@pytest.fixture
def api_client() -> ApiClient:
    return make_api_client()


@pytest.fixture
def run_page() -> Callable[..., AppTest]:
    """Run the full app (navigation + sidebar) on a given page with an injected API client."""

    def _run(page: str | None = None, client: ApiClient | None = None, **state: object) -> AppTest:
        at = AppTest.from_file(str(APP_PATH), default_timeout=30)
        at.session_state["api_client"] = client or make_api_client()
        for key, value in state.items():
            at.session_state[key] = value
        at.run()
        if page:
            at.switch_page(f"views/{page}.py")
            at.run()
        return at

    return _run
