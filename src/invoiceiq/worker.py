"""Worker entry point for one Container Apps Job execution.

    python -m invoiceiq.worker

Receives BlobCreated messages from Service Bus, validates each invoice with the deterministic
engine, stores the result, and exits when the queue is empty, the message cap is reached or the
time budget is spent. KEDA starts a new execution while messages are waiting.

The worker never imports the AI package: it has no model client, no AI settings and (in Azure)
no Azure OpenAI role assignment. Exit code 0 means the run finished normally, even if some
invoices failed validation or were retried; 2 means the worker is misconfigured.
"""

from __future__ import annotations

import logging
import sys

from invoiceiq.configuration import Settings, get_settings
from invoiceiq.logging_config import configure_logging
from invoiceiq.processing.processor import InvoiceProcessor, ProcessorConfig, RunLimits, run_once

logger = logging.getLogger("invoiceiq.worker")


def processor_config(settings: Settings) -> ProcessorConfig:
    return ProcessorConfig(
        container=settings.upload_container,
        max_invoice_bytes=settings.submission_max_bytes,
        expected_blob_host=settings.expected_blob_host,
        lease_seconds=settings.worker_lease_seconds,
        max_delivery_count=settings.worker_max_delivery_count,
    )


def run_limits(settings: Settings) -> RunLimits:
    return RunLimits(
        max_messages=settings.worker_max_messages_per_run,
        receive_wait_seconds=settings.worker_receive_wait_seconds,
        budget_seconds=settings.worker_run_budget_seconds,
    )


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    from invoiceiq.processing.azure_table_store import AzureTableSubmissionStore
    from invoiceiq.processing.blobs import AzureBlobStore
    from invoiceiq.processing.messages import AzureServiceBusSource

    try:
        store = AzureTableSubmissionStore.from_settings(settings)
        blobs = AzureBlobStore.from_settings(settings)
        source = AzureServiceBusSource.from_settings(settings)
    except Exception as exc:
        logger.error("worker.misconfigured", extra={"error": type(exc).__name__})
        return 2
    try:
        processor = InvoiceProcessor(store, blobs, source, processor_config(settings))
        logger.info("worker.start", extra={"worker_id": processor.worker_id})
        run_once(processor, run_limits(settings))
    finally:
        source.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
