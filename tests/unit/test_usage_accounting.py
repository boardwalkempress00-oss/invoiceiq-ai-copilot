"""How chat requests reserve and settle usage, and production safeguards for the usage store."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from invoiceiq.ai.ai_service import ChatMode, cost_micro, estimate_usage
from invoiceiq.ai.providers import AIErrorKind, TokenUsage
from invoiceiq.api.main import create_app, create_usage_store
from invoiceiq.configuration import Settings
from invoiceiq.core.usage_limiter import InMemoryUsageStore, UnavailableUsageStore, UsageStoreError
from invoiceiq.storage.azure_table_usage_store import AzureTableUsageStore
from tests.unit.test_ai_service import FailingProvider, ScriptedProvider, ask, make_service

PRICES = {"ai_price_input_per_1m": "0.40", "ai_price_output_per_1m": "1.60"}


def snapshot(service):
    return service._limiter.snapshot()  # test-only access to the limiter


def test_cost_micro_is_tokens_times_price_rounded_up():
    settings = Settings(_env_file=None, **PRICES)
    # 1000 × 0.40 + 200 × 1.60 = 720 micro-USD = $0.00072
    assert cost_micro(TokenUsage(1000, 200, "provider"), settings) == 720
    assert cost_micro(TokenUsage(1, 0, "provider"), settings) == 1  # 0.4 rounds up


def test_estimate_is_an_upper_bound():
    settings = Settings(_env_file=None, **PRICES)
    messages = [{"role": "user", "content": "x" * 3000}]
    estimate = estimate_usage(messages, 700, settings)
    assert estimate.tokens == 1000 + 10 + 700  # chars/3 + per-message overhead + max output
    assert estimate.cost_micro == int(Decimal(1010) * Decimal("0.40") + 700 * Decimal("1.60"))


def test_successful_chat_reserves_then_reconciles_actual_usage():
    service = make_service(ScriptedProvider("ok [VAL-RESULT]"), **PRICES)
    ask(service, "Explain")
    usage = snapshot(service)
    assert usage.requests == 1
    assert usage.reserved_cost_micro == 0
    assert usage.spent_cost_micro == 720  # actual 1000/200 tokens from the provider
    assert (usage.actual_prompt_tokens, usage.actual_completion_tokens) == (1000, 200)
    assert usage.estimated_tokens > 0 and usage.unreconciled == 0


@pytest.mark.parametrize(
    ("kind", "charged"),
    [
        (AIErrorKind.TIMEOUT, True),
        (AIErrorKind.UNAVAILABLE, True),
        (AIErrorKind.RATE_LIMITED, True),  # may follow a retried attempt that was processed
        (AIErrorKind.CONTENT_FILTERED, True),
        (AIErrorKind.AUTHENTICATION, False),  # 401/403 are not retried: nothing reached the model
        (AIErrorKind.CONFIGURATION, False),  # no call was made
    ],
)
def test_failed_calls_are_settled_conservatively(kind, charged):
    service = make_service(FailingProvider(kind), **PRICES)
    outcome = ask(service, "Explain")
    assert outcome.mode == ChatMode.READ_ONLY
    usage = snapshot(service)
    assert usage.reserved_cost_micro == 0
    assert (usage.spent_cost_micro > 0) is charged
    assert usage.unreconciled == (1 if charged else 0)
    assert usage.requests == 1


def test_daily_budget_blocks_before_calling_the_model():
    # Through the real app wiring, which passes GLOBAL_DAILY_COST_LIMIT to the limiter.
    provider = ScriptedProvider("ok [VAL-RESULT]")
    settings = Settings(_env_file=None, global_daily_cost_limit="0.0001", **PRICES)
    client = TestClient(create_app(settings, provider=provider))
    body = client.post(
        "/chat", json={"invoice_id": "inv-001", "question": "Explain", "session_id": "s" * 10}
    ).json()
    assert body["read_only_reason"] == "daily_budget_reached"
    assert provider.requests == []


class ExplodingProvider(ScriptedProvider):
    def generate(self, request):
        raise RuntimeError("unexpected bug")


def test_unexpected_provider_exception_is_charged_and_reraised():
    service = make_service(ExplodingProvider("x"), **PRICES)
    with pytest.raises(RuntimeError):
        ask(service, "Explain")
    usage = snapshot(service)
    assert usage.reserved_cost_micro == 0 and usage.spent_cost_micro > 0


class SettleFailsStore(InMemoryUsageStore):
    """Reservation succeeds; every later write fails (storage outage mid-request)."""

    def __init__(self) -> None:
        super().__init__()
        self.commits = 0

    def commit(self, day, writes):
        self.commits += 1
        if self.commits > 1:
            raise ConnectionError("storage went away")
        return super().commit(day, writes)


def test_settlement_failure_keeps_reservation_counted():
    store = SettleFailsStore()
    service = make_service(ScriptedProvider("ok [VAL-RESULT]"), store=store, **PRICES)
    assert ask(service, "Explain").mode == ChatMode.AI  # the user still gets the answer
    usage = store.read(service._limiter._clock(), ["global"])["global"].counters
    assert usage.reserved_cost_micro > 0  # over-counted (safe), never under-counted


# --- Store configuration -------------------------------------------------------------------


def test_production_refuses_connection_string():
    settings = Settings(
        _env_file=None,
        app_env="production",
        usage_store="azure_table",
        azure_table_connection_string=SecretStr("UseDevelopmentStorage=true"),
    )
    with pytest.raises(UsageStoreError, match="Azurite only"):
        AzureTableUsageStore.from_settings(settings)
    store = create_usage_store(settings)
    assert isinstance(store, UnavailableUsageStore)


def test_missing_table_endpoint_fails_closed():
    settings = Settings(_env_file=None, usage_store="azure_table")
    store = create_usage_store(settings)
    assert isinstance(store, UnavailableUsageStore)
    client = TestClient(create_app(settings))
    ai = client.get("/health").json()["ai"]
    assert ai["available"] is False and ai["block_reason"] == "limiter_unavailable"
    body = client.post(
        "/chat", json={"invoice_id": "inv-001", "question": "Explain", "session_id": "s" * 10}
    ).json()
    assert body["read_only_reason"] == "limiter_unavailable"


def test_managed_identity_client_built_without_network():
    settings = Settings(
        _env_file=None,
        usage_store="azure_table",
        azure_table_endpoint="https://example.table.core.windows.net",
    )
    store = AzureTableUsageStore.from_settings(settings)
    assert store.shared_durable is True
    assert store._table.table_name == "aiusage"


# --- Mock vs live cost accounting --------------------------------------------------------------


def test_mock_chat_counts_requests_but_spends_nothing():
    service = make_service(**PRICES)  # default provider: MockChatProvider (not live)
    outcome = ask(service, "Explain")
    assert outcome.mode == ChatMode.AI
    assert outcome.usage.source == "simulated"
    assert outcome.estimated_cost == Decimal("0") and outcome.cost_is_simulated
    usage = snapshot(service)
    assert usage.requests == 1  # still counted against request limits
    assert (usage.reserved_cost_micro, usage.spent_cost_micro) == (0, 0)
    assert usage.estimated_tokens == 0  # simulated tokens are not reserved
    assert (usage.actual_prompt_tokens, usage.actual_completion_tokens) == (0, 0)
    assert usage.unreconciled == 0


def test_mock_chat_respects_session_limit():
    service = make_service(session_request_limit=1, **PRICES)
    assert ask(service, "Explain").mode == ChatMode.AI
    blocked = ask(service, "Explain")
    assert blocked.mode == ChatMode.READ_ONLY
    assert blocked.read_only_reason.value == "session_limit_reached"


def test_mock_chat_respects_global_request_limit():
    service = make_service(global_daily_request_limit=1, **PRICES)
    assert ask(service, "Explain", session="session-01").mode == ChatMode.AI
    blocked = ask(service, "Explain", session="session-02")
    assert blocked.read_only_reason.value == "daily_limit_reached"


def test_mock_chat_does_not_consume_monetary_budget():
    # Through the real app wiring: a zero monetary budget must not block mock answers.
    settings = Settings(_env_file=None, global_daily_cost_limit="0", **PRICES)
    client = TestClient(create_app(settings))
    for i in range(3):
        body = client.post(
            "/chat",
            json={
                "invoice_id": "inv-001",
                "question": "Explain",
                "session_id": f"mock-sess-{i:02d}",
            },
        ).json()
        assert body["mode"] == "ai" and body["estimated_cost"] == "0"


def test_live_chat_reports_real_estimated_cost():
    service = make_service(ScriptedProvider("ok [VAL-RESULT]"), **PRICES)
    outcome = ask(service, "Explain")
    assert outcome.cost_is_simulated is False
    assert outcome.estimated_cost == Decimal("0.000720")  # 1000 × 0.40 + 200 × 1.60 per 1M
    assert snapshot(service).estimated_tokens > 0  # live reservations include a token estimate


def test_live_provider_without_reported_usage_is_charged_at_estimate():
    class NoUsageProvider(ScriptedProvider):
        def generate(self, request):
            result = super().generate(request)
            return type(result)(text=result.text, model=result.model, usage=None)

    service = make_service(NoUsageProvider("ok [VAL-RESULT]"), **PRICES)
    ask(service, "Explain")
    usage = snapshot(service)
    assert usage.spent_cost_micro > 0 and usage.unreconciled == 1
    assert usage.actual_prompt_tokens == 0  # nothing invented
