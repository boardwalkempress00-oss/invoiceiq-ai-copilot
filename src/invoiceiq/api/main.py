"""FastAPI application factory.

Run locally:  uv run uvicorn invoiceiq.api.main:app_factory --factory --reload
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from invoiceiq import __version__
from invoiceiq.ai.ai_service import ChatService
from invoiceiq.ai.providers import ChatProvider, create_provider
from invoiceiq.api.middleware import correlation_and_timing, error_response
from invoiceiq.api.routes import AppContainer, InputTooLongError, router
from invoiceiq.configuration import Settings, UsageStoreName, get_settings
from invoiceiq.core.invoice_service import (
    InvoiceNotFoundError,
    InvoiceService,
    JsonInvoiceRepository,
)
from invoiceiq.core.policy_retriever import BM25PolicyRetriever
from invoiceiq.core.usage_limiter import (
    InMemoryUsageStore,
    SqliteUsageStore,
    UnavailableUsageStore,
    UsageLimiter,
    UsageStore,
)
from invoiceiq.logging_config import configure_logging

logger = logging.getLogger(__name__)


def create_usage_store(settings: Settings) -> UsageStore:
    if settings.usage_store == UsageStoreName.AZURE_TABLE:
        from invoiceiq.storage.azure_table_usage_store import AzureTableUsageStore

        try:
            store = AzureTableUsageStore.from_settings(settings)
            if settings.azure_table_connection_string is not None:
                store.ensure_table()  # local Azurite convenience; Terraform creates it in Azure
            return store
        except Exception as exc:
            # Fail closed: the app still serves validation results, but AI stays unavailable.
            logger.error("usage_store.unavailable", extra={"error": type(exc).__name__})
            return UnavailableUsageStore(f"usage store unavailable: {type(exc).__name__}")
    if settings.usage_store == UsageStoreName.SQLITE:
        return SqliteUsageStore(settings.usage_sqlite_path)
    return InMemoryUsageStore()


def build_container(
    settings: Settings,
    *,
    provider: ChatProvider | None = None,
    usage_store: UsageStore | None = None,
) -> AppContainer:
    invoice_service = InvoiceService(JsonInvoiceRepository(settings.invoices_dir))
    retriever = BM25PolicyRetriever.from_file(
        settings.policy_path, min_score=settings.retrieval_min_score
    )
    limiter = UsageLimiter(
        usage_store or create_usage_store(settings),
        session_limit=settings.session_request_limit,
        global_daily_limit=settings.global_daily_request_limit,
        daily_cost_limit_micro=int(settings.global_daily_cost_limit * 1_000_000),
    )
    chat_service = ChatService(
        settings=settings,
        invoice_service=invoice_service,
        retriever=retriever,
        provider=provider or create_provider(settings),
        limiter=limiter,
    )
    return AppContainer(settings, invoice_service, chat_service)


def create_app(
    settings: Settings | None = None,
    *,
    provider: ChatProvider | None = None,
    usage_store: UsageStore | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="InvoiceIQ API",
        version=__version__,
        description="AI Finance Operations Copilot — fictional demo data only.",
    )
    app.state.container = build_container(settings, provider=provider, usage_store=usage_store)
    app.middleware("http")(correlation_and_timing)
    app.include_router(router)

    @app.exception_handler(InvoiceNotFoundError)
    async def _not_found(_: Request, exc: InvoiceNotFoundError) -> JSONResponse:
        return error_response(404, "invoice_not_found", str(exc))

    @app.exception_handler(InputTooLongError)
    async def _input_error(_: Request, exc: InputTooLongError) -> JSONResponse:
        return error_response(422, "invalid_request", str(exc))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Report which fields are invalid, but never echo submitted values back.
        details = [
            {"field": ".".join(str(p) for p in err.get("loc", ())), "issue": err.get("msg", "")}
            for err in exc.errors()
        ]
        return error_response(422, "invalid_request", "The request is invalid.", details=details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return error_response(exc.status_code, f"http_{exc.status_code}", message)

    return app


def app_factory() -> FastAPI:
    """Entry point for uvicorn (`--factory`): configures logging, then builds the app."""
    settings = get_settings()
    configure_logging(settings.log_level)
    return create_app(settings)
