"""Request and response models for the HTTP API."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

INVOICE_ID_PATTERN = r"^[a-z0-9-]{1,40}$"
SESSION_ID_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Requests ------------------------------------------------------------------------------------


class ChatHistoryMessage(_Schema):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=5_000)


class ChatRequest(_Schema):
    invoice_id: str = Field(pattern=INVOICE_ID_PATTERN)
    # Hard upper bound here; the configurable (smaller) limit is enforced in the route.
    question: str = Field(min_length=1, max_length=2_000)
    session_id: str = Field(
        pattern=SESSION_ID_PATTERN,
        description="Opaque per-browser-session identifier used for the per-session allowance.",
    )
    history: list[ChatHistoryMessage] = Field(default_factory=list, max_length=20)


# --- Responses -----------------------------------------------------------------------------------


class ErrorBody(_Schema):
    code: str
    message: str
    correlation_id: str
    details: list[dict[str, str]] | None = None


class ErrorResponse(_Schema):
    error: ErrorBody


class AIStatus(_Schema):
    provider: str
    live: bool = Field(description="True when answers come from a paid, external model.")
    available: bool
    block_reason: str | None
    daily_requests_remaining: int | None = Field(
        description="None when the usage store cannot be read (AI is then unavailable)."
    )


class HealthResponse(_Schema):
    status: Literal["ok"]
    version: str
    environment: str
    ai: AIStatus


class PolicySectionOut(_Schema):
    section_id: str
    number: str
    title: str
    chapter: str
    text: str


class VerifiedFactOut(_Schema):
    label: str
    value: str
    source_id: str


class CitationOut(_Schema):
    evidence_id: str
    kind: str
    title: str


class UsageOut(_Schema):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    source: Literal["provider", "estimated"]


class TimingsOut(_Schema):
    retrieval_ms: float
    ai_ms: float | None
    total_ms: float


class LimitsOut(_Schema):
    session_remaining: int | None
    global_remaining: int | None


class ChatResponse(_Schema):
    mode: Literal["ai", "read_only"]
    answer: str = Field(description="AI-generated (mode=ai) or deterministic summary (read_only).")
    notice: str | None
    read_only_reason: str | None
    verified_facts: list[VerifiedFactOut] = Field(
        description="Produced by the validation engine, never by the AI model."
    )
    citations: list[CitationOut]
    policy_evidence_found: bool
    grounded: bool
    removed_citations: list[str]
    truncated: bool
    provider: str
    model: str | None
    usage: UsageOut | None
    estimated_cost: Decimal | None
    cost_currency: str | None
    timings: TimingsOut
    limits: LimitsOut
    correlation_id: str
