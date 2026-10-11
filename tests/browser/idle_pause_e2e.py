"""Browser end-to-end checks for the idle pause (manual; needs Chrome and running servers).

Not collected by pytest. Run against two local instances with short timeouts:

    uv run uvicorn invoiceiq.api.main:app_factory --factory --port 8010
    INVOICEIQ_API_URL=http://127.0.0.1:8010 INVOICEIQ_IDLE_TIMEOUT_MINUTES=0.1 \
      INVOICEIQ_HIDDEN_TIMEOUT_MINUTES=0.05 INVOICEIQ_IDLE_WARNING_SECONDS=2 \
      uv run streamlit run src/invoiceiq/frontend/app.py --server.port 8701
    INVOICEIQ_API_URL=http://127.0.0.1:8010 INVOICEIQ_IDLE_TIMEOUT_MINUTES=0 \
      INVOICEIQ_HIDDEN_TIMEOUT_MINUTES=0.05 INVOICEIQ_IDLE_WARNING_SECONDS=1 \
      uv run streamlit run src/invoiceiq/frontend/app.py --server.port 8702
    uv run --no-project --with playwright python tests/browser/idle_pause_e2e.py

The cold-start loading view is checked separately in tests/browser/cold_start_e2e.py.

Hidden tabs are simulated by overriding document.visibilityState (headless Chrome never hides
a tab); WebSocket closure is verified server-side by counting ESTABLISHED TCP connections (lsof).
"""

import json
import time

from playwright.sync_api import sync_playwright

A, B = (
    "http://localhost:8701",
    "http://localhost:8702",
)  # A: idle 6s/hidden 3s/warn 2s; B: idle OFF, hidden 3s
results = []


def ok(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)


def open_app(browser, base, path="/"):
    ctx = browser.new_context(viewport={"width": 1300, "height": 900})
    pg = ctx.new_page()
    log = {"open": 0, "closed": 0, "opened_at": [], "console": [], "pageerrors": []}

    def on_ws(ws):
        if "_stcore/stream" in ws.url:
            log["open"] += 1
            log["opened_at"].append(time.time())
            ws.on("close", lambda _: log.__setitem__("closed", log["closed"] + 1))

    pg.on("websocket", on_ws)
    pg.on(
        "console",
        lambda m: log["console"].append(m.text) if m.type in ("error", "warning") else None,
    )
    pg.on("pageerror", lambda e: log["pageerrors"].append(str(e)))
    pg.goto(base + path, timeout=60000)
    pg.get_by_text("InvoiceIQ").first.wait_for(timeout=60000)
    pg.wait_for_function("() => !!window.__invoiceiqIdle", timeout=20000)
    return ctx, pg, log


def banner(pg):
    return pg.locator("[data-testid=invoiceiq-idle-warning]")


def paused(pg):
    return pg.url.endswith("/app/static/paused.html")


def hide(pg):
    pg.evaluate("""() => { Object.defineProperty(document,'visibilityState',{configurable:true,get:()=> 'hidden'});
        Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});
        document.dispatchEvent(new Event('visibilitychange')); }""")


