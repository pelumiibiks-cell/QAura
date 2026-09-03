"""Accessibility detector — injects axe-core via axe_playwright_python and turns its
violations into Findings. No LLM needed; axe-core's own rule engine is the detector
here, same "rules first" spirit as detectors/visual.py.

API note: axe_playwright_python.async_playwright.Axe().run(page) returns an
AxeResults wrapping the raw axe-core response dict; violations live at
`.response["violations"]`, each with `id`/`impact`/`description`/`help`/`nodes`
(nodes carry `target` selectors and an `html` snippet) — confirmed by reading the
installed package's source directly (axe_playwright_python/base.py) since this
dependency had never actually been exercised before this detector, per plan/Phase 0.
"""
from __future__ import annotations

import logging

from playwright.async_api import Page

from qaura.reporting.models import Evidence, Finding, ReproStep, Severity

_log = logging.getLogger(__name__)

# axe-core impact levels, worst first
_IMPACT_TO_SEVERITY = {
    "critical": Severity.CRITICAL,
    "serious": Severity.HIGH,
    "moderate": Severity.MEDIUM,
    "minor": Severity.LOW,
}


async def detect(page: Page, url: str, repro_steps: list[ReproStep], persona: str = "heuristic") -> list[Finding]:
    from axe_playwright_python.async_playwright import Axe

    # axe-core is injected as a script into the page — a target with a CSP that
    # blocks inline/eval script injection makes this raise on the very first state
    # visit, with no degradation path before this, killing the entire run over one
    # missing capability rather than just skipping this one detector for this page.
    try:
        results = await Axe().run(page)
    except Exception:
        _log.debug("axe-core run failed on %s (often a CSP blocking script injection)", url, exc_info=True)
        return []
    violations = results.response.get("violations", [])

    findings: list[Finding] = []
    for v in violations:
        targets = [", ".join(node.get("target", [])) for node in v.get("nodes", [])]
        snippet_count = len(v.get("nodes", []))
        findings.append(Finding(
            title=f"Accessibility: {v.get('help', v.get('id', 'unknown rule'))} ({snippet_count} element(s))",
            detector="a11y",
            severity=_IMPACT_TO_SEVERITY.get(v.get("impact"), Severity.MEDIUM),
            persona=persona,
            url=url,
            description=(
                f"{v.get('description', '')} Affected: {'; '.join(targets[:5])}"
                f"{' ...' if len(targets) > 5 else ''}"
            ),
            repro_steps=list(repro_steps),
            evidence=Evidence(),
        ))
    return findings
