"""Asynchronous invoice processing: upload → Blob Storage → Event Grid → Service Bus → worker.

The modules here are infrastructure-agnostic. Each Azure dependency sits behind a small protocol
(`SubmissionStore`, `BlobStore`, `MessageSource`) with an in-memory implementation for tests and
local development, so the processing rules can be verified without any cloud resources.
"""
