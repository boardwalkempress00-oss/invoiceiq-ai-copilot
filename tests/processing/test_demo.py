"""Public demo: catalog accuracy, real processing in memory, bounds, and isolation from Azure."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from invoiceiq.configuration import Settings
from invoiceiq.core.invoice_validator import validate_invoice
from invoiceiq.core.models import Invoice
from invoiceiq.processing import demo as demo_module
from invoiceiq.processing.demo import DemoCatalog, DemoError, DemoService
from invoiceiq.processing.models import SubmissionSource, SubmissionStatus

ALL_RULES = {
    "REQ-001", "REQ-002", "CALC-001", "CALC-002", "CALC-003", "CALC-004",
    "VAT-001", "VAT-002", "CUR-001", "APR-001", "APR-002", "APR-003",
}  # fmt: skip


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, demo_mode_enabled=True, **overrides)  # type: ignore[arg-type]


@pytest.fixture
def catalog() -> DemoCatalog:
    return DemoCatalog.load(settings().demo_scenarios_dir)


# --- Catalog -----------------------------------------------------------------------------------


def test_catalog_has_ten_scenarios_covering_every_rule(catalog):
    scenarios = catalog.list()
    assert len(scenarios) == 10
    assert len({s.scenario_id for s in scenarios}) == 10
    assert set().union(*(s.expected_rules for s in scenarios)) == ALL_RULES


def test_catalog_expectations_match_the_validation_engine(catalog):
    for s in catalog.list():
        result = validate_invoice(Invoice.model_validate_json(s.data))
        assert result.status == s.expected_status, s.scenario_id
        assert sorted({f.rule_id for f in result.findings}) == list(s.expected_rules), s.scenario_id


def test_scenarios_are_visibly_fictional(catalog):
    for s in catalog.list():
        invoice = json.loads(s.data)
        assert invoice["notes"].startswith("Fictional demo scenario"), s.scenario_id
        assert invoice["vendor"]["email"].endswith(".example")
        assert invoice["vendor"]["vat_number"].startswith("DEMO-VAT-")
        approval = invoice.get("approval") or {}
        assert "fictional" in (approval.get("approved_by") or "fictional")


def test_catalog_rejects_unsafe_entries(tmp_path):
    (tmp_path / "x.json").write_text("{}")
    for bad in ({"scenario_id": "../etc", "file": "x.json"},
                {"scenario_id": "demo-ok", "file": "../x.json"}):  # fmt: skip
        entry = {"title": "t", "story": "s", "expected_status": "Passed", "expected_rules": []}
        (tmp_path / "catalog.json").write_text(json.dumps({"scenarios": [{**entry, **bad}]}))
        with pytest.raises(ValueError, match="invalid scenario"):
            DemoCatalog.load(tmp_path)


# --- Runs ----------------------------------------------------------------------------------------


def test_every_scenario_runs_through_the_real_processor(catalog):
    service = DemoService.from_settings(settings())
    for s in catalog.list():
        run = service.run(session_id="session-abcdefgh", scenario_id=s.scenario_id)
        record = run.record
        assert (
            record.status == SubmissionStatus.COMPLETED and record.source == SubmissionSource.DEMO
        )
        assert record.attempts == 1 and record.queued_at and record.started_at
        payload = json.loads(record.result_json)
        assert payload["result"]["status"] == s.expected_status
        assert sorted({f["rule_id"] for f in payload["result"]["findings"]}) == list(
            s.expected_rules
        )
    assert service.blobs.blobs == {}  # blobs are dropped after each run
    assert service.queue.active_count() == 0 and not service.queue.dead_letters


def test_unknown_scenario_is_rejected():
    service = DemoService.from_settings(settings())
    with pytest.raises(DemoError) as err:
        service.run(session_id="session-abcdefgh", scenario_id="demo-does-not-exist")
    assert (err.value.status_code, err.value.code) == (404, "scenario_not_found")


def test_session_and_global_demo_allowances():
    service = DemoService.from_settings(
        settings(demo_session_daily_limit=2, demo_global_daily_limit=3)
    )
    for _ in range(2):
        service.run(session_id="session-one-abc", scenario_id="demo-clean")
    with pytest.raises(DemoError) as err:
        service.run(session_id="session-one-abc", scenario_id="demo-clean")
    assert err.value.code == "demo_limit_reached"
    service.run(session_id="session-two-abc", scenario_id="demo-clean")
    with pytest.raises(DemoError):  # global cap of 3 reached
        service.run(session_id="session-three-abc", scenario_id="demo-clean")


# --- Bounded memory ------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def test_memory_is_capped_at_max_runs():
    service = DemoService.from_settings(
        settings(demo_max_runs_in_memory=10, demo_session_daily_limit=1000)
    )
    ids = [
        service.run(session_id="session-abcdefgh", scenario_id="demo-clean").record.submission_id
        for _ in range(25)
    ]
    assert len(service.store) == 10
    assert service.store.get(ids[0]) is None and service.store.get(ids[-1]) is not None


def test_runs_expire_after_the_ttl():
    clock = Clock()
    service = DemoService(
        settings(demo_run_ttl_seconds=60),
        DemoCatalog.load(settings().demo_scenarios_dir),
        {},
        clock=clock,
    )
    first = service.run(session_id="session-abcdefgh", scenario_id="demo-clean").record
    clock.now += timedelta(seconds=61)
    service.run(session_id="session-abcdefgh", scenario_id="demo-clean")  # triggers eviction
    assert service.store.get(first.submission_id) is None
    assert len(service.store) == 1


def test_only_todays_allowance_counters_are_kept():
    clock = Clock()
    service = DemoService(
        settings(), DemoCatalog.load(settings().demo_scenarios_dir), {}, clock=clock
    )
    service.run(session_id="session-abcdefgh", scenario_id="demo-clean")
    clock.now += timedelta(days=1)
    service.run(session_id="session-abcdefgh", scenario_id="demo-clean")
    assert {day for day, _ in service.store._quota} == {"2026-10-13"}


# --- Isolation from Azure ------------------------------------------------------------------------


def test_demo_never_constructs_azure_clients(monkeypatch):
    """Even with Azure submission settings present, the demo touches no Azure client."""
    import azure.data.tables
    import azure.servicebus
    import azure.storage.blob

    from invoiceiq.processing import azure_table_store, blobs, messages

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("demo mode constructed an Azure client")

    for target in (
        (blobs.AzureBlobStore, "from_settings"),
        (azure_table_store.AzureTableSubmissionStore, "from_settings"),
        (messages.AzureServiceBusSource, "from_settings"),
        (azure.storage.blob.ContainerClient, "__init__"),
        (azure.storage.blob.BlobServiceClient, "__init__"),
        (azure.data.tables.TableClient, "__init__"),
        (azure.servicebus.ServiceBusClient, "__init__"),
    ):
        monkeypatch.setattr(*target, forbidden)

    from fastapi.testclient import TestClient

    from invoiceiq.api.main import create_app

    app_settings = settings(
        async_processing_enabled=False,
        submission_backend="azure",
        azure_blob_endpoint="https://example.blob.core.windows.net",
        azure_table_endpoint="https://example.table.core.windows.net",
        servicebus_namespace="example.servicebus.windows.net",
    )
    client = TestClient(create_app(app_settings))
    for s in client.get("/demo/scenarios").json():
        run = client.post(
            "/demo/runs", json={"session_id": "session-abcdefgh", "scenario_id": s["scenario_id"]}
        )
        assert run.status_code == 200 and run.json()["status"] == "Completed"
    assert client.get("/health").json()["async_processing"] is False


def test_demo_process_loads_no_azure_storage_or_messaging_sdk():
    code = (
        "import sys\n"
        "from fastapi.testclient import TestClient\n"
        "from invoiceiq.api.main import create_app\n"
        "from invoiceiq.configuration import Settings\n"
        "c = TestClient(create_app(Settings(_env_file=None, demo_mode_enabled=True)))\n"
        "for s in c.get('/demo/scenarios').json():\n"
        "    assert c.post('/demo/runs', json={'session_id': 'session-abcdefgh',"
        " 'scenario_id': s['scenario_id']}).status_code == 200\n"
        "bad = sorted(m for m in sys.modules if m.startswith("
        "('azure.storage', 'azure.servicebus', 'azure.data.tables')))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""


def test_demo_module_imports_only_in_memory_implementations():
    names = set(vars(demo_module))
    assert {"InMemoryBlobStore", "InMemoryQueue", "InMemorySubmissionStore"} <= names
    assert not {"AzureBlobStore", "AzureServiceBusSource", "AzureTableSubmissionStore"} & names
