"""Browser check: after "Run scenario", the page scrolls to "2 · Processing result" — once.

Manual (not collected by pytest). Needs a local app with the demo on and a per-session demo
allowance of 10, so the 11th run is refused (to check that a failed run does not scroll):

    DEMO_MODE_ENABLED=true DEMO_SESSION_DAILY_LIMIT=10 \\
      uv run uvicorn invoiceiq.api.main:app_factory --factory --port 8010
    INVOICEIQ_API_URL=http://127.0.0.1:8010 \\
      uv run streamlit run src/invoiceiq/frontend/app.py --server.port 8511
    DEMO_BASE=http://127.0.0.1:8511 \\
      uv run --no-project --with playwright python tests/browser/demo_autoscroll_e2e.py

Checks, at 1440 px and 390 px: every one of the ten scenarios scrolls the result heading fully
into view (below Streamlit's fixed header); a second scenario scrolls again; feedback appears in
the clicked card; navigating away and back (a rerun with the same token) does not scroll; a
refused run does not scroll; simulated and historical panels stay separate; no page errors.
"""

import asyncio
import os

from playwright.async_api import Page, async_playwright

BASE = os.getenv("DEMO_BASE", "http://127.0.0.1:8511")
HEADING = "2 · Processing result"
SCENARIOS = [
    "demo-clean",
    "demo-approved-manager",
    "demo-approval-missing",
    "demo-insufficient-authority",
    "demo-vat-error",
    "demo-line-error",
    "demo-nonstandard-vat",
    "demo-foreign-supplier",
    "demo-foreign-currency",
    "demo-missing-fields",
]
results: list[bool] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)


async def heading_box(pg: Page) -> dict | None:
    loc = pg.get_by_text(HEADING, exact=True)
    return await loc.first.bounding_box() if await loc.count() else None


async def header_bottom(pg: Page) -> float:
    return await pg.evaluate(
        "() => { const h = document.querySelector('header[data-testid=\"stHeader\"]');"
        " return h ? h.getBoundingClientRect().bottom : 0; }"
    )


async def heading_fully_visible(pg: Page, height: int) -> tuple[bool, str]:
    box = await heading_box(pg)
    top_limit = await header_bottom(pg)
    if not box:
        return False, "no heading"
    visible = box["y"] >= top_limit - 1 and box["y"] + box["height"] <= height
    return visible, f"heading y={box['y']:.0f}, header bottom={top_limit:.0f}"


async def open_demo(pg: Page, width: int) -> None:
    await pg.goto(BASE, timeout=90000)
    await pg.get_by_text("InvoiceIQ — AI Finance Operations Copilot").first.wait_for(timeout=90000)
    await pg.get_by_role("button", name="Try InvoiceIQ").click()
    await pg.get_by_text("Try invoice processing").first.wait_for(timeout=30000)
    await pg.locator(".st-key-run_demo-clean button").wait_for(timeout=30000)
    await pg.wait_for_timeout(800)


async def run(pg: Page, scenario: str) -> None:
    btn = pg.locator(f".st-key-run_{scenario} button")
    await btn.scroll_into_view_if_needed()
    await pg.wait_for_timeout(300)
    box = await heading_box(pg)
    await btn.click()
    return box


async def nav(pg: Page, label: str, width: int) -> None:
    link = pg.locator('[data-testid="stSidebar"]').get_by_text(label, exact=True).first
    sidebar = await pg.locator('[data-testid="stSidebar"]').bounding_box()
    if not sidebar or sidebar["x"] + sidebar["width"] <= 0:  # collapsed (off-screen) on phones
        await pg.locator(
            '[data-testid="stExpandSidebarButton"], [data-testid="stSidebarCollapsedControl"] button'
        ).first.click()
        await pg.wait_for_timeout(500)
    await link.click()


async def check_width(br, width: int, height: int, scenarios: list[str], refused: bool) -> None:
    pg = await br.new_page(viewport={"width": width, "height": height})
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    await open_demo(pg, width)
    tag = f"[{width}px]"

    for i, sid in enumerate(scenarios):
        before = await run(pg, sid)
        await pg.get_by_text(HEADING, exact=True).first.wait_for(timeout=30000)
        await pg.wait_for_timeout(1500)  # smooth scroll + the second pass
        visible, detail = await heading_fully_visible(pg, height)
        after = await heading_box(pg)
        aligned = after is not None and after["y"] - await header_bottom(pg) < 80
        moved = "first result" if before is None else f"moved {before['y'] - after['y']:.0f}px"
        ok(
            f"{tag} {sid}: result heading scrolled fully into view, just below the header",
            visible and aligned,
            f"{detail}; {moved}",
        )
        card = pg.locator(f".st-key-run_{sid}").locator(
            "xpath=ancestor::div[@data-testid='stVerticalBlock'][1]"
        )
        ok(
            f"{tag} {sid}: feedback shown in the clicked card",
            await card.get_by_text("Scenario processed — showing results below.").count() == 1,
        )
        if i == 0:
            body = await pg.inner_text("body")
            ok(
                f"{tag} simulated and historical panels stay separate",
                "Simulated in this container" in body
                and "Verified in Azure — historical test run, not live" in body,
            )

    # An ordinary rerun must not scroll: go back up, then navigate away and back (the page script
    # runs again with the same token).
    await pg.locator(".st-key-run_demo-clean button").scroll_into_view_if_needed()
    await pg.wait_for_timeout(500)
    await nav(pg, "Dashboard", width)
    await pg.get_by_text("Sample invoices").first.wait_for(timeout=30000)
    await nav(pg, "Try processing", width)
    await pg.get_by_text("Try invoice processing").first.wait_for(timeout=30000)
    await pg.wait_for_timeout(2000)
    box = await heading_box(pg)
    ok(
        f"{tag} navigating back to the page does not scroll",
        box is not None and box["y"] > height,
        f"heading y={box['y']:.0f}" if box else "no heading",
    )

    if refused:
        await pg.locator(".st-key-run_demo-clean button").scroll_into_view_if_needed()
        await pg.wait_for_timeout(500)
        before = await heading_box(pg)
        await pg.locator(".st-key-run_demo-clean button").click()
        await pg.get_by_text("daily allowance").first.wait_for(timeout=30000)
        await pg.wait_for_timeout(1500)
        after = await heading_box(pg)
        ok(
            f"{tag} a refused run shows an error and does not scroll",
            before and after and abs(after["y"] - before["y"]) < 5,
            f"heading y {before['y']:.0f} → {after['y']:.0f}" if before and after else "",
        )

    ok(f"{tag} no JavaScript errors", not errors, "; ".join(errors[:3]))
    await pg.close()


async def main() -> None:
    async with async_playwright() as p:
        br = await p.chromium.launch(channel="chrome", headless=True)
        await check_width(br, 1440, 900, SCENARIOS, refused=True)
        await check_width(br, 390, 844, ["demo-vat-error", "demo-clean"], refused=False)
        await br.close()
    print(f"{sum(results)}/{len(results)} passed")
    raise SystemExit(0 if all(results) else 1)


asyncio.run(main())
