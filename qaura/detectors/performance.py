"""Performance detector — lowest priority of the five Phase 4 detectors per the
plan's own file ordering. Long tasks and CLS are fully implemented; memory growth is
a documented partial (see `read_heap_size` below) rather than force-fit into this
module's per-action pattern.

Long tasks and layout shifts are captured via PerformanceObserver, registered through
`setup()` as a page init script — it MUST run before the first navigation (`buffered:
true` only backfills entries from page load if the observer already existed when they
happened), so callers add it right before `page.goto()`, not after.
"""
from __future__ import annotations

from playwright.async_api import Page

from qaura.reporting.models import Evidence, Finding, ReproStep, Severity

_INIT_SCRIPT = """
window.__qaura_perf = { longtasks: [], clsTotal: 0 };
try {
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      window.__qaura_perf.longtasks.push(entry.duration);
    }
  }).observe({ type: 'longtask', buffered: true });
} catch (e) {}
try {
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      if (!entry.hadRecentInput) window.__qaura_perf.clsTotal += entry.value;
    }
  }).observe({ type: 'layout-shift', buffered: true });
} catch (e) {}
"""

DEFAULT_LONG_TASK_THRESHOLD_MS = 200
DEFAULT_CLS_THRESHOLD = 0.25  # Google's "poor" CLS boundary


async def setup(page: Page) -> None:
    """Call once per page, before the first navigation."""
    await page.add_init_script(_INIT_SCRIPT)


async def detect(
    page: Page,
    url: str,
    repro_steps: list[ReproStep],
    persona: str = "heuristic",
    long_task_threshold_ms: float = DEFAULT_LONG_TASK_THRESHOLD_MS,
    cls_threshold: float = DEFAULT_CLS_THRESHOLD,
) -> list[Finding]:
    """Reads accumulated PerformanceObserver data and clears it, so each call reports
    only what happened since the last call — same windowing pattern as
    Recorder.clear() for console/network."""
    try:
        data = await page.evaluate("() => window.__qaura_perf || { longtasks: [], clsTotal: 0 }")
    except Exception:
        return []  # e.g. page navigated away mid-read — non-fatal, just skip this window

    findings: list[Finding] = []
    longtasks = data.get("longtasks", [])
    slow = [t for t in longtasks if t >= long_task_threshold_ms]
    if slow:
        findings.append(Finding(
            title=f"Long task(s) blocking the main thread ({len(slow)} >= {long_task_threshold_ms:.0f}ms)",
            detector="performance",
            severity=Severity.MEDIUM,
            persona=persona,
            url=url,
            description=(
                f"{len(slow)} task(s) blocked the main thread for {long_task_threshold_ms:.0f}ms or "
                f"longer (durations: {[round(t) for t in slow]}). This can make the page feel "
                f"unresponsive — clicks and keystrokes queue up until the task finishes."
            ),
            repro_steps=list(repro_steps),
            evidence=Evidence(),
        ))

    cls = data.get("clsTotal", 0)
    if cls >= cls_threshold:
        findings.append(Finding(
            title=f"High cumulative layout shift ({cls:.2f})",
            detector="performance",
            severity=Severity.LOW,
            persona=persona,
            url=url,
            description=(
                f"Cumulative Layout Shift reached {cls:.2f} (Google's 'poor' threshold is "
                f"{cls_threshold}) — content is visibly jumping around as the page loads or "
                f"updates, which can cause misclicks."
            ),
            repro_steps=list(repro_steps),
            evidence=Evidence(),
        ))

    try:
        await page.evaluate("() => { window.__qaura_perf = { longtasks: [], clsTotal: 0 }; }")
    except Exception:
        pass

    return findings


async def read_heap_size(page: Page) -> int | None:
    """Returns `performance.memory.usedJSHeapSize` in bytes (Chromium-only, and
    coarse-grained by design for privacy) or None where unavailable. NOT wired into
    `detect()` — a meaningful memory-growth finding needs a before/after comparison
    across a real user journey (e.g. "open and close this modal 10 times"), which is
    a different shape of check than every other detector in this module (all of
    which report on a single window since the last call). Exposed as a building block
    for whoever writes that journey-level check later, rather than forced into this
    detector's per-action pattern just to claim full coverage of the plan's five-item
    list — a partial, honestly-labeled implementation beats a wired-but-meaningless one.
    """
    try:
        value = await page.evaluate("() => (performance.memory && performance.memory.usedJSHeapSize) || null")
        return int(value) if value is not None else None
    except Exception:
        return None


# Below this, a session's baseline reading is treated as noise, not a real starting
# point — comparing against a near-empty heap makes almost any growth ratio look
# alarming despite being a handful of KB in absolute terms.
MIN_MEANINGFUL_BASELINE_BYTES = 5 * 1024 * 1024  # 5 MB
DEFAULT_HEAP_GROWTH_RATIO = 2.0  # heap has DOUBLED since the crawl/persona-run started


def check_heap_growth(
    baseline_bytes: int | None,
    current_bytes: int | None,
    url: str,
    persona: str = "heuristic",
    threshold_ratio: float = DEFAULT_HEAP_GROWTH_RATIO,
) -> Finding | None:
    """The session-level counterpart to read_heap_size()'s per-call reading — call
    once per crawl/persona run, comparing the FIRST reading (taken right after the
    initial goto) against the LAST (taken when the run ends), not read_heap_size()
    wired into detect()'s per-action window. A single window's heap size in
    isolation says nothing about a leak; sustained growth across an entire session
    that never comes back down is the actual signal, and that requires exactly two
    points — start and end — not a reading after every action."""
    if baseline_bytes is None or current_bytes is None:
        return None  # performance.memory unavailable (non-Chromium) or read failed
    if baseline_bytes < MIN_MEANINGFUL_BASELINE_BYTES:
        return None
    if current_bytes < baseline_bytes * threshold_ratio:
        return None

    return Finding(
        title=f"JS heap grew {current_bytes / baseline_bytes:.1f}x over the session",
        detector="performance",
        severity=Severity.LOW,
        persona=persona,
        url=url,
        description=(
            f"performance.memory.usedJSHeapSize went from {baseline_bytes / 1_048_576:.1f}MB at "
            f"the start of this session to {current_bytes / 1_048_576:.1f}MB at the end — a "
            f"{current_bytes / baseline_bytes:.1f}x increase, above the {threshold_ratio}x "
            f"threshold. This is a coarse, single-session signal (not a controlled before/after "
            f"of one specific journey) and can have an innocent explanation — more exploration "
            f"means more page state — but a large, one-directional growth across a whole run is "
            f"worth checking for a leak (event listeners or DOM nodes not released, a cache with "
            f"no eviction, etc.)."
        ),
        evidence=Evidence(),
    )
