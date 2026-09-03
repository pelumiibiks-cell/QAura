"""Re-executes a finding's repro_steps against a fresh browser context N times,
recording how often the same bug fingerprint reappears. `ReproStep` (built in Phase 2,
carrying action_kind/ref/value/url_before) already has everything needed to
reconstruct the exact Action sequence — no new data collection required.

Reproduction is checked by re-running the same detectors used during the crawl and
comparing fingerprints via analysis/dedupe.py:fingerprint() — reusing dedupe's
fingerprint here means "did this bug happen again" and "is this the same bug as
that other finding" are the same question asked twice, not two separate concepts to
keep in sync.
"""
from __future__ import annotations

from dataclasses import dataclass

from qaura.analysis.dedupe import fingerprint as dedupe_fingerprint
from qaura.browser.actions import Action, ActionError, ActionKind, execute
from qaura.browser.driver import ContextSpec, Driver
from qaura.browser.observe import build_page_model
from qaura.browser.recorder import Recorder
from qaura.config import QAuraConfig
from qaura.core import invariants as invariants_engine
from qaura.core.guardrails import guard_goto
from qaura.detectors import a11y as a11y_detector
from qaura.detectors import console as console_detector
from qaura.detectors import crash as crash_detector
from qaura.detectors import network as network_detector
from qaura.detectors import performance as performance_detector
from qaura.detectors import security as security_detector
from qaura.detectors import visual as visual_detector
from qaura.reporting.models import Finding, ReproStatus, ReproStep

# Detectors whose findings can be meaningfully re-checked by re-running the action
# sequence. "flow" needs an LLM + the original expectation string, neither of which
# is persisted on a Finding, and "visual_baseline" is inherently a cross-run diff, not
# a single-run replay — both get ReproStatus.NOT_APPLICABLE instead of a fabricated
# reproducibility fraction. cli.py's analysis step is what actually applies this filter
# before calling replay_finding; it's re-exported here so replay.py and its callers
# agree on exactly one definition of "replayable".
REPLAYABLE_DETECTORS = {"crash", "console", "network", "security", "invariant", "visual", "a11y", "performance"}


@dataclass
class ReplayResult:
    finding_id: str
    attempts: int
    successes: int

    @property
    def rate_str(self) -> str:
        return f"{self.successes}/{self.attempts}"


def _action_from_repro_step(step: ReproStep) -> Action | None:
    if step.action_kind is None:
        return None
    try:
        kind = ActionKind(step.action_kind)
    except ValueError:
        return None
    return Action(kind=kind, ref=step.ref, value=step.value)


async def _attempt_once(
    driver: Driver, cfg: QAuraConfig, finding: Finding, storage_state_path: str | None = None
) -> bool:
    if not finding.repro_steps:
        return False  # nothing to replay against

    spec = ContextSpec(persona="replay", role=None, storage_state_path=storage_state_path)
    async with driver.context(spec) as (context, page):
        recorder = Recorder(page, context)
        await recorder.start(trace=False)
        await performance_detector.setup(page)

        start_url = finding.repro_steps[0].url_before or finding.url
        try:
            await guard_goto(page, start_url, cfg.guardrails)
        except Exception:
            return False

        for step in finding.repro_steps:
            action = _action_from_repro_step(step)
            if action is None:
                continue  # a step with no action_kind (e.g. NAVIGATE-only) — skip, not fatal
            try:
                model = await build_page_model(page)
            except Exception:
                # Previously unguarded — a rebuild failure here raised out of this
                # whole attempt, and (before analysis got its own try/except in
                # cli.py) could abort every remaining replay in the run, not just
                # this one attempt at this one finding.
                return False
            try:
                await execute(page, model, action)
            except ActionError:
                return False  # the ref this bug depended on no longer resolves
            except Exception:
                pass  # the action itself erroring might BE the bug reproducing — keep going
            await page.wait_for_timeout(200)

        new_findings: list[Finding] = []
        new_findings.extend(crash_detector.detect(recorder, page.url, [], finding.persona))
        new_findings.extend(console_detector.detect(recorder, page.url, [], finding.persona))
        new_findings.extend(network_detector.detect(recorder, page.url, [], finding.persona))
        new_findings.extend(await security_detector.detect_reflected_injection(page, page.url, [], finding.persona))
        for inv in cfg.invariants:
            inv_finding = await invariants_engine.check(page, inv, page.url, [], finding.persona)
            if inv_finding:
                new_findings.append(inv_finding)
        # Page-state detectors, gated on the finding's own detector so a crash/security
        # finding doesn't pay for a full axe-core pass or DOM walk it has no use for.
        # Phase D fix: these three were never re-run at all before, so every
        # visual/a11y/performance finding was stamped a misleading `0/N, not
        # confirmed` regardless of whether it actually reproduced — see ReproStatus.
        if finding.detector == "visual":
            new_findings.extend(await visual_detector.detect(page, page.url, [], finding.persona))
        elif finding.detector == "a11y":
            new_findings.extend(await a11y_detector.detect(page, page.url, [], finding.persona))
        elif finding.detector == "performance":
            new_findings.extend(await performance_detector.detect(page, page.url, [], finding.persona))

        target = dedupe_fingerprint(finding)
        return any(dedupe_fingerprint(f) == target for f in new_findings)


async def replay_finding(
    driver: Driver,
    cfg: QAuraConfig,
    finding: Finding,
    attempts: int = 3,
    storage_state_path: str | None = None,
) -> ReplayResult:
    successes = 0
    for _ in range(attempts):
        if await _attempt_once(driver, cfg, finding, storage_state_path=storage_state_path):
            successes += 1
    result = ReplayResult(finding_id=finding.id, attempts=attempts, successes=successes)
    finding.reproducibility = result.rate_str
    finding.confirmed = successes > 0
    if successes == attempts:
        finding.reproducibility_status = ReproStatus.CONFIRMED
    elif successes == 0:
        finding.reproducibility_status = ReproStatus.NOT_REPRODUCED
    else:
        finding.reproducibility_status = ReproStatus.FLAKY
    return result
