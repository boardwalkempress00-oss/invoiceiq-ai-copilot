"""Chat model providers.

* `MockChatProvider` — deterministic, offline answers assembled from the same evidence the real
  model receives. Used for local development, automated tests and the free public demo mode.
* `AzureOpenAIChatProvider` — Azure OpenAI through the official OpenAI Python SDK, using the GA
  v1 endpoint (`<resource>/openai/v1/`). Authenticates with Microsoft Entra ID (managed identity
  in Azure, `az login` locally) unless an API key is configured.

Retries: the SDK retries rate limits (429), timeouts, connection errors and 5xx responses with
exponential backoff (0.5 s doubling up to 8 s, honouring `Retry-After`). `max_retries` and
`timeout` bound the total time.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol

from invoiceiq.ai.context import ChatContext, money
from invoiceiq.configuration import Settings
from invoiceiq.core.models import Finding, InvoiceStatus

if TYPE_CHECKING:
    from openai import OpenAI

AZURE_COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    # provider: reported by the paid model; estimated: pre-call estimate for a live model;
    # simulated: produced by the offline mock — not real model usage and never billed.
    source: Literal["provider", "estimated", "simulated"]

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class GenerationRequest:
    messages: list[dict[str, str]]
    context: ChatContext
    question: str
    max_output_tokens: int


@dataclass(frozen=True)
class GenerationResult:
    text: str
    model: str
    usage: TokenUsage | None
    truncated: bool = False


class AIErrorKind(StrEnum):
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    AUTHENTICATION = "authentication"
    CONTENT_FILTERED = "content_filtered"
    BAD_RESPONSE = "bad_response"
    CONFIGURATION = "configuration"


class AIProviderError(Exception):
    """A provider failure, safe to describe to users (no upstream details included)."""

    def __init__(self, kind: AIErrorKind, detail: str = "") -> None:
        super().__init__(f"{kind.value}: {detail}" if detail else kind.value)
        self.kind = kind


class ChatProvider(Protocol):
    name: str
    is_live: bool  # True if calls cost money / leave the process

    def generate(self, request: GenerationRequest) -> GenerationResult: ...


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 characters per token) for providers that report no usage."""
    return max(1, len(text) // 4)


# --------------------------------------------------------------------------------------------
# Mock provider
# --------------------------------------------------------------------------------------------

_SUPPLIER_FACING_PREFIXES = ("CALC-", "REQ-", "VAT-001")
_OUT_OF_SCOPE_TERMS = (
    "fraud", "tax advice", "legal advice", "lawsuit", "sue ", "invest", "other invoice",
    "approve it", "approve this", "authorize payment", "authorise payment",
)  # fmt: skip


def _cite_policy(context: ChatContext, section_id: str) -> str:
    return f" [{section_id}]" if section_id in context.allowed_ids else ""


class MockChatProvider:
    """Template-based answers grounded in the evidence. No network, no cost, deterministic."""

    name = "mock"
    is_live = False
    model = "mock-grounded-template-v1"

    def generate(self, request: GenerationRequest) -> GenerationResult:
        question = request.question.lower()
        context = request.context
        if any(term in question for term in _OUT_OF_SCOPE_TERMS):
            text = self._out_of_scope()
        elif "email" in question or "follow-up" in question or "follow up" in question:
            text = self._supplier_email(context)
        elif any(w in question for w in ("next", "should", "action", "do now", "recommend")):
            text = self._next_steps(context)
        elif any(w in question for w in ("flag", "why", "discrepanc", "wrong", "issue", "error")):
            text = self._findings(context)
        elif any(w in question for w in ("comply", "complian", "policy", "allowed", "rule")):
            text = self._compliance(context)
        elif any(w in question for w in ("explain", "summar", "what is", "overview", "invoice")):
            text = self._explain(context)
        else:
            text = (
                "The available evidence does not answer this question. I can explain this "
                "invoice, its validation findings, relevant purchasing policy sections, next "
                "steps, or draft a follow-up email to the supplier. [VAL-RESULT]"
            )
        prompt = "".join(m["content"] for m in request.messages)
        usage = TokenUsage(estimate_tokens(prompt), estimate_tokens(text), "simulated")
        return GenerationResult(text=text, model=self.model, usage=usage)

    # -- templates -------------------------------------------------------------------------------

    @staticmethod
    def _out_of_scope() -> str:
        return (
            "**Verified facts**\n- This assistant only explains the validation results and "
            "purchasing policy evidence for the selected fictional invoice. [VAL-RESULT]\n\n"
            "**Recommendations (AI-generated)**\n- Questions about fraud, tax or legal matters, "
            "or payment and approval decisions, must be handled by an authorised person. "
            "Automated results are decision support only."
        )

    @staticmethod
    def _facts_for(findings: list[Finding]) -> list[str]:
        return [
            f"- **{f.title}** ({f.severity.value}): {f.description} [{f.finding_id}] "
            f"[{f.policy_reference}]"
            for f in findings
        ]

    def _explain(self, context: ChatContext) -> str:
        inv, val = context.invoice, context.validation
        cur = inv.currency
        vendor = inv.vendor.name if inv.vendor and inv.vendor.name else "an unnamed vendor"
        facts = [
            f"- Invoice {inv.invoice_number or '(number missing)'} from {vendor}, issued "
            f"{inv.issue_date or '(date missing)'}, with {len(inv.line_items)} line item(s). "
            "[INV-HEADER] [INV-LINES]",
            f"- Stated total {money(inv.total, cur)}; total recalculated from the line items "
            f"{money(val.calculated.total, cur)}. [INV-TOTALS]",
            f"- Validation status: **{val.status.value}** with {len(val.findings)} finding(s). "
            "[VAL-RESULT]",
        ]
        facts += self._facts_for(val.findings)
        recommendation = (
            "- No action beyond the normal payment process appears necessary."
            if val.status == InvoiceStatus.PASSED
            else "- Resolve the findings above before releasing the invoice for payment."
        )
        return (
            "**Verified facts**\n" + "\n".join(facts)
            + "\n\n**Recommendations (AI-generated)**\n" + recommendation
        )  # fmt: skip

    def _findings(self, context: ChatContext) -> str:
        val = context.validation
        if not val.findings:
            return (
                "**Verified facts**\n- This invoice was not flagged: all validation checks "
                "passed and there are no findings. [VAL-RESULT]\n\n"
                "**Recommendations (AI-generated)**\n- No discrepancies need to be resolved."
            )
        return (
            "**Verified facts**\n"
            + f"- Validation status: **{val.status.value}**. [VAL-RESULT]\n"
            + "\n".join(self._facts_for(val.findings))
            + "\n\n**Recommendations (AI-generated)**\n"
            + "\n".join(dict.fromkeys(f"- {f.recommended_action}" for f in val.findings))
        )

    def _compliance(self, context: ChatContext) -> str:
        val = context.validation
        if not val.findings:
            body = (
                "- All automated policy checks passed (mandatory fields, arithmetic, VAT rate "
                "and approval threshold). [VAL-RESULT]"
            )
            if val.required_approval_role is None:
                body += (
                    "\n- The net amount does not exceed the manager approval threshold, so no "
                    "approval is required." + _cite_policy(context, "PP-3.1") + " [INV-APPROVAL]"
                )
            recommendation = (
                "- The invoice appears compliant with the checked rules; a person should still "
                "confirm the goods or services were received."
            )
        else:
            body = (
                f"- The invoice does **not** fully comply: status **{val.status.value}** with "
                f"{len(val.findings)} finding(s). [VAL-RESULT]\n"
                + "\n".join(self._facts_for(val.findings))
            )
            recommendation = "- Keep the invoice on payment hold until the findings are resolved."
        return (
            "**Verified facts**\n" + body
            + "\n\n**Recommendations (AI-generated)**\n" + recommendation
            + _cite_policy(context, "PP-7.1")
        )  # fmt: skip

    def _next_steps(self, context: ChatContext) -> str:
        val = context.validation
        if not val.findings:
            return (
                "**Verified facts**\n- No validation findings. [VAL-RESULT]\n\n"
                "**Recommendations (AI-generated)**\n- Confirm receipt of the goods or services "
                "and release the invoice through the normal payment process."
                + _cite_policy(context, "PP-6.1")
            )
        steps = [f"{i}. {f.recommended_action} [{f.finding_id}]" for i, f in
                 enumerate(val.findings, start=1)]  # fmt: skip
        steps.append(
            f"{len(steps) + 1}. Escalate to the finance controller if unresolved after 10 "
            "business days." + _cite_policy(context, "PP-5.3")
        )
        return (
            "**Verified facts**\n"
            + f"- Status **{val.status.value}** with {len(val.findings)} finding(s). "
            + "[VAL-RESULT]\n\n**Recommendations (AI-generated)**\n"
            + "\n".join(steps)
        )

    def _supplier_email(self, context: ChatContext) -> str:
        inv, val = context.invoice, context.validation
        issues = [f for f in val.findings if f.rule_id.startswith(_SUPPLIER_FACING_PREFIXES)]
        if not issues:
            return (
                "**Verified facts**\n- There are no supplier-facing discrepancies on this "
                "invoice. [VAL-RESULT]\n\n**Recommendations (AI-generated)**\n- No email to the "
                "supplier is needed. Internal approval matters are not shared with suppliers."
                + _cite_policy(context, "PP-5.2")
            )
        vendor = inv.vendor.name if inv.vendor and inv.vendor.name else "[Supplier name]"
        bullet_lines = [
            f"- {f.title}: invoice states {money(Decimal(f.actual_value or '0'), inv.currency)}"
            f", our calculation gives {money(Decimal(f.expected_value or '0'), inv.currency)}"
            if f.rule_id.startswith("CALC-") else f"- {f.description}"
            for f in issues
        ]  # fmt: skip
        email = (
            f"Subject: Invoice {inv.invoice_number} — request for corrected invoice\n\n"
            f"Dear {vendor} accounts team,\n\n"
            f"Thank you for invoice {inv.invoice_number} dated {inv.issue_date}. During our "
            "routine check we found the following:\n\n" + "\n".join(bullet_lines) + "\n\n"
            "Could you please send a corrected invoice or a credit note? We will process the "
            "invoice as soon as we receive it.\n\nKind regards,\n[Your name]\n"
            "Accounts Payable, Aurora Nordic Trading ApS"
        )
        return (
            "**Verified facts**\n"
            + "\n".join(self._facts_for(issues))
            + "\n\n**Recommendations (AI-generated)**\n\n_Draft email — must be reviewed by a "
            + "person before sending._" + _cite_policy(context, "PP-7.1") + "\n\n"
            + "\n".join(f"> {line}  " if line else ">" for line in email.splitlines())
        )  # fmt: skip


# --------------------------------------------------------------------------------------------
# Azure OpenAI provider
# --------------------------------------------------------------------------------------------


class AzureOpenAIChatProvider:
    name = "azure_openai"
    is_live = True

    def __init__(self, settings: Settings, client: OpenAI | None = None) -> None:
        if not settings.azure_openai_endpoint or not settings.azure_openai_deployment:
            raise AIProviderError(
                AIErrorKind.CONFIGURATION,
                "AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT must be set",
            )
        self._deployment = settings.azure_openai_deployment
        self._client = client or self._build_client(settings)

    @staticmethod
    def _build_client(settings: Settings) -> OpenAI:
        from openai import OpenAI

        api_key: Any
        if settings.azure_openai_api_key is not None:
            api_key = settings.azure_openai_api_key.get_secret_value()
        else:
            # Keyless: managed identity in Azure, developer credentials (az login) locally.
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            api_key = get_bearer_token_provider(DefaultAzureCredential(), AZURE_COGNITIVE_SCOPE)
        endpoint = (settings.azure_openai_endpoint or "").rstrip("/")
        return OpenAI(
            base_url=f"{endpoint}/openai/v1/",
            api_key=api_key,
            timeout=settings.ai_timeout_seconds,
            max_retries=settings.ai_max_retries,
        )

    def generate(self, request: GenerationRequest) -> GenerationResult:
        import openai

        try:
            response = self._client.chat.completions.create(
                model=self._deployment,
                messages=request.messages,  # type: ignore[arg-type]
                max_completion_tokens=request.max_output_tokens,
            )
        except openai.RateLimitError as exc:
            raise AIProviderError(AIErrorKind.RATE_LIMITED) from exc
        except openai.APITimeoutError as exc:
            raise AIProviderError(AIErrorKind.TIMEOUT) from exc
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            raise AIProviderError(AIErrorKind.AUTHENTICATION) from exc
        except openai.BadRequestError as exc:
            if "content_filter" in str(getattr(exc, "code", "") or "") or "content_filter" in str(
                getattr(exc, "body", "") or ""
            ):
                raise AIProviderError(AIErrorKind.CONTENT_FILTERED) from exc
            raise AIProviderError(AIErrorKind.BAD_RESPONSE, "request rejected") from exc
        except (openai.APIConnectionError, openai.APIStatusError) as exc:
            raise AIProviderError(AIErrorKind.UNAVAILABLE) from exc
        except openai.OpenAIError as exc:
            raise AIProviderError(AIErrorKind.UNAVAILABLE) from exc

        if not response.choices:
            raise AIProviderError(AIErrorKind.BAD_RESPONSE, "no choices returned")
        choice = response.choices[0]
        if choice.finish_reason == "content_filter":
            raise AIProviderError(AIErrorKind.CONTENT_FILTERED)
        text = (choice.message.content or "").strip()
        if not text:
            raise AIProviderError(AIErrorKind.BAD_RESPONSE, "empty answer")
        usage = (
            TokenUsage(response.usage.prompt_tokens, response.usage.completion_tokens, "provider")
            if response.usage
            else None
        )
        return GenerationResult(
            text=text,
            model=response.model or self._deployment,
            usage=usage,
            truncated=choice.finish_reason == "length",
        )


class DisabledChatProvider:
    name = "disabled"
    is_live = False

    def generate(self, request: GenerationRequest) -> GenerationResult:
        raise AIProviderError(AIErrorKind.CONFIGURATION, "AI provider disabled")


def create_provider(settings: Settings) -> ChatProvider:
    from invoiceiq.configuration import AIProviderName

    match settings.ai_provider:
        case AIProviderName.MOCK:
            return MockChatProvider()
        case AIProviderName.AZURE_OPENAI:
            return AzureOpenAIChatProvider(settings)
        case AIProviderName.DISABLED:
            return DisabledChatProvider()
