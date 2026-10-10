"""Azure OpenAI provider tests using a fake SDK client. No network calls are made."""

from __future__ import annotations

from types import SimpleNamespace

import httpx2
import openai
import pytest
from pydantic import SecretStr

from invoiceiq.ai.context import build_context
from invoiceiq.ai.providers import (
    AIErrorKind,
    AIProviderError,
    AzureOpenAIChatProvider,
    GenerationRequest,
)
from invoiceiq.configuration import Settings
from invoiceiq.core.invoice_service import InvoiceService, JsonInvoiceRepository
from tests.conftest import DATA_DIR

_REQUEST = httpx2.Request("POST", "https://example.invalid/openai/v1/chat/completions")


def _status_error(cls: type[openai.APIStatusError], status: int, body: object = None):
    return cls("error", response=httpx2.Response(status, request=_REQUEST), body=body)


class FakeCompletions:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def fake_client(outcome: object) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(outcome)))


def completion(content: str | None, finish_reason: str = "stop", usage: bool = True):
    return SimpleNamespace(
        model="gpt-test",
        choices=[
            SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content))
        ],
        usage=SimpleNamespace(prompt_tokens=900, completion_tokens=120) if usage else None,
    )


SETTINGS = Settings(
    _env_file=None,
    ai_provider="azure_openai",
    azure_openai_endpoint="https://example.invalid",
    azure_openai_deployment="chat-deployment",
    ai_max_output_tokens=321,
)


@pytest.fixture(scope="module")
def request_() -> GenerationRequest:
    invoices = InvoiceService(JsonInvoiceRepository(DATA_DIR / "invoices"))
    invoice = invoices.get_invoice("inv-001")
    context = build_context(invoice, invoices.process(invoice), [], max_chars=5_000)
    return GenerationRequest(
        messages=[{"role": "user", "content": "hi"}],
        context=context,
        question="hi",
        max_output_tokens=321,
    )


def test_successful_completion_maps_text_usage_and_limits(request_):
    client = fake_client(completion("Answer [VAL-RESULT]"))
    result = AzureOpenAIChatProvider(SETTINGS, client=client).generate(request_)
    assert result.text == "Answer [VAL-RESULT]"
    assert result.model == "gpt-test"
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (900, 120)
    assert result.usage.source == "provider"
    call = client.chat.completions.calls[0]
    assert call["model"] == "chat-deployment"
    assert call["max_completion_tokens"] == 321


def test_length_finish_reason_marks_truncation(request_):
    client = fake_client(completion("partial", finish_reason="length"))
    assert AzureOpenAIChatProvider(SETTINGS, client=client).generate(request_).truncated


def test_missing_usage_is_tolerated(request_):
    client = fake_client(completion("ok", usage=False))
    assert AzureOpenAIChatProvider(SETTINGS, client=client).generate(request_).usage is None


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (_status_error(openai.RateLimitError, 429), AIErrorKind.RATE_LIMITED),
        (openai.APITimeoutError(request=_REQUEST), AIErrorKind.TIMEOUT),
        (openai.APIConnectionError(request=_REQUEST), AIErrorKind.UNAVAILABLE),
        (_status_error(openai.AuthenticationError, 401), AIErrorKind.AUTHENTICATION),
        (_status_error(openai.PermissionDeniedError, 403), AIErrorKind.AUTHENTICATION),
        (_status_error(openai.InternalServerError, 500), AIErrorKind.UNAVAILABLE),
        (
            _status_error(openai.BadRequestError, 400, {"code": "content_filter"}),
            AIErrorKind.CONTENT_FILTERED,
        ),
        (_status_error(openai.BadRequestError, 400), AIErrorKind.BAD_RESPONSE),
    ],
)
def test_sdk_errors_are_mapped(request_, error, kind):
    provider = AzureOpenAIChatProvider(SETTINGS, client=fake_client(error))
    with pytest.raises(AIProviderError) as info:
        provider.generate(request_)
    assert info.value.kind == kind


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (completion("x", finish_reason="content_filter"), AIErrorKind.CONTENT_FILTERED),
        (completion("   "), AIErrorKind.BAD_RESPONSE),
        (completion(None), AIErrorKind.BAD_RESPONSE),
    ],
)
def test_unusable_responses_are_errors(request_, response, kind):
    provider = AzureOpenAIChatProvider(SETTINGS, client=fake_client(response))
    with pytest.raises(AIProviderError) as info:
        provider.generate(request_)
    assert info.value.kind == kind


def test_missing_configuration_is_rejected():
    with pytest.raises(AIProviderError) as info:
        AzureOpenAIChatProvider(Settings(_env_file=None, ai_provider="azure_openai"))
    assert info.value.kind == AIErrorKind.CONFIGURATION


def test_real_client_uses_v1_endpoint_timeout_and_bounded_retries():
    settings = SETTINGS.model_copy(
        update={"azure_openai_api_key": SecretStr("test-key-not-real"), "ai_max_retries": 1}
    )
    client = AzureOpenAIChatProvider._build_client(settings)
    assert str(client.base_url) == "https://example.invalid/openai/v1/"
    assert client.max_retries == 1
    assert client.timeout == settings.ai_timeout_seconds
