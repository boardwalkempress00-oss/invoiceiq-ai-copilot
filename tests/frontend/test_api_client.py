from __future__ import annotations

import httpx
import pytest

from invoiceiq.frontend.api_client import ApiClient, ApiError


def test_endpoints_against_real_backend(api_client):
    assert api_client.health()["status"] == "ok"
    assert len(api_client.list_invoices()) == 3
    assert api_client.get_invoice("inv-002")["invoice_number"] == "FBC-2026-0187"
    assert api_client.validate("inv-002")["status"] == "Flagged"
    assert "PP-3.1" in api_client.policy_sections()


def test_static_data_is_cached_per_session(api_client):
    api_client.list_invoices()
    api_client.list_invoices()
    assert [m.path for m in api_client.measurements] == ["/invoices"]
    api_client.clear_cache()
    api_client.list_invoices()
    assert len(api_client.measurements) == 2


def test_chat_records_real_measurement(api_client):
    response = api_client.chat(
        invoice_id="inv-003",
        question="Why was this flagged?",
        session_id="frontend-test-1",
        history=[],
    )
    assert response["mode"] == "ai"
    m = api_client.measurements[-1]
    assert m.path == "/chat" and m.status_code == 200
    assert m.correlation_id == response["correlation_id"]
    assert m.server_ms is not None and m.round_trip_ms >= 0
    assert m.ai_ms == response["timings"]["ai_ms"]
    assert m.token_source == "estimated"
    assert m.estimated_cost is None  # prices not configured → no invented cost


def test_api_error_carries_backend_message_and_correlation_id(api_client):
    with pytest.raises(ApiError) as info:
        api_client.get_invoice("inv-999")
    assert info.value.status_code == 404
    assert "not found" in info.value.message
    assert info.value.correlation_id


def test_unreachable_backend_gives_friendly_error():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = ApiClient(
        http=httpx.Client(base_url="http://x", transport=httpx.MockTransport(refuse))
    )
    with pytest.raises(ApiError) as info:
        client.health()
    assert "not reachable" in info.value.message
    assert client.measurements[-1].status_code is None


def test_timeout_gives_friendly_error():
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = ApiClient(http=httpx.Client(base_url="http://x", transport=httpx.MockTransport(slow)))
    with pytest.raises(ApiError) as info:
        client.list_invoices()
    assert "too long" in info.value.message


def test_non_json_error_response():
    client = ApiClient(http=httpx.Client(
        base_url="http://x", transport=httpx.MockTransport(lambda r: httpx.Response(502, text="x"))
    ))  # fmt: skip
    with pytest.raises(ApiError) as info:
        client.health()
    assert info.value.status_code == 502
    assert info.value.message == "The request could not be completed."


def test_base_url_from_environment(monkeypatch):
    monkeypatch.setenv("INVOICEIQ_API_URL", "http://backend:9000/")
    assert ApiClient().base_url == "http://backend:9000"
