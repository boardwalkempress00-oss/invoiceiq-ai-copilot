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
from invoiceiq.api.demo_routes import router as demo_router
from invoiceiq.api.middleware import correlation_and_timing, error_response
from invoiceiq.api.routes import AppContainer, InputTooLongError, router
from invoiceiq.api.submission_routes import router as submission_router
from invoiceiq.api.submission_routes import submission_error_response
from invoiceiq.configuration import Settings, SubmissionBackend, UsageStoreName, get_settings
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
from invoiceiq.processing.demo import DemoError, DemoService
from invoiceiq.processing.submissions import SubmissionError, SubmissionService

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


def create_submission_service(
    settings: Settings, invoice_service: InvoiceService
) -> SubmissionService | None:
    """None when the feature is off. Independent of the chat service and its usage store."""
    if not settings.async_processing_enabled:
        return None
    from invoiceiq.processing.processor import ProcessorConfig

    if settings.submission_backend == SubmissionBackend.AZURE:
        from invoiceiq.processing.azure_table_store import AzureTableSubmissionStore
        from invoiceiq.processing.blobs import AzureBlobStore

        try:
            store = AzureTableSubmissionStore.from_settings(settings)
            blobs = AzureBlobStore.from_settings(settings)
            if settings.azure_table_connection_string is not None:
                store.ensure_table()  # local Azurite convenience; Terraform creates them in Azure
            if settings.azure_blob_connection_string is not None:
                blobs.ensure_container()
        except Exception as exc:
            # Fail closed for this feature only; chat and validation keep working.
            logger.error("submission_backend.unavailable", extra={"error": type(exc).__name__})
            return None
        return SubmissionService(settings, store, blobs, invoice_service)

    from invoiceiq.processing.blobs import InMemoryBlobStore
    from invoiceiq.processing.local_pipeline import LocalPipeline
    from invoiceiq.processing.store import InMemorySubmissionStore

    mem_store, mem_blobs = InMemorySubmissionStore(), InMemoryBlobStore()
    pipeline = LocalPipeline(
        mem_store,
        mem_blobs,
        ProcessorConfig(
            container=settings.upload_container,
            max_invoice_bytes=settings.submission_max_bytes,
            lease_seconds=settings.worker_lease_seconds,
            max_delivery_count=settings.worker_max_delivery_count,
        ),
        delay_seconds=settings.local_pipeline_delay_seconds,
    )
    return SubmissionService(
        settings, mem_store, mem_blobs, invoice_service, on_uploaded=pipeline.on_uploaded
    )


def create_demo_service(settings: Settings) -> DemoService | None:
    """None when the demo is off. Built from in-memory parts only — never from Azure clients."""
    if not settings.demo_mode_enabled:
        return None
    return DemoService.from_settings(settings)


def build_container(
    settings: Settings,
    *,
    provider: ChatProvider | None = None,
    usage_store: UsageStore | None = None,
    submission_service: SubmissionService | None = None,
    demo_service: DemoService | None = None,
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
    return AppContainer(
        settings,
        invoice_service,
        chat_service,
        submission_service or create_submission_service(settings, invoice_service),
        demo_service or create_demo_service(settings),
    )


def create_app(
    settings: Settings | None = None,
    *,
    provider: ChatProvider | None = None,
    usage_store: UsageStore | None = None,
    submission_service: SubmissionService | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="InvoiceIQ API",
        version=__version__,
        description="AI Finance Operations Copilot — fictional demo data only.",
    )
    app.state.container = build_container(
        settings,
        provider=provider,
        usage_store=usage_store,
        submission_service=submission_service,
    )
    app.middleware("http")(correlation_and_timing)
    app.include_router(router)
    app.include_router(submission_router)
    app.include_router(demo_router)

    @app.exception_handler(DemoError)
    async def _demo_error(_: Request, exc: DemoError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(SubmissionError)
    async def _submission_error(_: Request, exc: SubmissionError) -> JSONResponse:
        return submission_error_response(exc)

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