with sync_playwright() as p:
    br = p.chromium.launch(channel="chrome", headless=True)

    # 1. Active interaction prevents pausing (A: idle 6 s) — 15 s of activity.
    ctx, pg, log = open_app(br, A)
    end = time.time() + 15
    while time.time() < end:
        pg.mouse.move(200 + int(time.time() * 37) % 400, 300)
        pg.wait_for_timeout(1500)
    ok(
        "1. active user is never paused (15 s of activity, idle limit 6 s)",
        not paused(pg) and banner(pg).count() == 0,
    )
    ctx.close()

    # 2. Warning appears, then activity cancels it and resets the timer.
    ctx, pg, log = open_app(br, A)
    banner(pg).wait_for(timeout=10000)
    shown = time.time()
    ok(
        "2a. warning banner shown before pausing",
        banner(pg).count() == 1,
        banner(pg).inner_text()[:70],
    )
    ok("2b. banner text rendered as text (role=alert)", banner(pg).get_attribute("role") == "alert")
    pg.mouse.move(500, 400)
    pg.wait_for_timeout(300)
    ok("2c. activity removes the warning", banner(pg).count() == 0)
    pg.wait_for_timeout(3000)  # past the original deadline (warning was 2 s before it)
    ok("2d. no pause at the original deadline after cancellation", not paused(pg))
    ctx.close()

    # 2e. Synthetic (untrusted) events must not keep a session alive.
    ctx, pg, log = open_app(br, A)
    for _ in range(8):
        pg.evaluate("() => window.dispatchEvent(new PointerEvent('pointermove'))")
        pg.wait_for_timeout(1000)
    pg.wait_for_url("**/app/static/paused.html", timeout=10000)
    ok("2e. scripted (untrusted) events do not prevent pausing", paused(pg))
    ctx.close()

    # 3. Idle pause closes the WebSocket and does not reconnect.
    import subprocess

    def established(port):  # server-side view: ESTABLISHED TCP connections to the Streamlit port
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:ESTABLISHED"], capture_output=True, text=True
        ).stdout
        return sum(1 for l in out.splitlines()[1:] if f":{port}->" in l)

    ctx = br.new_context(viewport={"width": 1300, "height": 900})
    pg = ctx.new_page()
    cdp = ctx.new_cdp_session(pg)
    cdp.send("Network.enable")
    ws = {"created": [], "closed": []}
    cdp.on(
        "Network.webSocketCreated",
        lambda e: ws["created"].append(e["url"]) if "_stcore/stream" in e["url"] else None,
    )
    cdp.on("Network.webSocketClosed", lambda e: ws["closed"].append(e["requestId"]))
    pg.goto(A + "/chat")
    pg.get_by_text("Suggested questions").wait_for(timeout=30000)
    pg.wait_for_function("() => !!window.__invoiceiqIdle", timeout=20000)
    conns_before = established(8701)
    t0 = time.time()
    pg.wait_for_url("**/app/static/paused.html", timeout=15000)
    took = time.time() - t0
    ok(
        "3a. idle visible tab pauses after the configured time",
        4 <= took <= 11,
        f"{took:.1f}s after page ready (limit 6 s)",
    )
    pg.wait_for_timeout(2000)
    pg.wait_for_timeout(3000)
    conns_after = established(8701)
    ok(
        "3b. WebSocket closed — server-side ESTABLISHED connections dropped",
        conns_after < conns_before,
        f"ESTABLISHED {conns_before} -> {conns_after}",
    )
    created_before = len(ws["created"])
    pg.wait_for_timeout(10000)
    ok(
        "3c. no reconnection for 10 s after pause (no new WebSocket via CDP, no new server connections)",
        len(ws["created"]) == created_before and established(8701) <= conns_after,
        f"ws created total={len(ws['created'])}",
    )
    ok(
        "3d. paused page shown with Resume",
        pg.get_by_text("Session paused").count() == 1
        and pg.get_by_role("link", name="Resume").count() == 1,
    )
    ok(
        "3e. Resume returns to the page that was open",
        pg.get_by_role("link", name="Resume").get_attribute("href") == "/chat",
    )
    ok("3f. paused page does not navigate on its own", paused(pg))
    ctx.close()

    # 4. Hidden tab pauses after the hidden timeout; idle-visible disabled (B: idle 0).
    ctx, pg, log = open_app(br, B)
    pg.wait_for_timeout(8000)
    ok(
        "4a. idle timeout 0 disables visible-tab pausing (8 s idle)",
        not paused(pg) and banner(pg).count() == 0,
    )
    t0 = time.time()
    hide(pg)
    pg.wait_for_url("**/app/static/paused.html", timeout=15000)
    ok(
        "4b. hidden tab pauses after the hidden timeout",
        2 <= time.time() - t0 <= 7,
        f"{time.time() - t0:.1f}s (limit 3 s)",
    )
    ctx.close()

    # 5. Chat history survives pause → Resume in the same tab.
    ctx, pg, log = open_app(br, B, "/chat")
    pg.get_by_test_id("stSelectbox").first.click()
    pg.get_by_role("option", name="KFS-2026-0203", exact=False).click()
    pg.wait_for_timeout(1500)
    pg.get_by_role("button", name="Why was this invoice flagged?").click()
    pg.get_by_text("Evidence cited").wait_for(timeout=30000)
    pg.wait_for_timeout(2500)
    saved = json.loads(pg.evaluate("() => sessionStorage.getItem('invoiceiq.chat.v1')") or "{}")
    ok("5a. snapshot stored in sessionStorage", len(saved.get("chats", {}).get("inv-003", [])) == 2)
    blob = json.dumps(saved)
    ok(
        "5b. no secrets/credentials in browser storage",
        not any(
            w in blob.lower()
            for w in ("accountkey", "bearer ", "api_key", "password", "client_secret")
        ),
    )
    hide(pg)
    pg.wait_for_url("**/app/static/paused.html", timeout=15000)
    pg.evaluate("() => { delete document.visibilityState; delete document.hidden; }")
    pg.get_by_role("link", name="Resume").click()
    pg.get_by_text("Evidence cited").wait_for(timeout=30000)
    pg.wait_for_timeout(1500)
    main = pg.locator("section.stMain, [data-testid=stMain]").first.inner_text()
    ok("5c. Resume requires a click and lands on /chat", pg.url.endswith("/chat"))
    ok(
        "5d. conversation restored after resume",
        "Why was this invoice flagged?" in main
        and "VAT amount mismatch" in main
        and "AI-generated · mock provider" in main,
    )
    picked = pg.get_by_role("combobox").first.evaluate("(e) => e.value")
    ok("5e. selected invoice restored", picked.startswith("KFS-2026-0203"), picked)
    pg.get_by_role("button", name="Explain this invoice.").click()
    pg.wait_for_function(
        "() => document.querySelectorAll('[data-testid=stChatMessage]').length >= 4", timeout=30000
    )
    pg.wait_for_timeout(1500)
    picked_after = pg.get_by_role("combobox").first.evaluate("(e) => e.value")
    n_msgs = pg.locator("[data-testid=stChatMessage]").count()
    ok(
        "5g. follow-up after resume stays on the restored invoice",
        picked_after.startswith("KFS-2026-0203") and n_msgs == 4,
        f"{picked_after[:13]}, {n_msgs} messages",
    )
    ctx.close()

    # 5f. Regression: a click right after a fresh page load must not be lost (no restore rerun).
    ctx, pg, log = open_app(br, B, "/chat")
    pg.get_by_role("button", name="Why was this invoice flagged?").click()
    try:
        pg.get_by_text("Evidence cited").wait_for(timeout=20000)
        answered = True
    except Exception:
        answered = False
    ok("5f. question clicked immediately after first load is answered", answered)
    ctx.close()

    # 7. Cold start UX: see tests/browser/cold_start_e2e.py (needs the async Playwright API).

    # 6. Malicious sessionStorage content cannot execute script or break the app.
    ctx, pg, log = open_app(br, B, "/chat")
    evil = {
        "v": 1,
        "selected": "inv-001",
        "chats": {
            "inv-001": [
                {
                    "role": "user",
                    "content": '<img src=x onerror="window.__xss=1"> **hi** [x](javascript:window.__xss=2)',
                },
                {
                    "role": "assistant",
                    "content": "a",
                    "response": {
                        "mode": "ai",
                        "answer": "<script>window.__xss=3</script><img src=x onerror=window.__xss=4>",
                        "citations": [
                            {
                                "evidence_id": "PP-3.1",
                                "title": "<img src=x onerror=window.__xss=5>",
                                "kind": "policy",
                            },
                            {"evidence_id": "<script>", "title": "bad"},
                        ],
                        "verified_facts": [
                            {
                                "label": "<b>x</b>",
                                "value": "<img src=x onerror=window.__xss=6>",
                                "source_id": "VAL",
                            }
                        ],
                        "correlation_id": '"><script>window.__xss=7</script>',
                        "estimated_cost": "1e9; alert(1)",
                        "evil_field": "<script>",
                    },
                },
            ],
            "../../etc": [{"role": "user", "content": "path"}],
            "inv-999": [{"role": "user", "content": "unknown invoice"}],
        },
    }
    pg.evaluate("(v) => sessionStorage.setItem('invoiceiq.chat.v1', v)", json.dumps(evil))
    pg.reload()
    pg.get_by_text("AI Chat").first.wait_for(timeout=30000)
    pg.wait_for_timeout(4000)
    main = pg.locator("section.stMain, [data-testid=stMain]").first.inner_text()
    ok("6a. no injected script executed", pg.evaluate("() => window.__xss === undefined"))
    ok(
        "6b. injected markup shown as text, not HTML",
        "<img src=x" in main and pg.locator("section.stMain img[src='x']").count() == 0,
    )
    ok(
        "6c. invalid invoice ids and fields dropped",
        "unknown invoice" not in main and "path" not in main.split("\n"),
    )
    ok("6d. app still works (no exception)", pg.locator("[data-testid=stException]").count() == 0)
    csp = [m for m in log["console"] if "Content Security Policy" in m or "Refused to" in m]
    ok("6e. no CSP violations in console", not csp, "; ".join(csp[:2]))
    ok("6f. no uncaught page errors", not log["pageerrors"], "; ".join(log["pageerrors"][:2]))
    ctx.close()
    br.close()
print(f"RESULT: {sum(results)}/{len(results)} passed")
