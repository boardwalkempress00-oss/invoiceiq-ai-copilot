"""HTTP routes. Each handler validates input and delegates to a service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request

from invoiceiq import __version__
from invoiceiq.ai.ai_service import ChatOutcome, ChatService, ReadOnlyReason
from invoiceiq.ai.prompts import HistoryMessage, sanitize_user_text
from invoiceiq.api.schemas import (
    INVOICE_ID_PATTERN,
    AIStatus,
    ChatRequest,
    ChatResponse,
    CitationOut,
    ErrorResponse,
    HealthResponse,
    LimitsOut,
    PolicySectionOut,
    TimingsOut,
    UsageOut,
    VerifiedFactOut,
)
from invoiceiq.configuration import Settings
from invoiceiq.core.invoice_service import InvoiceService
from invoiceiq.core.models import Invoice, InvoiceSummary, ValidationResult
from invoiceiq.logging_config import correlation_id_var
from invoiceiq.processing.demo import DemoService
from invoiceiq.processing.submissions import SubmissionService


@dataclass(frozen=True)
class AppContainer:
    settings: Settings
    invoice_service: InvoiceService
    chat_service: ChatService
    # None unless ASYNC_PROCESSING_ENABLED is set (see submission_routes.py).
    submission_service: SubmissionService | None = None
    # None unless DEMO_MODE_ENABLED is set (see demo_routes.py). In-memory only.
    demo_service: DemoService | None = None


class InputTooLongError(ValueError):
    pass


def get_container(request: Request) -> AppContainer:
    return request.app.state.container  # type: ignore[no-any-return]


Container = Annotated[AppContainer, Depends(get_container)]
InvoiceId = Annotated[str, Path(pattern=INVOICE_ID_PATTERN)]

router = APIRouter()
_NOT_FOUND = {404: {"model": ErrorResponse}}


@router.get("/health/live", tags=["system"])
def liveness() -> dict[str, str]:
    """Process liveness only. Touches no dependencies, so a storage or AI outage cannot make
    the platform restart a working container."""
    return {"status": "ok"}


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health(container: Container) -> HealthResponse:
    chat = container.chat_service
    block = chat.live_ai_block_reason()
    remaining = chat.usage_remaining()
    daily_remaining = remaining[1] if remaining is not None else None
    if block is None and remaining is None:
        block = ReadOnlyReason.LIMITER_UNAVAILABLE
    return HealthResponse(
        status="ok",
        version=__version__,
        environment=container.settings.app_env.value,
        ai=AIStatus(
            provider=chat.provider.name,
            live=chat.provider.is_live,
            available=block is None and bool(daily_remaining),
            block_reason=block.value if block else None,
            daily_requests_remaining=daily_remaining,
        ),
        async_processing=container.submission_service is not None,
        demo_mode=container.demo_service is not None,
    )


@router.get("/policy/sections", response_model=list[PolicySectionOut], tags=["policy"])
def list_policy_sections(container: Container) -> list[PolicySectionOut]:
    """The fictional purchasing policy, split into citable sections."""
    return [
        PolicySectionOut(
            section_id=s.section_id, number=s.number, title=s.title, chapter=s.chapter, text=s.text
        )
        for s in container.chat_service.retriever.sections
    ]


@router.get("/invoices", response_model=list[InvoiceSummary], tags=["invoices"])
def list_invoices(container: Container) -> list[InvoiceSummary]:
    return container.invoice_service.list_summaries()


@router.get(
    "/invoices/{invoice_id}", response_model=Invoice, responses=_NOT_FOUND, tags=["invoices"]
)
def get_invoice(invoice_id: InvoiceId, container: Container) -> Invoice:
    return container.invoice_service.get_invoice(invoice_id)


@router.post(
    "/invoices/{invoice_id}/validate",
    response_model=ValidationResult,
    responses=_NOT_FOUND,
    tags=["invoices"],
)
def validate_invoice(invoice_id: InvoiceId, container: Container) -> ValidationResult:
    return container.invoice_service.validate(invoice_id)


@router.post(
    "/chat",
    response_model=ChatResponse,
    responses={**_NOT_FOUND, 422: {"model": ErrorResponse}},
    tags=["chat"],
)
def chat(body: ChatRequest, container: Container) -> ChatResponse:
    settings = container.settings
    question = sanitize_user_text(body.question)
    if not question:
        raise InputTooLongError("The question is empty.")
    if len(question) > settings.chat_max_question_chars:
        raise InputTooLongError(
            f"Questions are limited to {settings.chat_max_question_chars} characters."
        )
    history = [
        HistoryMessage(m.role, m.content[: settings.chat_max_history_message_chars])
        for m in body.history
    ]
    outcome = container.chat_service.answer(
        invoice_id=body.invoice_id,
        question=question,
        session_id=body.session_id,
        history=history,
    )
    return _to_response(outcome)


def _to_response(outcome: ChatOutcome) -> ChatResponse:
    usage = outcome.usage
    return ChatResponse(
        mode=outcome.mode.value,
        answer=outcome.answer,
        notice=outcome.notice,
        read_only_reason=outcome.read_only_reason.value if outcome.read_only_reason else None,
        verified_facts=[
            VerifiedFactOut(label=f.label, value=f.value, source_id=f.source_id)
            for f in outcome.verified_facts
        ],
        citations=[
            CitationOut(evidence_id=c.evidence_id, kind=c.kind.value, title=c.title)
            for c in outcome.citations
        ],
        policy_evidence_found=outcome.policy_evidence_found,
        grounded=outcome.grounded,
        removed_citations=outcome.removed_citations,
        truncated=outcome.truncated,
        provider=outcome.provider,
        model=outcome.model,
        usage=UsageOut(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            source=usage.source,
        )
        if usage
        else None,
        estimated_cost=outcome.estimated_cost,
        cost_currency=outcome.cost_currency,
        cost_is_simulated=outcome.cost_is_simulated,
        timings=TimingsOut(
            retrieval_ms=outcome.timings.retrieval_ms,
            ai_ms=outcome.timings.ai_ms,
            total_ms=outcome.timings.total_ms,
        ),
        limits=LimitsOut(
            session_remaining=outcome.session_remaining,
            global_remaining=outcome.global_remaining,
        ),
        correlation_id=correlation_id_var.get(),
    )
