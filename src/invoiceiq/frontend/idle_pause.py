"""Idle pause: close genuinely inactive Streamlit sessions so the app can scale to zero.

Why this is needed: Streamlit keeps a WebSocket open for every browser tab and reconnects
automatically if it drops, so an abandoned tab keeps the container replica running. Streamlit
has no idle timeout of its own.

How it works (all in the browser — no server-side timers or polling):

* One Streamlit Components v2 component, mounted on every page. Its JavaScript installs a single
  idle controller per tab: trusted user events (pointer, keyboard, wheel, touch, scroll) reset one
  `setTimeout` to the next deadline.
* Visible tab: pause after `INVOICEIQ_IDLE_TIMEOUT_MINUTES` (default 15) without interaction.
  Hidden tab: pause `INVOICEIQ_HIDDEN_TIMEOUT_MINUTES` (default 5) after it was hidden.
  A warning banner appears `INVOICEIQ_IDLE_WARNING_SECONDS` (default 60) before pausing; any
  activity cancels it. A value of 0 disables that timeout.
* Pausing navigates the tab to a static page (`static/paused.html`). Leaving the Streamlit page
  closes its WebSocket and stops the client, so it cannot reconnect. "Resume" needs a click.
* Chat history survives pause/resume in the same tab: a size-capped snapshot is kept in
  `sessionStorage` (per tab, cleared when the tab closes) and handed back to Python through the
  component on the next session. Restored data is untrusted browser input, so it is validated
  and rebuilt from an allowlist before use. No secrets or credentials are ever stored.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from typing import Any

STORAGE_VERSION = 1
MAX_MESSAGES_PER_INVOICE = 20
MAX_SNAPSHOT_CHARS = 200_000
MAX_TEXT_CHARS = 8_000
MAX_LIST_ITEMS = 50

_INVOICE_ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")
_EVIDENCE_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Z0-9][A-Z0-9._-]{0,40}$")
_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


# --- Configuration -----------------------------------------------------------------------------


@dataclass(frozen=True)
class IdleConfig:
    idle_minutes: float = 15.0
    hidden_minutes: float = 5.0
    warning_seconds: float = 60.0

    @classmethod
    def from_env(cls) -> IdleConfig:
        return cls(
            idle_minutes=_non_negative("INVOICEIQ_IDLE_TIMEOUT_MINUTES", cls.idle_minutes),
            hidden_minutes=_non_negative("INVOICEIQ_HIDDEN_TIMEOUT_MINUTES", cls.hidden_minutes),
            warning_seconds=_non_negative("INVOICEIQ_IDLE_WARNING_SECONDS", cls.warning_seconds),
        )

    def to_js(self) -> dict[str, int]:
        """Milliseconds for the browser. 0 disables the respective timeout."""
        return {
            "idleMs": round(self.idle_minutes * 60_000),
            "hiddenMs": round(self.hidden_minutes * 60_000),
            "warningMs": round(self.warning_seconds * 1_000),
        }


def _non_negative(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= 0 else default


# --- History snapshot (browser sessionStorage) -------------------------------------------------


def serialize_history(chats: dict[str, list[dict[str, Any]]], selected: str | None) -> str:
    """JSON snapshot of chat history for sessionStorage, newest messages kept, size-capped."""
    trimmed: dict[str, list[dict[str, Any]]] = {}
    for invoice_id, messages in chats.items():
        kept = [
            {
                k: m[k]
                for k in ("role", "content", "response", "round_trip_ms")
                if k in m and m[k] is not None
            }
            for m in messages
            if not m.get("error")  # transient failures are not worth restoring
        ]
        if kept:
            trimmed[invoice_id] = kept[-MAX_MESSAGES_PER_INVOICE:]

    def dump() -> str:
        return json.dumps(
            {"v": STORAGE_VERSION, "selected": selected, "chats": trimmed},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    snapshot = dump()
    while len(snapshot) > MAX_SNAPSHOT_CHARS and trimmed:
        # Drop the oldest message of the largest conversation until it fits.
        largest = max(trimmed, key=lambda k: len(json.dumps(trimmed[k])))
        trimmed[largest] = trimmed[largest][1:]
        if not trimmed[largest]:
            del trimmed[largest]
        snapshot = dump()
    return snapshot


def restore_history(
    raw: object, valid_invoice_ids: set[str]
) -> tuple[dict[str, list[dict[str, Any]]], str | None] | None:
    """Validate a snapshot from the browser and rebuild it from allowed fields only."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_SNAPSHOT_CHARS * 2:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("v") != STORAGE_VERSION:
        return None
    chats_in = data.get("chats")
    if not isinstance(chats_in, dict):
        return None

    chats: dict[str, list[dict[str, Any]]] = {}
    for invoice_id, messages in chats_in.items():
        if not (isinstance(invoice_id, str) and _INVOICE_ID_RE.match(invoice_id)):
            continue
        if invoice_id not in valid_invoice_ids or not isinstance(messages, list):
            continue
        clean = [m for m in (_clean_message(x) for x in messages[-MAX_MESSAGES_PER_INVOICE:]) if m]
        if clean:
            chats[invoice_id] = clean
    selected = data.get("selected")
    if not (isinstance(selected, str) and selected in valid_invoice_ids):
        selected = None
    return chats, selected


