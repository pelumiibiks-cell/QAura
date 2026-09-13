"""Guardrails execute in code, never as prompt text — plan design decision #5.
Nothing in here trusts the LLM (or the heuristic crawler) to stay in scope on its
own. Two enforcement points, not one: check_action() validates NAVIGATE actions and
destructive patterns before an action runs, but a CLICK has no URL to check ahead of
time — if it follows an <a href> off-target, that's only catchable after the fact, via
enforce_scope_after_action(). Every actual page.goto() in the codebase is expected to
go through guard_goto() rather than calling Playwright directly, so allowed_domains
means something.
"""
from __future__ import annotations

import asyncio
import fnmatch
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from playwright.async_api import Page, Response

from qaura.browser.actions import Action, ActionKind
from qaura.browser.observe import ElementInfo, PageModel
from qaura.config import GuardrailConfig
from qaura.core.forms import group_forms

if TYPE_CHECKING:
    # Deferred to avoid a runtime import cycle (personas -> ... -> guardrails isn't
    # actually a cycle today, but there's no reason for this module to need the
    # personas package at import time just for a type hint).
    from qaura.personas.base import Persona


class GuardrailViolation(RuntimeError):
    def __init__(self, reason: str, action: Action | None = None) -> None:
        self.reason = reason
        self.action = action
        super().__init__(reason)


class BudgetStop(RuntimeError):
    """Distinct from GuardrailViolation: this isn't 'that action was unsafe', it's
    'the run has done enough / spent enough, stop cleanly now'. The orchestrator
    catches this to flush a partial report rather than crashing."""


@dataclass
class RunLimiter:
    """Tracks the run-wide caps from GuardrailConfig: action count, wall clock, and
    a simple token-bucket-ish request rate limiter. One instance per run, shared
    across all personas/contexts in that run since the caps are run-wide, not
    per-persona (a run with 5 personas shouldn't get 5x the action budget)."""

    cfg: GuardrailConfig
    actions_taken: int = 0
    _start_time: float = field(default_factory=time.monotonic)
    _last_action_time: float | None = None

    def check_before_action(self) -> None:
        elapsed = time.monotonic() - self._start_time
        if elapsed > self.cfg.max_wall_clock_seconds:
            raise BudgetStop(f"wall clock cap hit: {elapsed:.0f}s > {self.cfg.max_wall_clock_seconds}s")
        if self.actions_taken >= self.cfg.max_actions_per_run:
            raise BudgetStop(f"action cap hit: {self.actions_taken} >= {self.cfg.max_actions_per_run}")

    async def throttle(self) -> float:
        """Sleeps just long enough to respect max_requests_per_second, if needed.
        Returns the number of seconds actually slept, so a caller/test can assert on
        it without needing to wall-clock the whole call. Awaits rather than blocking
        the event loop."""
        if self.cfg.max_requests_per_second <= 0:
            return 0.0
        min_interval = 1.0 / self.cfg.max_requests_per_second
        now = time.monotonic()
        slept = 0.0
        if self._last_action_time is not None:
            wait = min_interval - (now - self._last_action_time)
            if wait > 0:
                await asyncio.sleep(wait)
                slept = wait
        self._last_action_time = time.monotonic()
        return slept

    def record_action(self) -> None:
        self.actions_taken += 1


def is_in_scope(url: str, cfg: GuardrailConfig) -> bool:
    parts = urlsplit(url)
    # hostname is already lowercased and port-stripped by urlsplit, unlike netloc
    # (which is "host:port" — comparing that against allowed_domains like "localhost"
    # never matches once a port is present, silently defeating this whole check).
    host = parts.hostname
    domains = [d.lower() for d in cfg.allowed_domains]
    if domains and not (host is not None and any(host == d or host.endswith(f".{d}") for d in domains)):
        return False
    # Decoded and lowercased so /log%6Fut and /LOGOUT match the same patterns on every OS
    path = unquote(parts.path or "/").lower()
    if cfg.blocked_paths and any(fnmatch.fnmatchcase(path, pat.lower()) for pat in cfg.blocked_paths):
        return False
    if cfg.allowed_paths and not any(fnmatch.fnmatchcase(path, pat.lower()) for pat in cfg.allowed_paths):
        return False
    return True


async def guard_goto(page: Page, url: str, cfg: GuardrailConfig) -> Response | None:
    """The single choke point every navigation (crawl start, frontier jump, form
    re-anchor, cross-role probe, replay) must go through. Without this, `is_in_scope`
    is only reachable via ActionKind.NAVIGATE, which nothing in the codebase ever
    emits — every real navigation called page.goto() directly, so allowed_domains
    never actually constrained anything despite the module-level claim that "every
    action is checked here first".

    Returns Playwright's Response so a caller can read the HTTP status — `qaura init`'s
    recon needs it to tell a 401/403 wall from a normal page, which is not recoverable
    from the DOM alone. Playwright returns None for a same-document navigation (a
    fragment change), so the type is Optional. Every pre-existing caller ignores the
    return value."""
    if not is_in_scope(url, cfg):
        raise GuardrailViolation(f"navigation target out of scope: {url}")
    return await page.goto(url)


