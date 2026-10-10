"""Per-browser-session state: API client, session ID, selected invoice and chat history."""

from __future__ import annotations

import secrets
import time
from typing import Any

import streamlit as st

from invoiceiq.frontend.api_client import ApiClient, ApiError

HEALTH_MAX_AGE_SECONDS = 30.0


def client() -> ApiClient:
    if "api_client" not in st.session_state:
        st.session_state.api_client = ApiClient()
    return st.session_state.api_client  # type: ignore[no-any-return]


def session_id() -> str:
    """Random, opaque ID used only for the backend's per-session AI allowance."""
    if "session_id" not in st.session_state:
        st.session_state.session_id = secrets.token_urlsafe(16)
    return st.session_state.session_id  # type: ignore[no-any-return]


def health(force: bool = False) -> dict[str, Any] | None:
    """Backend health, refreshed at most every 30 s. None if the backend is unreachable."""
    cached = st.session_state.get("_health")
    if not force and cached and time.monotonic() - cached[0] < HEALTH_MAX_AGE_SECONDS:
        return cached[1]  # type: ignore[no-any-return]
    try:
        value: dict[str, Any] | None = client().health()
    except ApiError:
        value = None
    st.session_state._health = (time.monotonic(), value)
    return value


def selected_invoice(default: str | None = None) -> str | None:
    return st.session_state.get("selected_invoice", default)  # type: ignore[no-any-return]


def select_invoice(invoice_id: str) -> None:
    st.session_state.selected_invoice = invoice_id


def chat_messages(invoice_id: str) -> list[dict[str, Any]]:
    """Chat history scoped to one invoice."""
    chats: dict[str, list[dict[str, Any]]] = st.session_state.setdefault("chats", {})
    return chats.setdefault(invoice_id, [])


def clear_chat(invoice_id: str) -> None:
    st.session_state.setdefault("chats", {})[invoice_id] = []