def _text(value: object, limit: int = MAX_TEXT_CHARS) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _clean_message(message: object) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None
    role, content = message.get("role"), _text(message.get("content"))
    if role not in ("user", "assistant") or not content:
        return None
    if role == "user":
        return {"role": "user", "content": content}
    response = _clean_response(message.get("response"))
    if response is None:
        return None
    return {
        "role": "assistant",
        "content": content,
        "response": response,
        "round_trip_ms": _number(message.get("round_trip_ms")) or 0.0,
    }


def _clean_response(response: object) -> dict[str, Any] | None:
    if not isinstance(response, dict) or response.get("mode") not in ("ai", "read_only"):
        return None
    answer = _text(response.get("answer"))
    if answer is None:
        return None
    citations = []
    for c in _list(response.get("citations")):
        if isinstance(c, dict) and _EVIDENCE_ID_RE.match(str(c.get("evidence_id", ""))):
            citations.append(
                {
                    "evidence_id": c["evidence_id"],
                    "title": _text(c.get("title"), 200) or "",
                    "kind": _text(c.get("kind"), 40) or "",
                }
            )
    facts = [
        {
            "label": _text(f.get("label"), 200) or "",
            "value": _text(f.get("value"), 1_000) or "",
            "source_id": _text(f.get("source_id"), 60) or "",
        }
        for f in _list(response.get("verified_facts"))
        if isinstance(f, dict)
    ]
    usage_in = response.get("usage")
    usage = None
    if isinstance(usage_in, dict) and usage_in.get("source") in (
        "provider",
        "estimated",
        "simulated",
    ):
        total = _number(usage_in.get("total_tokens"))
        if total is not None:
            usage = {"total_tokens": int(total), "source": usage_in["source"]}
    timings_in = response.get("timings") if isinstance(response.get("timings"), dict) else {}
    limits_in = response.get("limits") if isinstance(response.get("limits"), dict) else {}
    correlation_id = str(response.get("correlation_id", ""))
    cost = response.get("estimated_cost")
    return {
        "mode": response["mode"],
        "answer": answer,
        "notice": _text(response.get("notice"), 500),
        "read_only_reason": _text(response.get("read_only_reason"), 60),
        "verified_facts": facts,
        "citations": citations,
        "policy_evidence_found": bool(response.get("policy_evidence_found", True)),
        "removed_citations": [
            x
            for x in _list(response.get("removed_citations"))
            if isinstance(x, str) and _EVIDENCE_ID_RE.match(x)
        ],
        "provider": _text(response.get("provider"), 40),
        "usage": usage,
        "estimated_cost": cost
        if isinstance(cost, str) and re.match(r"^\d{1,6}(\.\d{1,9})?$", cost)
        else None,
        "cost_currency": _text(response.get("cost_currency"), 8),
        "cost_is_simulated": bool(response.get("cost_is_simulated", False)),
        "timings": {"ai_ms": _number(timings_in.get("ai_ms"))},
        "limits": {"session_remaining": _int_or_none(limits_in.get("session_remaining"))},
        "correlation_id": correlation_id
        if _CORRELATION_ID_RE.match(correlation_id)
        else "restored",
    }