async def enforce_scope_after_action(page: Page, cfg: GuardrailConfig) -> bool:
    """Call after any action that might have navigated (a click on an <a href>, most
    commonly). A CLICK is only checked against destructive_patterns by check_action —
    it has no URL to validate before the fact — so this is what actually stops the
    crawler from following a link off-target and continuing to fuzz forms there under
    whatever storage_state (auth cookies) the run was given. Returns True if it had to
    step back, and raises GuardrailViolation if stepping back didn't return the page to
    scope, so the caller can re-anchor or stop instead of acting on the foreign page."""
    if is_in_scope(page.url, cfg):
        return False
    try:
        await page.go_back()
    except Exception:
        pass
    if not is_in_scope(page.url, cfg):
        raise GuardrailViolation(f"left scope and could not step back: {page.url}")
    return True


def is_destructive(element: ElementInfo, cfg: GuardrailConfig) -> bool:
    """Matches the element's accessible name (case-insensitive substring) against the
    configured destructive-pattern list. Deliberately over-inclusive — a false positive
    here just means a persona skips a safe-but-scary-sounding button (e.g. "Delete
    filter"); a false negative means a real destructive action runs unchecked, which is
    the worse failure mode."""
    name_lower = (element.name or "").lower()
    return any(pattern.lower() in name_lower for pattern in cfg.destructive_patterns)


def _submits_destructive_form(action: Action, element: ElementInfo, page_model: PageModel, cfg: GuardrailConfig) -> bool:
    """Enter in a form field submits the form, so it inherits the form's destructive buttons."""
    if action.kind != ActionKind.KEY or str(action.value or "").lower() != "enter" or not element.form_key:
        return False
    form = next((g for g in group_forms(page_model) if g.key == element.form_key), None)
    return form is not None and any(is_destructive(button, cfg) for button in form.buttons)


def check_action(
    action: Action, page_model: PageModel, cfg: GuardrailConfig, persona: "Persona | None" = None,
) -> None:
    """Raises GuardrailViolation if `action` should not be executed. Call this
    immediately before browser/actions.py:execute() — every single action, no
    exceptions, including ones the planner marked as safe.

    `persona` is an additional, optional restriction on top of `cfg.allow_destructive`
    — never a way to loosen it. Persona.attempts_destructive used to be set on every
    persona (all False, per Persona's own default) and asserted by a test, but never
    actually read anywhere: if a run set `allow_destructive: true`, every persona
    performed destructive actions equally, including ones with no business doing so
    (e.g. "accessibility" clicking a real delete button), because nothing consulted
    the per-persona flag. Passing `persona=None` (heuristic mode, which has no
    Persona objects) preserves the old cfg-only behavior exactly."""
    if action.kind == ActionKind.NAVIGATE:
        if action.value and not is_in_scope(str(action.value), cfg):
            raise GuardrailViolation(f"navigate target out of scope: {action.value}", action)
        return

    if action.ref is None:
        return  # BACK/FORWARD/RELOAD — always in-scope by construction, same page

    element = page_model.find(action.ref)
    if element is None:
        raise GuardrailViolation(f"ref {action.ref!r} not found in current PageModel", action)

    if is_destructive(element, cfg) or _submits_destructive_form(action, element, page_model, cfg):
        persona_allows = persona is None or persona.attempts_destructive
        if not cfg.allow_destructive:
            raise GuardrailViolation(
                f"destructive action blocked: {action.kind.value} on {element.role} {element.name!r} "
                f"(set guardrails.allow_destructive: true to permit)",
                action,
            )
        if not persona_allows:
            raise GuardrailViolation(
                f"destructive action blocked: {action.kind.value} on {element.role} {element.name!r} "
                f"(run allows destructive actions, but persona {persona.name!r} does not attempt them)",
                action,
            )


def sanitize_fill_value(value: str, element: ElementInfo, cfg: GuardrailConfig) -> str:
    """Substitutes real-looking payment/email values with known-safe test values before
    a FILL action reaches the page. Heuristic on the element's accessible name/role —
    conservative substring match, same false-positive-tolerant reasoning as
    is_destructive()."""
    name_lower = (element.name or "").lower()
    # Parenthesized deliberately: `and` binds tighter than `or`, so this reads as
    # "card OR credit OR (textbox AND cvc)" either way — spelling it out removes any
    # doubt in a payment-data sanitization branch, where the ambiguity is worth zero.
    if "card" in name_lower or "credit" in name_lower or (element.role == "textbox" and "cvc" in name_lower):
        if any(c.isdigit() for c in value) and len(value.replace(" ", "")) >= 12:
            return cfg.test_card_number
    if "email" in name_lower and "@" in value and not value.endswith(f"@{cfg.fake_email_domain}"):
        local = value.split("@", 1)[0]
        return f"{local}@{cfg.fake_email_domain}"
    return value
