"""Chat orchestration.

For each question:

1. Load the invoice and run deterministic validation (Python, Decimal).
2. Retrieve policy evidence: sections cited by the findings, plus BM25 matches for the question.
3. Enforce cost controls (provider enabled, persistent limiter in production, session/global caps).
4. Ask the model to explain the evidence, then remove any citation that is not in the evidence.
5. Return the AI answer *separately* from verified facts, which are built here without the model.

If AI is disabled, exhausted or failing, a read-only answer built from the validation results is
returned instead, so the app stays useful.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum

from invoiceiq.ai.context import ChatContext, EvidenceKind, build_context, money
from invoiceiq.ai.prompts import HistoryMessage, build_messages
from invoiceiq.ai.providers import (
    AIErrorKind,
    AIProviderError,
    ChatProvider,
    DisabledChatProvider,
    GenerationRequest,
    TokenUsage,
)
from invoiceiq.configuration import Environment, Settings
from invoiceiq.core.invoice_service import InvoiceService
from invoiceiq.core.models import Invoice, ValidationResult
from invoiceiq.core.policy_retriever import PolicyRetriever, PolicySection
from invoiceiq.core.usage_limiter import (
    LimitReason,
    Reservation,
    SettleOutcome,
    UsageEstimate,
    UsageLimiter,
)

logger = logging.getLogger(__name__)

_BRACKET_RE = re.compile(r"\[([^\[\]\n]{2,120})\]")
_REMOVED = "\u0000"  # placeholder for a removed citation, cleaned up afterwards
_EVIDENCE_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Z0-9][A-Z0-9._-]*$")


class ChatMode(StrEnum):
    AI = "ai"
    READ_ONLY = "read_only"


class ReadOnlyReason(StrEnum):
    AI_DISABLED = "ai_disabled"
    PERSISTENT_LIMITER_REQUIRED = "persistent_limiter_required"
    SESSION_LIMIT_REACHED = "session_limit_reached"
    DAILY_LIMIT_REACHED = "daily_limit_reached"
    AI_UNAVAILABLE = "ai_unavailable"
    LIMITER_UNAVAILABLE = "limiter_unavailable"
    DAILY_BUDGET_REACHED = "daily_budget_reached"
    PRICING_NOT_CONFIGURED = "pricing_not_configured"


_READ_ONLY_NOTICES: dict[ReadOnlyReason, str] = {
    ReadOnlyReason.AI_DISABLED: "AI answers are switched off for this deployment.",
    ReadOnlyReason.PERSISTENT_LIMITER_REQUIRED: (
        "Live AI is blocked until a persistent usage limiter is configured."
    ),
    ReadOnlyReason.DAILY_BUDGET_REACHED: (
        "The demo's daily AI budget has been used up. It resets at midnight UTC."
    ),
    ReadOnlyReason.PRICING_NOT_CONFIGURED: (
        "Live AI is blocked until model prices are configured, so the daily budget can be enforced."
    ),
    ReadOnlyReason.LIMITER_UNAVAILABLE: (
        "AI answers are paused because the usage limiter is unavailable."
    ),
    ReadOnlyReason.SESSION_LIMIT_REACHED: (
        "You have used all AI questions for this session today."
    ),
    ReadOnlyReason.DAILY_LIMIT_REACHED: (
        "The demo's daily AI allowance has been used up. It resets at midnight UTC."
    ),
}

_ERROR_NOTICES: dict[AIErrorKind, str] = {
    AIErrorKind.RATE_LIMITED: "The AI service is busy right now. Please try again in a minute.",
    AIErrorKind.TIMEOUT: "The AI service took too long to respond. Please try again.",
    AIErrorKind.CONTENT_FILTERED: (
        "The AI service declined this request under its content safety policy."
    ),
}
_DEFAULT_ERROR_NOTICE = "The AI service is temporarily unavailable."

_LIMIT_REASONS: dict[LimitReason, ReadOnlyReason] = {
    LimitReason.SESSION_LIMIT_REACHED: ReadOnlyReason.SESSION_LIMIT_REACHED,
    LimitReason.DAILY_LIMIT_REACHED: ReadOnlyReason.DAILY_LIMIT_REACHED,
    LimitReason.DAILY_BUDGET_REACHED: ReadOnlyReason.DAILY_BUDGET_REACHED,
}

# Conservative token estimate: ~3 characters per token (English averages ~4), plus a small
# per-message overhead. The output side is reserved at the full max_output_tokens.
CHARS_PER_TOKEN_ESTIMATE = 3
MESSAGE_OVERHEAD_TOKENS = 10


def _settle_outcome_for(kind: AIErrorKind) -> SettleOutcome:
    """Only failures that certainly consumed no model tokens release the reserved cost.

    Configuration errors happen before any call; authentication failures (401/403) are not
    retried, so no attempt reached the model. Everything else — timeouts, 5xx, rate limits after
    retries, content filtering, bad responses — may follow an attempt the service processed, so
    it is charged at the reserved estimate.
    """
    if kind in (AIErrorKind.CONFIGURATION, AIErrorKind.AUTHENTICATION):
        return SettleOutcome.NOT_BILLED
    return SettleOutcome.AMBIGUOUS


def cost_micro(usage: TokenUsage, settings: Settings) -> int:
    """Cost in millionths of AI_PRICE_CURRENCY. Prices are per 1M tokens, so tokens × price."""
    value = (
        Decimal(usage.prompt_tokens) * settings.ai_price_input_per_1m
        + Decimal(usage.completion_tokens) * settings.ai_price_output_per_1m
    )
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def estimate_usage(
    messages: Sequence[dict[str, str]], max_output_tokens: int, settings: Settings
) -> UsageEstimate:
    """Upper-bound estimate reserved before the AI call."""
    chars = sum(len(m["content"]) for m in messages)
    prompt_tokens = -(-chars // CHARS_PER_TOKEN_ESTIMATE) + MESSAGE_OVERHEAD_TOKENS * len(messages)
    usage = TokenUsage(prompt_tokens, max_output_tokens, "estimated")
    return UsageEstimate(tokens=usage.total_tokens, cost_micro=cost_micro(usage, settings))


@dataclass(frozen=True)
class VerifiedFact:
    label: str
    value: str
    source_id: str


@dataclass(frozen=True)
class Citation:
    evidence_id: str
    kind: EvidenceKind
    title: str


@dataclass(frozen=True)
class ChatTimings:
    retrieval_ms: float
    ai_ms: float | None
    total_ms: float


@dataclass(frozen=True)
class ChatOutcome:
    mode: ChatMode
    answer: str
    verified_facts: list[VerifiedFact]
    citations: list[Citation]
    policy_evidence_found: bool
    timings: ChatTimings
    provider: str
    model: str | None = None
    usage: TokenUsage | None = None
    estimated_cost: Decimal | None = None
    cost_currency: str | None = None
    read_only_reason: ReadOnlyReason | None = None
    notice: str | None = None
    removed_citations: list[str] = field(default_factory=list)
    grounded: bool = True
    truncated: bool = False
    session_remaining: int | None = None
    global_remaining: int | None = None


def enforce_citations(text: str, allowed_ids: set[str]) -> tuple[str, list[str], list[str]]:
    """Remove citations that do not refer to supplied evidence.

    Returns (cleaned text, valid IDs in order of first use, removed IDs). Bracketed text that does
    not look like an evidence ID (e.g. "[Your name]" in an email draft) is left unchanged.
    """
    used: list[str] = []
    removed: list[str] = []

    def replace(match: re.Match[str]) -> str:
        parts = [p.strip() for p in re.split(r"[,;]", match.group(1))]
        if not all(_EVIDENCE_ID_RE.match(p) for p in parts):
            return match.group(0)
        valid = [p for p in parts if p in allowed_ids]
        removed.extend(p for p in parts if p not in allowed_ids)
        used.extend(p for p in valid if p not in used)
        return f"[{', '.join(valid)}]" if valid else _REMOVED

    cleaned = _BRACKET_RE.sub(replace, text)
    # Tidy whitespace left where a citation was removed.
    cleaned = re.sub(rf"[ \t]*{_REMOVED}([ \t]*)(?=[.,;:]|$)", "", cleaned, flags=re.M)
    cleaned = re.sub(rf"[ \t]*{_REMOVED}[ \t]*", " ", cleaned)
    return cleaned, used, list(dict.fromkeys(removed))


def estimate_cost(usage: TokenUsage | None, settings: Settings) -> Decimal | None:
    input_price, output_price = settings.ai_price_input_per_1m, settings.ai_price_output_per_1m
    if usage is None or (input_price == 0 and output_price == 0):
        return None
    cost = (
        Decimal(usage.prompt_tokens) * input_price + Decimal(usage.completion_tokens) * output_price
    ) / Decimal(1_000_000)
    return cost.quantize(Decimal("0.000001"))


def build_verified_facts(invoice: Invoice, validation: ValidationResult) -> list[VerifiedFact]:
    cur = invoice.currency
    calc = validation.calculated
    facts = [
        VerifiedFact("Validation status", validation.status.value, "VAL-RESULT"),
        VerifiedFact(
            "Total",
            f"stated {money(invoice.total, cur)}, calculated {money(calc.total, cur)}",
            "INV-TOTALS",
        ),
        VerifiedFact(
            "VAT",
            f"stated {money(invoice.vat_amount, cur)}, calculated {money(calc.vat_amount, cur)}",
            "INV-TOTALS",
        ),
        VerifiedFact(
            "Required approval", validation.required_approval_role or "none", "INV-APPROVAL"
        ),
    ]
    facts += [
        VerifiedFact(f"{f.title} ({f.severity.value})", f.description, f.finding_id)
        for f in validation.findings
    ]
    return facts


class ChatService:
    def __init__(
        self,
        *,
        settings: Settings,
        invoice_service: InvoiceService,
        retriever: PolicyRetriever,
        provider: ChatProvider,
        limiter: UsageLimiter,
    ) -> None:
        self._settings = settings
        self._invoices = invoice_service
        self._retriever = retriever
        self._provider = provider
        self._limiter = limiter

    @property
    def provider(self) -> ChatProvider:
        return self._provider

    def live_ai_block_reason(self) -> ReadOnlyReason | None:
        """Configuration-level reason why AI answers cannot be produced, if any."""
        if isinstance(self._provider, DisabledChatProvider):
            return ReadOnlyReason.AI_DISABLED
        if (
            self._provider.is_live
            and self._settings.app_env == Environment.PRODUCTION
            and not self._limiter.store.shared_durable
        ):
            # In-memory or local counters reset whenever the container restarts or scales, so
            # they cannot cap spend in a public deployment.
            return ReadOnlyReason.PERSISTENT_LIMITER_REQUIRED
        if (
            self._provider.is_live
            and self._settings.app_env == Environment.PRODUCTION
            and self._settings.ai_price_input_per_1m == 0
            and self._settings.ai_price_output_per_1m == 0
        ):
            # Without prices every cost estimate is zero, so the daily budget could not work.
            return ReadOnlyReason.PRICING_NOT_CONFIGURED
        return None

    def usage_remaining(self, session_id: str | None = None) -> tuple[int | None, int] | None:
        """Remaining (session, global) allowance, or None if the usage store is unavailable."""
        try:
            return self._limiter.remaining(session_id)
        except Exception:
            logger.exception("usage_limiter.unavailable")
            return None

    @property
    def retriever(self) -> PolicyRetriever:
        return self._retriever

    def retrieve_policy(self, question: str, validation: ValidationResult) -> list[PolicySection]:
        """Sections referenced by findings first (always relevant), then BM25 matches."""
        sections: dict[str, PolicySection] = {}
        for finding in validation.findings:
            section = self._retriever.get_section(finding.policy_reference)
            if section:
                sections.setdefault(section.section_id, section)
        for passage in self._retriever.search(question, top_k=self._settings.retrieval_top_k):
            sections.setdefault(passage.section.section_id, passage.section)
        return list(sections.values())

    def answer(
        self,
        *,
        invoice_id: str,
        question: str,
        session_id: str,
        history: Sequence[HistoryMessage] = (),
    ) -> ChatOutcome:
        started = time.perf_counter()
        invoice = self._invoices.get_invoice(invoice_id)
        validation = self._invoices.process(invoice)
        sections = self.retrieve_policy(question, validation)
        context = build_context(
            invoice, validation, sections, max_chars=self._settings.ai_max_context_chars
        )
        retrieval_ms = (time.perf_counter() - started) * 1000
        facts = build_verified_facts(invoice, validation)

        def read_only(
            reason: ReadOnlyReason,
            notice: str,
            session_remaining: int | None = None,
            global_remaining: int | None = None,
        ) -> ChatOutcome:
            logger.info("chat.read_only", extra={"reason": reason.value})
            return self._read_only(
                context, facts, reason, notice, retrieval_ms, started,
                session_remaining, global_remaining,
            )  # fmt: skip

        if block := self.live_ai_block_reason():
            return read_only(block, _READ_ONLY_NOTICES[block])

        history = list(history)[-self._settings.chat_max_history_messages :]
        request = GenerationRequest(
            messages=build_messages(context, question, history),
            context=context,
            question=question,
            max_output_tokens=self._settings.ai_max_output_tokens,
        )
        estimate = estimate_usage(request.messages, request.max_output_tokens, self._settings)

        try:
            decision = self._limiter.reserve(session_id, estimate)
        except Exception:
            # Fail closed: if usage cannot be recorded, no AI call is made.
            logger.exception("usage_limiter.unavailable")
            return read_only(
                ReadOnlyReason.LIMITER_UNAVAILABLE,
                _READ_ONLY_NOTICES[ReadOnlyReason.LIMITER_UNAVAILABLE],
            )
        if not decision.allowed or decision.reservation is None:
            reason = _LIMIT_REASONS.get(decision.reason, ReadOnlyReason.DAILY_LIMIT_REACHED)
            return read_only(
                reason,
                _READ_ONLY_NOTICES[reason],
                session_remaining=decision.session_remaining,
                global_remaining=decision.global_remaining,
            )
        reservation = decision.reservation

        ai_started = time.perf_counter()
        try:
            result = self._provider.generate(request)
        except AIProviderError as exc:
            logger.warning(
                "chat.ai_error",
                extra={"error_kind": exc.kind.value, "provider": self._provider.name},
            )
            self._settle(reservation, _settle_outcome_for(exc.kind))
            return read_only(
                ReadOnlyReason.AI_UNAVAILABLE,
                _ERROR_NOTICES.get(exc.kind, _DEFAULT_ERROR_NOTICE),
                session_remaining=decision.session_remaining,
                global_remaining=decision.global_remaining,
            )
        except BaseException:
            self._settle(reservation, SettleOutcome.AMBIGUOUS)
            raise
        ai_ms = (time.perf_counter() - ai_started) * 1000
        self._settle(
            reservation,
            SettleOutcome.SUCCEEDED,
            prompt_tokens=result.usage.prompt_tokens if result.usage else None,
            completion_tokens=result.usage.completion_tokens if result.usage else None,
            actual_cost_micro=cost_micro(result.usage, self._settings) if result.usage else None,
        )

        text, used_ids, removed = enforce_citations(result.text, context.allowed_ids)
        grounded = bool(used_ids)
        notice = None
        if removed:
            notice = "Some citations did not match the supplied evidence and were removed."
        if not grounded:
            notice = "This answer contains no verifiable citations. Treat it with caution."
        if result.truncated:
            text += "\n\n_(Answer shortened to stay within the response length limit.)_"

        cost = estimate_cost(result.usage, self._settings)
        logger.info(
            "chat.completed",
            extra={
                "provider": self._provider.name,
                "question_chars": len(question),
                "prompt_tokens": result.usage.prompt_tokens if result.usage else None,
                "completion_tokens": result.usage.completion_tokens if result.usage else None,
                "ai_ms": round(ai_ms, 1),
                "citations": len(used_ids),
                "removed_citations": len(removed),
            },
        )
        return ChatOutcome(
            mode=ChatMode.AI,
            answer=text,
            verified_facts=facts,
            citations=[self._citation(context, eid) for eid in used_ids],
            policy_evidence_found=bool(context.policy_sections),
            timings=ChatTimings(
                retrieval_ms=round(retrieval_ms, 2),
                ai_ms=round(ai_ms, 2),
                total_ms=round((time.perf_counter() - started) * 1000, 2),
            ),
            provider=self._provider.name,
            model=result.model,
            usage=result.usage,
            estimated_cost=cost,
            cost_currency=self._settings.ai_price_currency if cost is not None else None,
            notice=notice,
            removed_citations=removed,
            grounded=grounded,
            truncated=result.truncated,
            session_remaining=decision.session_remaining,
            global_remaining=decision.global_remaining,
        )

    def _settle(
        self, reservation: Reservation, outcome: SettleOutcome, **actual: int | None
    ) -> None:
        try:
            self._limiter.settle(reservation, outcome, **actual)
        except Exception:
            # The reservation stays counted at its estimate: over-counting is the safe direction.
            logger.exception("usage_limiter.settle_failed")

    @staticmethod
    def _citation(context: ChatContext, evidence_id: str) -> Citation:
        item = context.get(evidence_id)
        assert item is not None
        return Citation(evidence_id, item.kind, item.title)

    def _read_only(
        self,
        context: ChatContext,
        facts: list[VerifiedFact],
        reason: ReadOnlyReason,
        notice: str,
        retrieval_ms: float,
        started: float,
        session_remaining: int | None = None,
        global_remaining: int | None = None,
    ) -> ChatOutcome:
        lines = [f"- **{fact.label}:** {fact.value} [{fact.source_id}]" for fact in facts]
        answer = (
            f"{notice} Showing the verified validation results instead "
            "(no AI-generated content):\n\n" + "\n".join(lines)
        )
        cited = list(dict.fromkeys(fact.source_id for fact in facts))
        return ChatOutcome(
            mode=ChatMode.READ_ONLY,
            answer=answer,
            verified_facts=facts,
            citations=[self._citation(context, eid) for eid in cited],
            policy_evidence_found=bool(context.policy_sections),
            timings=ChatTimings(
                retrieval_ms=round(retrieval_ms, 2),
                ai_ms=None,
                total_ms=round((time.perf_counter() - started) * 1000, 2),
            ),
            provider=self._provider.name,
            read_only_reason=reason,
            notice=notice,
            session_remaining=session_remaining,
            global_remaining=global_remaining,
        )
