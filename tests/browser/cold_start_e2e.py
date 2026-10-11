"""Browser check: Resume from a paused session shows "Starting InvoiceIQ" during a slow start.

Manual (not collected by pytest). Needs instance B from idle_pause_e2e.py (idle 0, hidden 3 s)
behind a reverse proxy that holds the app's HTML page requests for 8 s — the way Azure's ingress
holds requests while a replica starts from zero. Static files and WebSockets pass straight
through. (Playwright request interception is not used: it changes how Chrome renders a pending
navigation, so it would not test real browser behaviour.)

    COLD_START_BASE=http://localhost:8703 \
      uv run --no-project --with playwright python tests/browser/cold_start_e2e.py
"""

import asyncio
import os
import time

from playwright.async_api import async_playwright

B = os.getenv("COLD_START_BASE", "http://localhost:8703")
DELAY_S = 8
results: list[bool] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)


async def main() -> None:
    async with async_playwright() as p:
        br = await p.chromium.launch(channel="chrome", headless=True)
        pg = await br.new_page(viewport={"width": 1300, "height": 900})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        await pg.goto(B + "/chat", timeout=60000)
        await pg.get_by_text("Suggested questions").wait_for(timeout=60000)
        await pg.wait_for_function("() => !!window.__invoiceiqIdle", timeout=20000)
        await pg.get_by_role("button", name="Explain this invoice.").click()
        await pg.get_by_text("Evidence cited").wait_for(timeout=30000)
        await pg.wait_for_timeout(2000)
        await pg.evaluate(
            """() => { Object.defineProperty(document,'visibilityState',{configurable:true,get:()=>'hidden'});
            Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});
            document.dispatchEvent(new Event('visibilitychange')); }"""
        )
        await pg.wait_for_url("**/app/static/paused.html", timeout=15000)

        t0 = time.time()
        await pg.get_by_role("link", name="Resume").click(no_wait_after=True)
        await pg.wait_for_timeout(1500)
        starting = pg.locator("#starting-view")
        text = await starting.inner_text() if await starting.is_visible() else ""
        ok("starting view shown right after Resume", pg.url.endswith("paused.html") and bool(text))
        ok("message: 'Starting InvoiceIQ' / 'may take up to a minute'",
           "Starting InvoiceIQ" in text and "may take up to a minute" in text)  # fmt: skip
        ok("paused view hidden, spinner + progress bar present",
           not await pg.locator("#paused-view").is_visible()
           and await pg.locator("#starting-view .spinner").count() == 1
           and await pg.locator("#starting-view .bar").count() == 1)  # fmt: skip
        await pg.wait_for_timeout(4000)
        ok("still shown while the app has not responded (5.5 s)",
           pg.url.endswith("paused.html") and await starting.is_visible())  # fmt: skip
        await pg.get_by_text("Evidence cited").first.wait_for(timeout=60000)
        elapsed = time.time() - t0
        picked = await pg.get_by_role("combobox").first.evaluate("(e) => e.value")
        ok("app loads once it responds; history + invoice restored",
           pg.url.endswith("/chat") and elapsed >= DELAY_S and picked.startswith("NOS-2026-0142"),
           f"{elapsed:.1f}s, {picked[:13]}")  # fmt: skip
        ok("no page errors", not errors, "; ".join(errors[:2]))
        await br.close()
    print(f"RESULT: {sum(results)}/{len(results)} passed")


asyncio.run(main())
