"""LLM-driven persona loop — the observe/decide/act/detect cycle from the plan's
architecture, using core/planner.py for the "decide" step instead of
core/heuristic.py's deterministic picker. Structurally mirrors heuristic.py's crawl
loop deliberately (same StateGraph/RunLimiter/Recorder/detector wiring) rather than
factoring out a shared base — heuristic.py needed to work and ship in Phase 2 without
any LLM entanglement, and duplicating ~40 lines of loop structure here is a smaller
risk than a premature shared abstraction between two loops with genuinely different
failure modes (a stale ref is a bug in Phase 2; in Phase 3 it can also mean the model
hallucinated one, which needs its own handling).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Page

from qaura.browser.actions import Action, ActionError, ActionKind, execute
from qaura.browser.observe import build_page_model
from qaura.browser.recorder import Recorder
from qaura.config import QAuraConfig
from qaura.core.guardrails import (
    BudgetStop,
    GuardrailViolation,
    RunLimiter,
    check_action,
    enforce_scope_after_action,
    guard_goto,
    sanitize_fill_value,
)
from qaura.core import invariants as invariants_engine
from qaura.core.planner import PlannerError, plan_next_action
from qaura.core.state import StateGraph, fingerprint
from qaura.detectors import a11y as a11y_detector
from qaura.detectors import console as console_detector
from qaura.detectors import crash as crash_detector
from qaura.detectors import flow as flow_detector
from qaura.detectors import network as network_detector
from qaura.detectors import performance as performance_detector
from qaura.detectors import security as security_detector
from qaura.detectors import visual as visual_detector
from qaura.llm.base import LLMProvider
from qaura.llm.budget import Budget, BudgetExceeded
from qaura.personas.base import Persona
from qaura.reporting.models import Evidence, Finding, ReproStep, Severity

_log = logging.getLogger(__name__)

# A planner that keeps hallucinating refs or unparseable output isn't going to
# self-correct — better to end this persona's run cleanly (findings so far are still
# valid) than loop until the wall-clock cap silently eats the whole budget on a
# broken model/prompt combination.
MAX_CONSECUTIVE_PLANNER_ERRORS = 5


@dataclass
class CrawlResult:
    findings: list[Finding]
    graph: StateGraph
    actions_taken: int
    llm_usage: dict


def _describe(action: Action, ref: str, role: str, name: str) -> str:
    value_part = f" = {action.value!r}" if action.value is not None else ""
    return f"{action.kind.value} {ref} {role} {name!r}{value_part}"


class PersonaOrchestrator:
    def __init__(
        self, target_url: str, cfg: QAuraConfig, provider: LLMProvider, persona: Persona,
        screenshot_dir: Path | None = None,
        limiter: RunLimiter | None = None, budget: Budget | None = None,
    ) -> None:
        self.target_url = target_url
        self.cfg = cfg
        self.provider = provider
        self.persona = persona
        self.graph = StateGraph()
        # Caller-supplied limiter/budget let cli.py share one instance across every
        # persona in a run, per guardrails.py's documented run-wide-caps contract.
        # Defaulting to fresh instances here keeps direct/test construction of a lone
        # orchestrator working exactly as before.
        self.limiter = limiter if limiter is not None else RunLimiter(cfg=cfg.guardrails)
        self.budget = budget if budget is not None else Budget(max_calls=cfg.guardrails.max_llm_calls_per_run)
        self.findings: list[Finding] = []
        self._session_id: str | None = None
        self._consecutive_planner_errors = 0
        # Full action history — see heuristic.py's identical field for why: a Finding
        # needs the WHOLE sequence to be replayable, not just the triggering step.
        self._action_history: list[ReproStep] = []
        # See heuristic.py's identical field/docstring note — screenshots were never
        # actually being captured anywhere despite Recorder.screenshot() existing
        # since Phase 1 and being an explicit deliverable from the original brief.
        self.screenshot_dir = screenshot_dir
        self._screenshot_counter = 0

    def _collect_from_recorder(self, recorder: Recorder, url: str, repro_steps: list[ReproStep]) -> None:
        self.findings.extend(crash_detector.detect(recorder, url, repro_steps, self.persona.name))
        self.findings.extend(console_detector.detect(recorder, url, repro_steps, self.persona.name))
        self.findings.extend(network_detector.detect(recorder, url, repro_steps, self.persona.name))
        recorder.clear()

    async def _check_invariants(self, page: Page, url: str, repro_steps: list[ReproStep]) -> None:
        for inv in self.cfg.invariants:
            finding = await invariants_engine.check(page, inv, url, repro_steps, self.persona.name)
            if finding:
                self.findings.append(finding)

    async def _attach_screenshot(self, page: Page, before_count: int) -> None:
        if self.screenshot_dir is None or len(self.findings) <= before_count:
            return
        self._screenshot_counter += 1
        path = self.screenshot_dir / f"{self._screenshot_counter:04d}.png"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(path), full_page=True)
        except Exception:
            return
        for f in self.findings[before_count:]:
            f.evidence.screenshot_path = str(path)

    async def _check_per_action(self, page: Page, url: str, repro_steps: list[ReproStep]) -> None:
        self.findings.extend(await security_detector.detect_reflected_injection(page, url, repro_steps, self.persona.name))
        self.findings.extend(await performance_detector.detect(page, url, repro_steps, self.persona.name))

    async def _check_per_state(self, page: Page, url: str, repro_steps: list[ReproStep]) -> None:
        """Only called on a state's first visit — see heuristic.py's identical method
        for why (visual/a11y are properties of the rendered state, not the action)."""
        self.findings.extend(await visual_detector.detect(page, url, repro_steps, self.persona.name))
        self.findings.extend(await a11y_detector.detect(page, url, repro_steps, self.persona.name))

    async def run(self, page: Page, recorder: Recorder) -> CrawlResult:
        await performance_detector.setup(page)  # must run before the first goto
        await guard_goto(page, self.target_url, self.cfg.guardrails)
        baseline_heap = await performance_detector.read_heap_size(page)

        while True:
            try:
                self.limiter.check_before_action()
                self.budget.check()
            except (BudgetStop, BudgetExceeded):
                break

            model = await build_page_model(page)
            node = self.graph.visit(model)
            state_key = node.fingerprint.key()
            if node.visit_count == 1:
                before = len(self.findings)
                await self._check_per_state(page, page.url, list(self._action_history))
                await self._attach_screenshot(page, before)

            try:
                action, planned, new_session = plan_next_action(
                    self.provider, self.persona.system_prompt, model, node.exercised_refs,
                    session=self._session_id, budget=self.budget,
                )
            except PlannerError as e:
                self._consecutive_planner_errors += 1
                _log.debug("planner returned something unusable: %s", e)
                if self._consecutive_planner_errors >= MAX_CONSECUTIVE_PLANNER_ERRORS:
                    break
                continue
            except Exception:
                # provider.complete() (inside plan_next_action) has no timeout/retry
                # beyond gemini.py's own 429 handling — this used to be completely
                # unguarded, so one transient network error ended the persona's run
                # outright rather than degrading the same way a bad model response
                # already does. Treated identically: count it, give up only after
                # MAX_CONSECUTIVE_PLANNER_ERRORS in a row.
                self._consecutive_planner_errors += 1
                _log.debug("planner call failed", exc_info=True)
                if self._consecutive_planner_errors >= MAX_CONSECUTIVE_PLANNER_ERRORS:
                    break
                continue
            self._consecutive_planner_errors = 0
            self._session_id = new_session

            element = model.find(action.ref)  # planner.py already guarantees this resolves
            if action.kind == ActionKind.FILL and action.value is not None:
                action = Action(
                    kind=action.kind, ref=action.ref,
                    value=sanitize_fill_value(str(action.value), element, self.cfg.guardrails),
                    expectation=action.expectation,
                )

            try:
                check_action(action, model, self.cfg.guardrails, persona=self.persona)
            except GuardrailViolation:
                self.graph.mark_exercised(state_key, action.ref, element=element)
                continue

            self.limiter.throttle()
            description = _describe(action, element.ref, element.role, element.name)
            repro_step = ReproStep(
                description=description, action_kind=action.kind.value, ref=action.ref,
                value=str(action.value) if action.value is not None else None, url_before=page.url,
            )
            self._action_history.append(repro_step)
            history = list(self._action_history)  # snapshot for this step's findings
            before = len(self.findings)

            try:
                await execute(page, model, action)
            except ActionError:
                self.graph.mark_exercised(state_key, action.ref, element=element)
                continue
            except Exception as e:
                self.findings.append(Finding(
                    title=f"Action raised an exception: {description}",
                    detector="crash", severity=Severity.HIGH, persona=self.persona.name,
                    url=page.url, description=f"Executing '{description}' raised: {e}",
                    repro_steps=history, evidence=Evidence(page_error=str(e)),
                ))
                await self._attach_screenshot(page, before)
                self.graph.mark_exercised(state_key, action.ref, element=element)
                self.limiter.record_action()
                continue

            self.limiter.record_action()
            self.graph.mark_exercised(state_key, action.ref, element=element)
            await page.wait_for_timeout(300)  # let network/console settle, and give the
                                                # divergence check a stable state to judge

            # See heuristic.py's identical call for why: check_action() only validates
            # NAVIGATE actions, so a CLICK that follows an <a href> off-target is
            # otherwise never caught.
            await enforce_scope_after_action(page, self.cfg.guardrails)

            try:
                new_model = await build_page_model(page)
                rebuild_failed = False
            except Exception:
                # Previously fell back to `new_model = model` — the SAME object as
                # the pre-action model. That fed a bogus self-edge into the state
                # graph (this state "transitions to itself" on every action where the
                # rebuild happens to fail) and, worse, guaranteed the flow-divergence
                # check below sees zero differences and reports "no change" as
                # settled fact rather than "we don't actually know" — silently
                # capable of masking a real expectation violation. Skip both instead.
                _log.debug("page-model rebuild failed after action %r", description, exc_info=True)
                new_model = model
                rebuild_failed = True

            if not rebuild_failed:
                self.graph.record_action(state_key, fingerprint(new_model).key(), description)
            before = len(self.findings)
            self._collect_from_recorder(recorder, page.url, history)
            await self._check_invariants(page, page.url, history)
            await self._check_per_action(page, page.url, history)

            try:
                self.budget.check()
            except BudgetExceeded:
                await self._attach_screenshot(page, before)
                break  # don't spend one more call on a divergence check we can't afford

            if not rebuild_failed:
                try:
                    divergence = flow_detector.check(
                        self.provider, action.expectation, model, new_model, description,
                        history, self.persona.name, budget=self.budget,
                    )
                except Exception:
                    # flow_detector.check() calls provider.complete() directly, with
                    # no timeout/retry beyond gemini.py's 429 handling — completely
                    # unguarded here meant one transient network error mid-loop ended
                    # the whole persona run instead of just skipping this one
                    # divergence check.
                    _log.debug("flow divergence check failed", exc_info=True)
                    divergence = None
                if divergence:
                    self.findings.append(divergence)
            await self._attach_screenshot(page, before)

        current_heap = await performance_detector.read_heap_size(page)
        growth_finding = performance_detector.check_heap_growth(
            baseline_heap, current_heap, page.url, self.persona.name,
        )
        if growth_finding:
            self.findings.append(growth_finding)

        return CrawlResult(self.findings, self.graph, self.limiter.actions_taken, self.budget.summary())
