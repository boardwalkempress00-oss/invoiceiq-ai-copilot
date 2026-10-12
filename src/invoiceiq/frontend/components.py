"""Reusable Streamlit UI pieces and the application stylesheet."""

from __future__ import annotations

import html
from typing import Any

import streamlit as st

from invoiceiq.frontend import state
from invoiceiq.frontend.api_client import ApiError
from invoiceiq.frontend.presentation import AiMode, ai_mode, money, status_style

# Page scripts live in views/, not pages/: a pages/ folder would trigger Streamlit's legacy
# automatic navigation and bypass app.py (sidebar, styles and layout).
PAGE_PATHS = {
    "dashboard": "views/dashboard.py",
    "analysis": "views/analysis.py",
    "chat": "views/chat.py",
    "process": "views/process.py",
    "architecture": "views/architecture.py",
    "observability": "views/observability.py",
}

_CSS = """
<style>
:root {
  --iq-navy: #14324f;
  --iq-blue: #1f5f99;
  --iq-ink: #1b2430;
  --iq-muted: #5b6676;
  --iq-line: #e3e8ef;
  --iq-surface: #f5f7fa;
}
.block-container { padding-top: 2rem; max-width: 1280px; }
h1, h2, h3 { color: var(--iq-ink); letter-spacing: -0.01em; }
.iq-hero {
  background: linear-gradient(135deg, #14324f 0%, #1f5f99 100%);
  color: #fff; border-radius: 14px; padding: 28px 32px; margin-bottom: 1.25rem;
}
.iq-hero h1 { color: #fff; font-size: 2rem; margin: 0 0 .35rem 0; padding: 0; }
.iq-hero p { color: #dbe7f3; margin: 0; font-size: 1.02rem; max-width: 820px; }
.iq-hero .iq-tag {
  display: inline-block; margin-top: 14px; padding: 3px 10px; border-radius: 999px;
  background: rgba(255,255,255,.14); color: #fff; font-size: .78rem; letter-spacing: .02em;
}
.iq-eyebrow {
  text-transform: uppercase; font-size: .72rem; letter-spacing: .08em;
  color: var(--iq-muted); font-weight: 600; margin-bottom: .15rem;
}
.iq-amount { font-size: 1.55rem; font-weight: 650; color: var(--iq-ink); line-height: 1.2; }
.iq-sub { color: var(--iq-muted); font-size: .88rem; }
.iq-dl { display: grid; grid-template-columns: max-content 1fr; gap: 6px 18px; font-size: .93rem;
  margin: 0; padding: 0; }
.iq-dl dt { color: var(--iq-muted); }
.iq-dl dd { margin: 0; color: var(--iq-ink); }
.iq-quote {
  border-left: 3px solid var(--iq-blue); background: var(--iq-surface);
  padding: 8px 12px; border-radius: 0 8px 8px 0; color: var(--iq-ink); font-size: .9rem;
}
.iq-kv { font-size: .9rem; color: var(--iq-ink); }
.iq-h4 { font-size: 1.25rem; font-weight: 650; color: var(--iq-ink); margin: .2rem 0; }
.iq-finding p { margin: .35rem 0; color: var(--iq-ink); font-size: .93rem; }
.iq-finding-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px 16px;
  font-size: .9rem; margin: .35rem 0; }
.iq-list { margin: .25rem 0 .5rem 1.1rem; padding: 0; font-size: .9rem; }
.iq-kv b { color: var(--iq-muted); font-weight: 600; }
</style>
"""


def inject_css() -> None:
    st.html(_CSS)


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


# --- Untrusted values ------------------------------------------------------------------------
# Anything that can come from a submitted invoice — invoice numbers, vendor names, line IDs,
# currency codes, schema field names, and validation messages that quote them — is rendered as
# escaped HTML, never as Markdown, so it cannot become a link, an image, formatting or markup.

_MD_PUNCTUATION = set("\\`*_{}[]()#+-.!|<>~:\"'&=$%^@,;/?")


