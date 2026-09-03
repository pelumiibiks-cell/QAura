"""Action executors, keyed off a PageModel ref rather than a raw CSS selector — the
planner/heuristic crawler only ever sees refs (e12, ...), never has to construct or
reason about selectors itself. Every executor re-resolves the ref to a live Playwright
locator at call time rather than caching one, since the DOM may have changed between
observe() and act().
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from playwright.async_api import Page

from qaura.browser.observe import ElementInfo, PageModel


class ActionKind(str, Enum):
    CLICK = "click"
    DBLCLICK = "dblclick"
    FILL = "fill"
    SELECT = "select"
    CHECK = "check"
    UNCHECK = "uncheck"
    KEY = "key"           # e.g. "Enter", "Escape", "Tab"
    SCROLL_INTO_VIEW = "scroll_into_view"
    SET_FILES = "set_files"  # upload via input[type=file] — value is a path or list of paths
    NAVIGATE = "navigate"  # goto an absolute/relative URL, not tied to a ref
    BACK = "back"
    FORWARD = "forward"
    RELOAD = "reload"


@dataclass
class Action:
    kind: ActionKind
    ref: str | None = None     # required for all element-targeted kinds
    value: Any = None          # fill text, select option, key name, or navigate URL
    expectation: str | None = None  # plan decision #3: what should be true afterward —
                                      # populated by the planner (Phase 3); the heuristic
                                      # crawler (Phase 2) leaves this None and relies on
                                      # passive detectors instead of divergence checking


class ActionError(RuntimeError):
    """Raised when a ref no longer resolves, or resolves ambiguously. Distinct from a
    Playwright timeout/crash — this is 'the plan was stale', which is itself a useful
    signal (the orchestrator should re-observe and let the planner retry), not a bug."""


def _locator_for(page: Page, page_model: PageModel, ref: str):
    element: ElementInfo | None = page_model.find(ref)
    if element is None:
        raise ActionError(f"ref {ref!r} not found in current PageModel — page likely changed")
    # Always index via .nth(element.index) rather than assuming a unique match —
    # `.nth(0)` on a locator with exactly one match behaves identically to no
    # `.nth()` at all, so this is strictly more correct with no downside for the
    # common non-duplicate case. Without it, any element sharing an accessible name
    # (duplicate "Delete" buttons, several unlabeled textboxes) raised a Playwright
    # strict-mode violation that surfaced as a bogus "crash" finding instead of
    # actually being addressed (see observe.py's grouped enrichment, Phase A).
    locator = page.get_by_role(element.role, name=element.name, exact=True).nth(element.index)
    return locator, element


async def execute(page: Page, page_model: PageModel, action: Action, timeout_ms: int = 5000) -> None:
    """Executes one action. Does NOT re-observe afterward — that's the orchestrator's
    job (Phase 2/3), since it needs to decide things like "wait for network idle" or
    "wait a fixed beat for the impatient persona" first, and that policy doesn't belong
    in this low-level executor.
    """
    if action.kind == ActionKind.NAVIGATE:
        if not action.value:
            raise ActionError("NAVIGATE action requires `value` (a URL)")
        await page.goto(str(action.value), timeout=timeout_ms)
        return
    if action.kind == ActionKind.BACK:
        await page.go_back(timeout=timeout_ms)
        return
    if action.kind == ActionKind.FORWARD:
        await page.go_forward(timeout=timeout_ms)
        return
    if action.kind == ActionKind.RELOAD:
        await page.reload(timeout=timeout_ms)
        return

    if action.ref is None:
        raise ActionError(f"{action.kind.value} action requires `ref`")
    locator, element = _locator_for(page, page_model, action.ref)

    if action.kind == ActionKind.CLICK:
        await locator.click(timeout=timeout_ms)
    elif action.kind == ActionKind.DBLCLICK:
        await locator.dblclick(timeout=timeout_ms)
    elif action.kind == ActionKind.FILL:
        await locator.fill(str(action.value or ""), timeout=timeout_ms)
    elif action.kind == ActionKind.SELECT:
        await locator.select_option(str(action.value), timeout=timeout_ms)
    elif action.kind == ActionKind.CHECK:
        await locator.check(timeout=timeout_ms)
    elif action.kind == ActionKind.UNCHECK:
        await locator.uncheck(timeout=timeout_ms)
    elif action.kind == ActionKind.KEY:
        await locator.press(str(action.value), timeout=timeout_ms)
    elif action.kind == ActionKind.SCROLL_INTO_VIEW:
        await locator.scroll_into_view_if_needed(timeout=timeout_ms)
    elif action.kind == ActionKind.SET_FILES:
        await locator.set_input_files(str(action.value), timeout=timeout_ms)
    else:
        raise ActionError(f"Unhandled action kind: {action.kind}")
