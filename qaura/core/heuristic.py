"""BFS-ish heuristic crawler: no LLM, no planner. Always returns to the start URL when
a branch runs out of unexercised elements, rather than attempting real backtracking
through the DOM — simpler, and the state graph still gets full coverage credit since
StateGraph collapses revisits to the same fingerprint.

Text fields get fuzzed with a bounded slice of core/inputs.py's corpus (not the whole
thing — that would blow the action budget on the first form) and a Tab press after each
fill, so blur/change-triggered validation gets exercised, not just the fill event itself.

Determinism (Phase 7): this module makes no `random`/`np.random` calls anywhere in the
crawl-decision path — element pick order is DOM order (`_pick_candidate`), fuzz value
order is `core/inputs.py:diverse_slice`'s fixed round-robin, and guardrail/rate-limit
logic involves no randomness. Confirmed EMPIRICALLY, not just by this code-reading
argument: two live runs against the same target with the same action budget produced
byte-identical finding sets (same titles, same detectors, same order — `Compare-Object`
diff was empty). `QAuraConfig.seed` is therefore currently unused and left that way —
there is nothing to seed yet. Wire it only once an actual randomness source is
introduced (e.g. randomized tie-breaking among equal-priority elements); adding
`random.seed()` calls now, with nothing downstream reading that seed, would be dead
code satisfying a need that doesn't exist. If persona (LLM) mode is ever expected to
replay identically too, that's a materially different problem — it depends on the
model's own sampling determinism, which isn't something this codebase controls.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Page

from qaura.browser.actions import Action, ActionKind, ActionError, execute
from qaura.browser.observe import ElementInfo, PageModel, build_page_model
from qaura.browser.recorder import Recorder
from qaura.config import QAuraConfig
from qaura.core.forms import FormGroup, group_forms
from qaura.core.guardrails import (
    BudgetStop,
    GuardrailViolation,
    RunLimiter,
    check_action,
    enforce_scope_after_action,
    guard_goto,
    sanitize_fill_value,
)
from qaura.core.inputs import diverse_slice, hostile_value_for, valid_value_for
from qaura.core import invariants as invariants_engine
from qaura.core.state import StateGraph, fingerprint, url_template
from qaura.detectors import a11y as a11y_detector
from qaura.detectors import console as console_detector
from qaura.detectors import crash as crash_detector
from qaura.detectors import network as network_detector
from qaura.detectors import performance as performance_detector
from qaura.detectors import security as security_detector
from qaura.detectors import visual as visual_detector
from qaura.reporting.models import Finding, ReproStep

# How many fuzz values to try per text field per visit. Small on purpose — every value
# tried is an action against the budget, and a field gets re-tried on a future visit
# to the same state if the run has budget left (mark-exercised only happens after the
# whole slice, so a short-budget run still gets at least the first value in everywhere
# before spending more actions on any one field).
FUZZ_SLICE_SIZE = 4

# How many fields to fuzz (one corrupted field per submission, rest valid) per form
# per state visit — bounded against the action budget the same way FUZZ_SLICE_SIZE is.
FORM_FUZZ_FIELD_LIMIT = 2

_MINIMAL_PDF_BYTES = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Size 4/Root 1 0 R>>\n"
    b"startxref\n0\n%%EOF"
)


_upload_sample_path_cache: str | None = None


def _sample_upload_path() -> str:
    """A tiny, cheap, syntactically-valid PDF for exercising file inputs — passes a
    magic-byte ('%PDF-') + extension check like the payroll app's own upload
    validator, without needing a PDF-generation dependency. Cached to one file on
    disk across the whole process, not regenerated per call.

    tempfile.mkdtemp() rather than a fixed shared name: the old fixed path
    (gettempdir()/"qaura_upload_samples"/"qaura_sample.pdf" with exist_ok=True) was
    predictable and, on POSIX, sits under a world-writable directory — another local
    user could pre-create the file (or a symlink at that path) and control what
    actually gets uploaded to the target app. mkdtemp() creates a uniquely-named,
    0700-permissioned directory no other user can pre-populate."""
    global _upload_sample_path_cache
    if _upload_sample_path_cache is None:
        tmp_dir = Path(tempfile.mkdtemp(prefix="qaura_upload_"))
        path = tmp_dir / "qaura_sample.pdf"
        path.write_bytes(_MINIMAL_PDF_BYTES)
        _upload_sample_path_cache = str(path)
    return _upload_sample_path_cache


@dataclass
class CrawlResult:
    findings: list[Finding]
    graph: StateGraph
    actions_taken: int


def _describe(action: Action, element: ElementInfo) -> str:
    value_part = f" = {action.value!r}" if action.value is not None else ""
    return f"{action.kind.value} {element.ref} {element.role} {element.name!r}{value_part}"


def _actions_for(element: ElementInfo) -> list[Action]:
    """One element may expand to several actions (a text field gets several fuzz
    values in sequence); everything else is a single action."""
    if (element.input_type or "").lower() == "file":
        # A file input surfaces as a "button" role in the ARIA snapshot; clicking it
        # opens a native OS file-chooser dialog Playwright can't interact with, and
        # the whole action just hangs until the executor's timeout. set_input_files
        # is the correct executor for it — see actions.py.
        return [Action(kind=ActionKind.SET_FILES, ref=element.ref, value=_sample_upload_path())]
    if element.role in ("textbox", "searchbox", "spinbutton"):
        values = diverse_slice(element.role, FUZZ_SLICE_SIZE)
        actions = []
        for v in values:
            actions.append(Action(kind=ActionKind.FILL, ref=element.ref, value=v.value))
            actions.append(Action(kind=ActionKind.KEY, ref=element.ref, value="Tab"))
        return actions
    if element.role in ("checkbox", "switch"):
        # Drive both transitions using the observed state, rather than always CHECK
        # (a no-op on an already-checked box, and the unchecking transition — which
        # can have its own bugs, e.g. a stale total after unchecking an item — was
        # never exercised at all before this).
        return [Action(kind=ActionKind.UNCHECK if element.checked else ActionKind.CHECK, ref=element.ref)]
    if element.role == "radio":
        return [Action(kind=ActionKind.CHECK, ref=element.ref)]
    if element.role in ("combobox", "listbox") and element.value is None:
        # SELECT needs a real option value; heuristic.py doesn't parse <option> lists
        # itself (that lives in actions.py/observe.py's domain), so fall through to a
        # click, which at least opens/toggles the control. Elements with a `value`
        # already observed are select-like widgets the LLM planner can target more
        # precisely; the deterministic crawler still gets partial coverage via click.
        return [Action(kind=ActionKind.CLICK, ref=element.ref)]
    # button, link, and everything else: a single click is the meaningful probe
    return [Action(kind=ActionKind.CLICK, ref=element.ref)]


class HeuristicCrawler:
    def __init__(self, target_url: str, cfg: QAuraConfig, screenshot_dir: Path | None = None) -> None:
        self.target_url = target_url
        self.cfg = cfg
        self.graph = StateGraph()
        self.limiter = RunLimiter(cfg=cfg.guardrails)
        self.findings: list[Finding] = []
        # Full action history for this crawl session, not just the most recent step —
        # a Finding needs the WHOLE sequence to be replayable (analysis/replay.py,
        # Phase 5). Found live: a bug that only manifests after two prior actions
        # (e.g. "apply coupon" then "change quantity") was being captured with
        # repro_steps=[just the quantity-change step], which alone reproduces nothing.
        self._action_history: list[ReproStep] = []
        # Phase 7 finding: Recorder.screenshot() existed since Phase 1 but nothing in
        # either crawl loop ever called it — every Finding's evidence.screenshot_path
        # was silently None despite screenshots being an explicit deliverable from the
        # original project brief. `screenshot_dir=None` (the default) preserves that
        # old no-screenshot behavior for callers/tests that don't need it; cli.py
        # passes a real directory for actual runs.
        self.screenshot_dir = screenshot_dir
        self._screenshot_counter = 0
        # Phase B: which form_keys have already had their fill+submit pass run, keyed
        # on URL TEMPLATE rather than the full state fingerprint. Found live against a
        # real search form: a GET-method search form's results change with the query,
        # so every submission (including QAura's own fuzz/valid submissions) produces
        # a DIFFERENT element signature and therefore a different state_key — keying
        # on state_key meant the form looked "unexercised" again after every single
        # submission, and the crawler looped on it until the action budget ran out,
        # never reaching anything past that one form. url_template already drops the
        # query string and normalizes id-like path segments (core/state.py), so
        # `/records?q=test` and `/records?q=🔥` collapse to the same key here even
        # though they're different STATES — which is exactly what's wanted: "have we
        # run this form's passes on this page" is a page-level question, not a
        # per-result-content one.
        self._form_exercised: dict[str, set[str]] = {}
        # Phase C: state keys already tried as a frontier-navigation target. Without
        # this, a state that LOOKS unexplored by (role, name) signature but is
        # actually saturated (e.g. a duplicate-name element already exercised under a
        # different index — see observe.py's Phase A grouping) would send the crawler
        # back to it forever, spinning at CPU speed until the wall-clock cap expires
        # rather than genuinely making progress. Each state is tried as a frontier
        # target at most once per run.
        self._frontier_tried: set[str] = set()

    def _pick_candidate(self, model: PageModel, exercised_refs: set[str]) -> ElementInfo | None:
        for el in model.elements:
            if el.ref in exercised_refs:
                continue
            if not el.enabled or not el.visible:
                continue
            return el
        return None

    def _next_unexercised_form(self, model: PageModel, page_template: str) -> FormGroup | None:
        done = self._form_exercised.setdefault(page_template, set())
        for form in group_forms(model):
            if form.submit is None or not form.fillable:
                continue  # nothing to fill, or no identifiable submit — not form-shaped
            if form.key in done:
                continue
            return form
        return None

    def _mark_form_exercised(self, page_template: str, state_key: str, form: FormGroup) -> None:
        # Both halves matter: record the form itself as done (what
        # _next_unexercised_form actually reads — this line was missing entirely
        # before, so a form was NEVER recognized as already-exercised regardless of
        # how it was keyed) and mark its individual elements exercised in the state
        # graph (what the coverage report reads).
        self._form_exercised.setdefault(page_template, set()).add(form.key)
        for el in (*form.fields, *form.buttons):
            self.graph.mark_exercised(state_key, el.ref, element=el)

    def _next_frontier_url(self) -> tuple[str, str] | None:
        """The nearest state the graph knows has unexercised elements that this run
        hasn't already tried jumping to. Returns (state_key, example_url) or None
        when the frontier is genuinely exhausted."""
        for node in self.graph.unexplored_states():
            key = node.fingerprint.key()
            if key in self._frontier_tried:
                continue
            return key, node.example_url
        return None

    def _is_below_fold(self, page: Page, bbox: tuple[float, float, float, float]) -> bool:
        viewport = page.viewport_size or {"width": 1280, "height": 800}
        _x, y, _w, h = bbox
        return y >= viewport["height"] or y + h <= 0

    async def _scroll_into_view(self, page: Page, model: PageModel, ref: str) -> None:
        try:
            await execute(page, model, Action(kind=ActionKind.SCROLL_INTO_VIEW, ref=ref))
        except Exception:
            pass  # best-effort — Playwright's own action executors auto-scroll anyway

    def _collect_from_recorder(self, recorder: Recorder, url: str, repro_steps: list[ReproStep], persona: str) -> None:
        self.findings.extend(crash_detector.detect(recorder, url, repro_steps, persona))
        self.findings.extend(console_detector.detect(recorder, url, repro_steps, persona))
        self.findings.extend(network_detector.detect(recorder, url, repro_steps, persona))
        recorder.clear()

    async def _check_invariants(self, page: Page, url: str, repro_steps: list[ReproStep], persona: str) -> None:
        for inv in self.cfg.invariants:
            finding = await invariants_engine.check(page, inv, url, repro_steps, persona)
            if finding:
                self.findings.append(finding)

    async def _attach_screenshot(self, page: Page, before_count: int) -> None:
        """Takes one screenshot covering every finding produced since `before_count`
        — all findings from the same detection batch are evidence of the same page
        state, so they share one screenshot rather than each triggering its own
        capture (would be redundant I/O for no extra information)."""
        if self.screenshot_dir is None or len(self.findings) <= before_count:
            return
        self._screenshot_counter += 1
        path = self.screenshot_dir / f"{self._screenshot_counter:04d}.png"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(path), full_page=True)
        except Exception:
            return  # best-effort — a failed screenshot shouldn't fail the whole crawl
        for f in self.findings[before_count:]:
            f.evidence.screenshot_path = str(path)

    async def _check_per_action(self, page: Page, url: str, repro_steps: list[ReproStep], persona: str) -> None:
        """Cheap checks worth running after every single action."""
        self.findings.extend(await security_detector.detect_reflected_injection(page, url, repro_steps, persona))
        self.findings.extend(await performance_detector.detect(page, url, repro_steps, persona))

    async def _check_per_state(self, page: Page, url: str, repro_steps: list[ReproStep], persona: str) -> None:
        """Page-rendering checks (visual geometry/contrast, axe-core a11y scan) —
        properties of the current state, not of a specific action, so running these
        after every action would just re-report the same static-page issues
        repeatedly. Called only on a state's FIRST visit (see run())."""
        self.findings.extend(await visual_detector.detect(page, url, repro_steps, persona))
        self.findings.extend(await a11y_detector.detect(page, url, repro_steps, persona))

    async def _do_action(
        self, page: Page, current_model: PageModel, action: Action, element: ElementInfo,
        recorder: Recorder, persona: str, state_key: str,
    ) -> str:
        """One action's full lifecycle — guardrail check, fill sanitization, throttle,
        repro-step history, execution, and the post-action detector batch. Shared by
        the single-element crawl path and the form-fill path (Phase B) so both go
        through byte-identical guardrail/detector/timing logic; previously the form
        path didn't exist and this logic lived inline in run()'s loop body only.
        Returns "ok", "stale" (ref no longer resolves — caller should stop this
        element/field's remaining queued actions), or "blocked" (guardrail refused
        it — caller should move on, not treat it as a failure)."""
        if action.kind == ActionKind.FILL and action.value is not None:
            action = Action(
                kind=action.kind, ref=action.ref,
                value=sanitize_fill_value(str(action.value), element, self.cfg.guardrails),
            )

        try:
            check_action(action, current_model, self.cfg.guardrails)
        except GuardrailViolation:
            return "blocked"

        self.limiter.throttle()
        repro_step = ReproStep(
            description=_describe(action, element),
            action_kind=action.kind.value, ref=action.ref,
            value=str(action.value) if action.value is not None else None,
            url_before=page.url,
        )
        self._action_history.append(repro_step)
        history = list(self._action_history)  # snapshot for this step's findings
        before = len(self.findings)

        try:
            await execute(page, current_model, action)
        except ActionError:
            return "stale"
        except Exception as e:
            self.findings.append(_action_failed_finding(page.url, history, e, persona))
            await self._attach_screenshot(page, before)
            return "ok"

        self.limiter.record_action()
        await page.wait_for_timeout(200)  # let network/console settle before observing

        # check_action() only validates NAVIGATE actions against allowed_domains — a
        # CLICK has no URL to check beforehand. This is what actually stops the
        # crawler from following an <a href> off-target and continuing to fuzz forms
        # there under this run's storage_state (auth cookies included).
        await enforce_scope_after_action(page, self.cfg.guardrails)

        try:
            new_model = await build_page_model(page)
        except Exception:
            new_model = None
        if new_model is not None:
            self.graph.record_action(state_key, fingerprint(new_model).key(), repro_step.description)
        self._collect_from_recorder(recorder, page.url, history, persona)
        await self._check_invariants(page, page.url, history, persona)
        await self._check_per_action(page, page.url, history, persona)
        await self._attach_screenshot(page, before)
        return "ok"

    async def _submit_form_once(
        self, page: Page, recorder: Recorder, form: FormGroup, persona: str, state_key: str,
        hostile_ref: str | None, hostile_seed: int,
    ) -> None:
        """Fills every field with realistic data (Phase B's valid pass), except
        `hostile_ref` if given, which gets one deliberately unusual value while
        everything else around it stays coherent — this is what makes a resulting
        error attributable to a specific field rather than "the form was full of
        junk". Then submits. Re-observes before each field since a prior field's
        value can trigger live validation that changes the DOM (a shown error, a
        dependent field appearing)."""
        for field in form.fillable:
            try:
                self.limiter.check_before_action()
            except BudgetStop:
                return
            current_model = await build_page_model(page)
            live = current_model.find(field.ref)
            if live is None or not live.enabled or not live.visible:
                continue
            if live.bbox and self._is_below_fold(page, live.bbox):
                await self._scroll_into_view(page, current_model, field.ref)
            if field.ref == hostile_ref:
                value = hostile_value_for(field.role, seed=hostile_seed).value
            else:
                value = valid_value_for(field.role, field.name, field.input_type)
            action = Action(kind=ActionKind.FILL, ref=field.ref, value=value)
            outcome = await self._do_action(page, current_model, action, live, recorder, persona, state_key)
            if outcome == "stale":
                return  # page navigated away mid-form — nothing left to submit

        for field in form.checkable:
            try:
                self.limiter.check_before_action()
            except BudgetStop:
                return
            current_model = await build_page_model(page)
            live = current_model.find(field.ref)
            if live is None or not live.enabled or not live.visible or live.checked:
                continue  # leave already-checked boxes and radios alone
            action = Action(kind=ActionKind.CHECK, ref=field.ref)
            outcome = await self._do_action(page, current_model, action, live, recorder, persona, state_key)
            if outcome == "stale":
                return

        if form.submit is None:
            return
        try:
            self.limiter.check_before_action()
        except BudgetStop:
            return
        current_model = await build_page_model(page)
        live_submit = current_model.find(form.submit.ref)
        if live_submit is None or not live_submit.enabled or not live_submit.visible:
            return
        action = Action(kind=ActionKind.CLICK, ref=form.submit.ref)
        await self._do_action(page, current_model, action, live_submit, recorder, persona, state_key)

    async def _run_form(
        self, page: Page, recorder: Recorder, form: FormGroup, page_template: str, state_key: str, persona: str,
    ) -> None:
        """Two-pass form exploration (Phase B), replacing the old one-field-at-a-time
        fuzzing that never reached a submit button: a bounded number of fuzz
        submissions (one field corrupted, rest valid) followed by exactly one fully
        valid submission — the pass that actually exercises real business logic
        (creating a record, running a search), not just page chrome. Marked
        exercised up front so a budget stop mid-form doesn't cause a retry loop on
        the next run."""
        self._mark_form_exercised(page_template, state_key, form)
        start_url = page.url

        fuzz_targets = form.fillable[:FORM_FUZZ_FIELD_LIMIT]
        for i, bad_field in enumerate(fuzz_targets):
            try:
                self.limiter.check_before_action()
            except BudgetStop:
                return
            if page.url.rstrip("/") != start_url.rstrip("/"):
                try:
                    await guard_goto(page, start_url, self.cfg.guardrails)
                except Exception:
                    return
            await self._submit_form_once(
                page, recorder, form, persona, state_key, hostile_ref=bad_field.ref, hostile_seed=i,
            )

        try:
            self.limiter.check_before_action()
        except BudgetStop:
            return
        if page.url.rstrip("/") != start_url.rstrip("/"):
            try:
                await guard_goto(page, start_url, self.cfg.guardrails)
            except Exception:
                return
        await self._submit_form_once(page, recorder, form, persona, state_key, hostile_ref=None, hostile_seed=0)

    async def run(self, page: Page, recorder: Recorder, persona: str = "heuristic") -> CrawlResult:
        await performance_detector.setup(page)  # must run before the first goto
        await guard_goto(page, self.target_url, self.cfg.guardrails)
        baseline_heap = await performance_detector.read_heap_size(page)

        budget_stopped = False
        while True:
            try:
                self.limiter.check_before_action()
            except BudgetStop:
                break

            model = await build_page_model(page)
            node = self.graph.visit(model)
            state_key = node.fingerprint.key()
            if node.visit_count == 1:
                before = len(self.findings)
                await self._check_per_state(page, page.url, list(self._action_history), persona)
                await self._attach_screenshot(page, before)

            # Phase B: fill-and-submit whole forms before falling back to picking off
            # individual elements — this is what actually exercises business logic
            # (creating a record, running a search) instead of just page chrome.
            # Tracked by URL template, not full state — see _form_exercised's
            # docstring on why (a GET search form's own results changing the state
            # fingerprint must not make the form look "unexercised" again).
            page_template = url_template(page.url)
            form = self._next_unexercised_form(model, page_template)
            if form is not None:
                await self._run_form(page, recorder, form, page_template, state_key, persona)
                continue

            candidate = self._pick_candidate(model, node.exercised_refs)
            if candidate is None:
                # Phase C: consult the state graph for the nearest known-unexplored
                # state instead of giving up the moment the CURRENT page saturates —
                # the old behavior returned to target_url once and broke if even that
                # was full, abandoning states the graph already knew had work left.
                frontier = self._next_frontier_url()
                if frontier is not None:
                    _frontier_key, frontier_url = frontier
                    self._frontier_tried.add(_frontier_key)
                    try:
                        await guard_goto(page, frontier_url, self.cfg.guardrails)
                        continue
                    except Exception:
                        pass
                break  # frontier genuinely exhausted (or unreachable) — nothing left

            actions = _actions_for(candidate)
            for action in actions:
                try:
                    self.limiter.check_before_action()
                except BudgetStop:
                    self.graph.mark_exercised(state_key, candidate.ref, element=candidate)
                    self._collect_from_recorder(recorder, page.url, list(self._action_history), persona)
                    budget_stopped = True
                    break

                current_model = await build_page_model(page)
                live = current_model.find(candidate.ref) or candidate
                if live.bbox and self._is_below_fold(page, live.bbox):
                    await self._scroll_into_view(page, current_model, candidate.ref)

                outcome = await self._do_action(page, current_model, action, candidate, recorder, persona, state_key)
                if outcome == "stale":
                    # ref went stale mid-sequence (e.g. the page navigated after a
                    # click) — stop this element's remaining queued actions, not the
                    # whole crawl.
                    break

            self.graph.mark_exercised(state_key, candidate.ref, element=candidate)
            if budget_stopped:
                break

        # Session-level counterpart to the per-action detector batch above — see
        # detectors/performance.py:check_heap_growth's docstring for why this needs
        # exactly two readings (start, end) rather than being wired into detect().
        current_heap = await performance_detector.read_heap_size(page)
        growth_finding = performance_detector.check_heap_growth(baseline_heap, current_heap, page.url, persona)
        if growth_finding:
            self.findings.append(growth_finding)

        return CrawlResult(self.findings, self.graph, self.limiter.actions_taken)


def _action_failed_finding(url: str, history: list[ReproStep], error: Exception, persona: str) -> Finding:
    from qaura.reporting.models import Evidence, Severity

    last = history[-1]
    return Finding(
        title=f"Action raised an exception: {last.description}",
        detector="crash",
        severity=Severity.HIGH,
        persona=persona,
        url=url,
        description=f"Executing '{last.description}' raised: {error}",
        repro_steps=list(history),
        evidence=Evidence(page_error=str(error)),
    )