def md_literal(value: Any) -> str:
    """Backslash-escape a value for Streamlit widgets that only accept Markdown (alerts).

    Every ASCII punctuation character is escaped, so no Markdown construct, HTML tag or
    autolink can form; newlines collapse to spaces.
    """
    text = " ".join(("" if value is None else str(value)).split())
    return "".join(f"\\{c}" if c in _MD_PUNCTUATION else c for c in text)


def untrusted_html(value: Any, css_class: str = "iq-sub") -> str:
    return f'<div class="{css_class}">{esc(value)}</div>'


def finding_html(finding: dict[str, Any]) -> str:
    """One validation finding as escaped HTML (title, ID, description, values, action)."""
    expected = finding.get("expected_value") or "—"
    actual = finding.get("actual_value") or "—"
    return (
        '<div class="iq-finding">'
        f"<div><b>{esc(finding.get('title'))}</b> · "
        f"<code>{esc(finding.get('finding_id'))}</code></div>"
        f"<p>{esc(finding.get('description'))}</p>"
        '<div class="iq-finding-grid">'
        f"<div><b>Expected</b><br>{esc(expected)}</div>"
        f"<div><b>Actual</b><br>{esc(actual)}</div>"
        f"<div><b>Policy</b><br><code>{esc(finding.get('policy_reference'))}</code></div>"
        "</div>"
        f"<p><b>Recommended action:</b> {esc(finding.get('recommended_action'))}</p>"
        "</div>"
    )


def details_html(details: list[Any]) -> str:
    items = "".join(f"<li><code>{esc(d)}</code></li>" for d in details)
    return f'<ul class="iq-list">{items}</ul>'


def hero(title: str, subtitle: str, tag: str | None = None) -> None:
    tag_html = f'<span class="iq-tag">{esc(tag)}</span>' if tag else ""
    st.html(f'<div class="iq-hero"><h1>{esc(title)}</h1><p>{esc(subtitle)}</p>{tag_html}</div>')


def status_badge(status: str | None) -> None:
    style = status_style(status)
    st.badge(style.label, icon=style.icon, color=style.color)  # type: ignore[arg-type]


def definition_list(items: list[tuple[str, Any]]) -> None:
    body = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v) if v not in (None, '') else '—'}</dd>"
                   for k, v in items)  # fmt: skip
    st.html(f'<dl class="iq-dl">{body}</dl>')


def amount_block(label: str, value: str | None, currency: str | None, note: str = "") -> None:
    note_html = f'<div class="iq-sub">{esc(note)}</div>' if note else ""
    st.html(
        f'<div class="iq-eyebrow">{esc(label)}</div>'
        f'<div class="iq-amount">{esc(money(value, currency))}</div>{note_html}'
    )


def ai_mode_banner(health: dict[str, Any] | None) -> AiMode:
    mode = ai_mode(health)
    show = {"success": st.success, "info": st.info, "warning": st.warning, "error": st.error}
    show[mode.tone](f"**{mode.label}** — {mode.description}", icon=mode.icon)
    return mode


def backend_error(error: ApiError) -> None:
    ref = f" (reference: `{error.correlation_id}`)" if error.correlation_id else ""
    st.error(f"{error.message}{ref}", icon=":material/error:")


PICKER_GENERATION_KEY = "_invoice_picker_generation"


def invoice_selector(invoices: list[dict[str, Any]], key: str) -> str:
    """Invoice picker kept in sync across pages through session state."""
    ids = [inv["invoice_id"] for inv in invoices]
    labels = {
        inv["invoice_id"]: f"{inv['invoice_number'] or inv['invoice_id']} · {inv['vendor_name']}"
        f" · {status_style(inv['status']).symbol} {inv['status']}"
        for inv in invoices
    }
    current = state.selected_invoice(ids[0])
    index = ids.index(current) if current in ids else 0
    # After a history restore the selection changes server-side; a new widget identity makes the
    # browser show it (a widget keeps its own client-side value for an unchanged key).
    generation = st.session_state.get(PICKER_GENERATION_KEY, 0)
    widget_key = key if not generation else f"{key}-{generation}"
    chosen = st.selectbox(
        "Invoice", ids, index=index, format_func=labels.__getitem__, key=widget_key
    )
    state.select_invoice(chosen)
    return chosen


def go(page: str) -> None:
    st.switch_page(PAGE_PATHS[page])