def _list(value: object) -> list[Any]:
    return value[:MAX_LIST_ITEMS] if isinstance(value, list) else []


def _int_or_none(value: object) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


# --- Browser component -------------------------------------------------------------------------

STORAGE_KEY = "invoiceiq.chat.v1"
RESUME_KEY = "invoiceiq.resume.path"
PAUSED_PAGE = "/app/static/paused.html"

# Trusted, first-party code. No data is ever interpolated into it: configuration and the history
# snapshot arrive through the component's `data` channel, and all page text is set with
# `textContent`, never `innerHTML`.
_JS = r"""
const STORAGE_KEY = "invoiceiq.chat.v1";
const RESUME_KEY = "invoiceiq.resume.path";
const PAUSED_PAGE = "/app/static/paused.html";
const ACTIVITY_EVENTS = ["pointerdown", "pointermove", "keydown", "wheel", "touchstart", "scroll"];

function createController() {
  const c = { idleMs: 0, hiddenMs: 0, warningMs: 60000, pauseTimer: null, warnTimer: null,
              paused: false, banner: null, lastActivity: Date.now(), hiddenSince: null,
              lastSchedule: 0 };
  if (document.visibilityState === "hidden") c.hiddenSince = Date.now();

  function hideBanner() { if (c.banner) { c.banner.remove(); c.banner = null; } }

  function showBanner() {
    if (c.paused || c.banner) return;
    const box = document.createElement("div");
    box.setAttribute("role", "alert");
    box.setAttribute("data-testid", "invoiceiq-idle-warning");
    box.style.cssText = "position:fixed;left:50%;bottom:24px;transform:translateX(-50%);" +
      "z-index:2147483647;background:#14324f;color:#fff;padding:14px 18px;border-radius:10px;" +
      "box-shadow:0 6px 24px rgba(0,0,0,.25);font:15px/1.4 system-ui,sans-serif;max-width:560px;";
    const text = document.createElement("span");
    const seconds = Math.round(c.warningMs / 1000);
    text.textContent = "This session will pause in about " + seconds +
      " seconds because of inactivity. Move the mouse or press a key to keep working.";
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Keep working";
    button.style.cssText = "margin-left:12px;padding:6px 12px;border-radius:6px;border:0;" +
      "background:#fff;color:#14324f;font-weight:600;cursor:pointer;";
    box.append(text, button);
    document.body.appendChild(box);
    c.banner = box;
  }

  function clearTimers() {
    clearTimeout(c.pauseTimer); clearTimeout(c.warnTimer);
    c.pauseTimer = c.warnTimer = null;
  }

  function schedule() {
    clearTimers(); hideBanner();
    if (c.paused) return;
    const hidden = document.visibilityState === "hidden";
    const limit = hidden ? c.hiddenMs : c.idleMs;
    if (!limit) return;                                   // 0 disables this timeout
    const deadline = (hidden ? c.hiddenSince : c.lastActivity) + limit;
    const now = Date.now();
    c.warnTimer = setTimeout(showBanner, Math.max(0, deadline - c.warningMs - now));
    c.pauseTimer = setTimeout(pause, Math.max(0, deadline - now));
  }

  function pause() {
    if (c.paused) return;
    c.paused = true; clearTimers();
    try { sessionStorage.setItem(RESUME_KEY, location.pathname); } catch (e) {}
    // Leaving the page unloads the Streamlit client: its WebSocket closes and cannot reconnect.
    window.location.assign(PAUSED_PAGE);
  }

  function onActivity(event) {
    if (!event.isTrusted || c.paused || document.visibilityState === "hidden") return;
    c.lastActivity = Date.now();
    // Re-arm at most once per second (pointermove fires very often), or at once if warning.
    if (c.banner || c.lastActivity - c.lastSchedule > 1000) {
      c.lastSchedule = c.lastActivity;
      schedule();
    }
  }

  function onVisibility() {
    if (document.visibilityState === "hidden") {
      c.hiddenSince = Date.now();
    } else {
      c.hiddenSince = null;
      c.lastActivity = Date.now();                        // returning to the tab is activity
    }
    schedule();
  }

  ACTIVITY_EVENTS.forEach((name) =>
    window.addEventListener(name, onActivity, { capture: true, passive: true }));
  document.addEventListener("visibilitychange", onVisibility);

  c.configure = (cfg) => {
    const ms = (v, d) => (Number.isFinite(v) && v >= 0 ? v : d);
    const next = { idleMs: ms(cfg.idleMs, 0), hiddenMs: ms(cfg.hiddenMs, 0),
                   warningMs: ms(cfg.warningMs, 60000) };
    if (next.idleMs !== c.idleMs || next.hiddenMs !== c.hiddenMs ||
        next.warningMs !== c.warningMs || (!c.pauseTimer && !c.paused)) {
      Object.assign(c, next);
      schedule();
    }
  };
  return c;
}

export default function (component) {
  const { data, setStateValue } = component;
  const payload = data || {};
  const OWNER_KEY = "invoiceiq.session";

  // Chat history for this tab. Each Streamlit session has a random nonce; the snapshot belongs to
  // the session recorded under OWNER_KEY. A new session first offers the saved history to Python
  // (only if there is any, so a first visit triggers no extra rerun), and only takes ownership
  // and starts overwriting once Python has acknowledged the restore.
  try {
    if (payload.nonce && sessionStorage.getItem(OWNER_KEY) !== payload.nonce) {
      const saved = sessionStorage.getItem(STORAGE_KEY) || "";
      if (saved && !payload.restoreAck) {
        if (window.__invoiceiqRestoreOffered !== payload.nonce) {
          window.__invoiceiqRestoreOffered = payload.nonce;
          setStateValue("restored", saved);
        }
      } else {
        sessionStorage.setItem(OWNER_KEY, payload.nonce);
      }
    }
    if (payload.nonce && sessionStorage.getItem(OWNER_KEY) === payload.nonce &&
        typeof payload.snapshot === "string") {
      sessionStorage.setItem(STORAGE_KEY, payload.snapshot);
    }
  } catch (e) { /* storage unavailable (e.g. blocked): history simply is not kept */ }

  // One idle controller per tab, reconfigured on every rerun.
  if (!window.__invoiceiqIdle) window.__invoiceiqIdle = createController();
  window.__invoiceiqIdle.configure(payload.config || {});
}
"""


def mount(chats: dict[str, list[dict[str, Any]]], selected: str | None, state: Any) -> Any:
    """Mount the idle-pause component; return a saved history snapshot to restore, or None.

    `state` is Streamlit's session state. A snapshot is offered at most once per Streamlit
    session, and only when the tab has saved history.
    """
    import secrets

    import streamlit as st

    nonce = state.setdefault("_idle_session_nonce", secrets.token_hex(8))
    acknowledged = state.get("_history_restore_ack", False)
    result = _component(st)(
        key="invoiceiq_idle_pause",
        data={
            "config": IdleConfig.from_env().to_js(),
            "nonce": nonce,
            "restoreAck": acknowledged,
            "snapshot": serialize_history(chats, selected),
        },
        on_restored_change=lambda: None,
    )
    restored = getattr(result, "restored", None) if result is not None else None
    if restored is not None and not acknowledged:
        state["_history_restore_ack"] = True
        return restored
    return None


def _component(st: Any) -> Any:
    # Registered on every script run, as in Streamlit's own examples: the registry belongs to
    # the Streamlit runtime, so a definition cached at module level would go stale.
    return st.components.v2.component("invoiceiq_idle_pause", js=_JS)
