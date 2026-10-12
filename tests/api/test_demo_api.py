"""Demo endpoints: feature flags, contract, provenance and isolation from live processing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from invoiceiq.api.main import create_app
from invoiceiq.configuration import Settings

SESSION = "session-abcdefgh"
ROOT = Path(__file__).resolve().parents[2]


def make(**overrides: object) -> TestClient:
    return TestClient(create_app(Settings(_env_file=None, **overrides)))  # type: ignore[arg-type]


@pytest.fixture
def demo() -> TestClient:
    return make(demo_mode_enabled=True)


def run(client: TestClient, scenario_id: str = "demo-vat-error", **extra: object):
    return client.post(
        "/demo/runs", json={"session_id": SESSION, "scenario_id": scenario_id, **extra}
    )


# --- Feature flags ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("demo_on", "async_on"), [(False, False), (True, False), (False, True), (True, True)]
)
def test_flag_matrix(demo_on: bool, async_on: bool):
    client = make(demo_mode_enabled=demo_on, async_processing_enabled=async_on)
    health = client.get("/health").json()
    assert (health["demo_mode"], health["async_processing"]) == (demo_on, async_on)

    for response in (client.get("/demo/scenarios"), run(client), client.get("/demo/reference")):
        if demo_on:
            assert response.status_code == 200
        else:
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "feature_disabled"

    live = client.post("/submissions", json={"session_id": SESSION, "sample_invoice_id": "inv-001"})
    if async_on:
        assert live.status_code == 202
    else:  # the demo never switches live processing on
        assert live.status_code == 404
        assert live.json()["error"]["code"] == "feature_disabled"


def test_demo_is_off_by_default():
    settings = Settings(_env_file=None)
    assert settings.demo_mode_enabled is False and settings.async_processing_enabled is False
    assert make().get("/health").json()["demo_mode"] is False


def test_demo_mode_reads_its_own_environment_variable(monkeypatch):
    monkeypatch.setenv("DEMO_MODE_ENABLED", "true")
    settings = Settings(_env_file=None)
    assert settings.demo_mode_enabled is True
    assert settings.async_processing_enabled is False  # one flag never implies the other


# --- Contract and provenance --------------------------------------------------------------------


def test_scenarios_list(demo):
    scenarios = demo.get("/demo/scenarios").json()
    assert len(scenarios) == 10
    first = scenarios[0]
    assert set(first) == {
        "scenario_id", "title", "story", "invoice_number", "vendor", "rules_exercised",
    }  # fmt: skip
    assert "data" not in first  # raw invoice bytes are not exposed


def test_run_is_labelled_simulated_with_a_full_result(demo):
    body = run(demo).json()
    assert body["processing_mode"] == "simulated"
    assert "No Azure service was used" in body["provenance"]
    assert body["status"] == "Completed"
    assert body["result"]["status"] == "Requires Review"
    assert {f["rule_id"] for f in body["result"]["findings"]} == {"CALC-003", "CALC-004"}
    assert [s["step"] for s in body["steps"]] == [
        "Submission recorded (Pending)",
        "Simulated BlobCreated event queued",
        "Processor claimed the submission",
        "Completed",
    ]
    offsets = [s["offset_ms"] for s in body["steps"]]
    assert offsets[0] == 0 and offsets == sorted(offsets)
    assert body["invoice"]["invoice_number"]


def test_every_scenario_matches_its_catalog_expectation(demo):
    catalog = json.loads((ROOT / "data/demo-scenarios/catalog.json").read_text())
    for entry in catalog["scenarios"]:
        result = run(demo, entry["scenario_id"]).json()["result"]
        assert result["status"] == entry["expected_status"], entry["scenario_id"]
        rules = sorted({f["rule_id"] for f in result["findings"]})
        assert rules == entry["expected_rules"], entry["scenario_id"]


def test_reference_is_labelled_historical_and_matches_the_evidence(demo):
    ref = demo.get("/demo/reference").json()
    assert ref["label"] == "Verified in Azure — historical test run, not live"
    assert ref["source"] == "docs/evidence/production-verification.md"
    assert [s["offset_seconds"] for s in ref["steps"]] == [0.0, 0.8, 4.5, 26.6, 29.3]
    summary = ref["summary"]
    assert (summary["upload_to_result_seconds_min"], summary["upload_to_result_seconds_max"]) == (
        24.5,
        35.7,
    )
    assert summary["duplicates"] == summary["retries"] == summary["dead_letters"] == 0
    evidence = (ROOT / ref["source"]).read_text(encoding="utf-8")
    for figure in ("+0.8 s", "+4.5 s", "+26.6 s", "+29.3 s", "24.5 – 35.7 s", "KFS-2026-0203"):
        assert figure in evidence, figure
    assert ref["invoice"].startswith("KFS-2026-0203")


# --- Input safety: catalog IDs only, no upload path ---------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"session_id": SESSION, "scenario_id": "../../etc/passwd"},
        {"session_id": SESSION, "scenario_id": "inv-001"},
        {"session_id": SESSION, "scenario_id": "demo-clean", "content": "{}"},
        {"session_id": SESSION, "scenario_id": "demo-clean", "invoice": {"total": 1}},
        {"session_id": "x", "scenario_id": "demo-clean"},
        {"scenario_id": "demo-clean"},
    ],
)
def test_malformed_or_upload_like_requests_are_rejected(demo, body):
    response = demo.post("/demo/runs", json=body)
    assert response.status_code == 422


def test_unknown_scenario_is_404(demo):
    response = run(demo, "demo-not-in-catalog")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "scenario_not_found"


def test_demo_allowance_is_enforced(monkeypatch):
    client = make(demo_mode_enabled=True, demo_session_daily_limit=1)
    assert run(client).status_code == 200
    response = run(client)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "demo_limit_reached"


def test_openapi_exposes_only_catalog_demo_endpoints(demo):
    paths = demo.get("/openapi.json").json()["paths"]
    demo_paths = {p: set(ops) for p, ops in paths.items() if p.startswith("/demo")}
    assert demo_paths == {
        "/demo/scenarios": {"get"},
        "/demo/runs": {"post"},
        "/demo/reference": {"get"},
    }
    run_schema = paths["/demo/runs"]["post"]["requestBody"]["content"]
    assert set(run_schema) == {"application/json"}  # no multipart/file upload


def test_demo_runs_do_not_touch_live_submissions():
    client = make(demo_mode_enabled=True, async_processing_enabled=True)
    run_id = run(client).json()["run_id"]
    assert client.get(f"/submissions/{run_id}").status_code == 404
    live = client.post("/submissions", json={"session_id": SESSION, "sample_invoice_id": "inv-001"})
    assert live.status_code == 202  # demo runs did not consume the live upload allowance
