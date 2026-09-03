import pytest
from playwright.async_api import async_playwright

from qaura.detectors.performance import (
    MIN_MEANINGFUL_BASELINE_BYTES,
    check_heap_growth,
    detect,
    read_heap_size,
    setup,
)

# Chromium's Long Tasks API only attributes tasks to real page-owned script execution
# (e.g. a <script> tag or event handler) — a busy-wait run via a bare
# page.evaluate()/CDP Runtime.evaluate call is NOT counted as a long task, confirmed
# by direct experiment (see the session's debug scripts). Injecting a <script> tag
# that itself does the busy-wait is what actually gets attributed.
_TRIGGER_LONG_TASK_JS = """
() => {
  const s = document.createElement('script');
  s.textContent = "const st=Date.now(); while(Date.now()-st<300){}";
  document.body.appendChild(s);
}
"""


@pytest.fixture
async def page():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        pg = await browser.new_page()
        await setup(pg)  # must run before goto -- see module docstring
        # about:blank doesn't reliably get an init script's fresh execution context;
        # a data: URL does, and doesn't need a real HTTP server.
        await pg.goto("data:text/html,<html><body>page</body></html>")
        yield pg
        await browser.close()


async def test_detects_long_task(page):
    await page.evaluate(_TRIGGER_LONG_TASK_JS)
    await page.wait_for_timeout(300)
    findings = await detect(page, page.url, [], "heuristic")
    assert any("Long task" in f.title for f in findings)
    assert all(f.detector == "performance" for f in findings)


async def test_no_long_task_finding_on_quiet_page(page):
    await page.wait_for_timeout(100)
    findings = await detect(page, page.url, [], "heuristic")
    assert not any("Long task" in f.title for f in findings)


async def test_detect_clears_buffer_between_calls(page):
    await page.evaluate(_TRIGGER_LONG_TASK_JS)
    await page.wait_for_timeout(300)
    first = await detect(page, page.url, [], "heuristic")
    assert any("Long task" in f.title for f in first)

    second = await detect(page, page.url, [], "heuristic")
    assert not any("Long task" in f.title for f in second)  # already reported and cleared


async def test_long_task_threshold_is_configurable(page):
    await page.evaluate(_TRIGGER_LONG_TASK_JS)
    await page.wait_for_timeout(300)
    findings = await detect(page, page.url, [], "heuristic", long_task_threshold_ms=10_000)
    assert not any("Long task" in f.title for f in findings)  # ~300ms task, 10s threshold


async def test_read_heap_size_returns_int_or_none(page):
    value = await read_heap_size(page)
    assert value is None or (isinstance(value, int) and value >= 0)


# check_heap_growth is a pure function — read_heap_size() itself was already
# implemented and tested, but nothing ever called it; this is the session-level
# counterpart that finally makes it inform a Finding (see the crawl loops in
# core/heuristic.py / core/orchestrator.py).
def test_check_heap_growth_flags_sustained_growth():
    baseline = MIN_MEANINGFUL_BASELINE_BYTES
    current = baseline * 3
    finding = check_heap_growth(baseline, current, "http://x/", "heuristic")
    assert finding is not None
    assert finding.detector == "performance"
    assert "3.0x" in finding.title


def test_check_heap_growth_ignores_small_baseline_as_noise():
    # A baseline of a few KB doubling to a few more KB isn't a leak signal — it's
    # noise from comparing against a near-empty heap.
    finding = check_heap_growth(1024, 1024 * 100, "http://x/", "heuristic")
    assert finding is None


def test_check_heap_growth_ignores_growth_below_threshold():
    baseline = MIN_MEANINGFUL_BASELINE_BYTES
    finding = check_heap_growth(baseline, int(baseline * 1.2), "http://x/", "heuristic")
    assert finding is None


def test_check_heap_growth_handles_missing_readings():
    assert check_heap_growth(None, 12345, "http://x/", "heuristic") is None
    assert check_heap_growth(12345, None, "http://x/", "heuristic") is None
    assert check_heap_growth(None, None, "http://x/", "heuristic") is None
