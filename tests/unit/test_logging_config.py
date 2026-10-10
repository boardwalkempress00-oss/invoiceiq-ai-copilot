"""Logging: Azure SDK noise suppressed, diagnostics kept, nothing sensitive printed."""

from __future__ import annotations

import json
import logging

import pytest

from invoiceiq.logging_config import configure_logging, correlation_id_var


@pytest.fixture
def json_logs(capsys):
    """Configure app logging into captured stdout; restore the root logger afterwards."""
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    levels = {n: logging.getLogger(n).level for n in ("azure", "openai", "httpx")}

    def read() -> list[dict]:
        out = capsys.readouterr().out
        return [json.loads(line) for line in out.splitlines() if line.startswith("{")]

    yield read
    root.handlers[:], root.level = saved[0], saved[1]
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


def test_azure_sdk_info_is_suppressed_but_warnings_kept(json_logs, monkeypatch):
    monkeypatch.delenv("AZURE_SDK_LOG_LEVEL", raising=False)
    configure_logging("INFO")
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").info(
        "Request URL: 'https://x.table.core.windows.net/aiusage()' Authorization: 'REDACTED'"
    )
    logging.getLogger("azure.identity").info("DefaultAzureCredential acquired a token")
    logging.getLogger("azure.identity").warning("ManagedIdentityCredential.get_token failed")
    messages = [r["message"] for r in json_logs()]
    assert messages == ["ManagedIdentityCredential.get_token failed"]


def test_azure_sdk_level_is_configurable(json_logs, monkeypatch):
    monkeypatch.setenv("AZURE_SDK_LOG_LEVEL", "INFO")
    configure_logging("INFO")
    logging.getLogger("azure.identity").info("token acquired")
    assert [r["message"] for r in json_logs()] == ["token acquired"]


def test_app_diagnostics_and_correlation_ids_are_kept(json_logs):
    configure_logging("INFO")
    token = correlation_id_var.set("abc-12345678")
    try:
        logging.getLogger("invoiceiq.ai.ai_service").info(
            "chat.completed",
            extra={"ai_ms": 812.4, "prompt_tokens": 1429, "completion_tokens": 106},
        )
        logging.getLogger("invoiceiq.ai.ai_service").error("usage_limiter.unavailable")
        logging.getLogger("openai._base_client").info("Retrying request in 0.5 seconds")
    finally:
        correlation_id_var.reset(token)
    records = json_logs()
    chat = next(r for r in records if r["message"] == "chat.completed")
    assert chat["correlation_id"] == "abc-12345678"
    assert (chat["ai_ms"], chat["prompt_tokens"], chat["completion_tokens"]) == (812.4, 1429, 106)
    assert any(
        r["message"] == "usage_limiter.unavailable" and r["level"] == "ERROR" for r in records
    )
    assert any("Retrying request" in r["message"] for r in records)  # SDK retries stay visible


def test_credential_like_fields_are_redacted(json_logs):
    configure_logging("INFO")
    logging.getLogger("invoiceiq").info(
        "x", extra={"authorization": "Bearer eyJ-secret", "api_key": "k-123", "prompt_tokens": 5}
    )
    (record,) = json_logs()
    assert record["authorization"] == "[REDACTED]" and record["api_key"] == "[REDACTED]"
    assert record["prompt_tokens"] == 5
    assert "eyJ-secret" not in json.dumps(record)


def test_chat_request_never_logs_question_text(json_logs):
    from fastapi.testclient import TestClient

    from invoiceiq.api.main import create_app
    from invoiceiq.configuration import Settings

    configure_logging("INFO")
    client = TestClient(create_app(Settings(_env_file=None)))
    secret_question = "Explain this invoice, my phone number is 12345678 ZXQW"
    client.post(
        "/chat",
        json={"invoice_id": "inv-001", "question": secret_question, "session_id": "log-test-01"},
    )
    dumped = json.dumps(json_logs())
    assert "ZXQW" not in dumped and "12345678 ZXQW" not in dumped
    assert '"chat.completed"' in dumped and '"question_chars"' in dumped
