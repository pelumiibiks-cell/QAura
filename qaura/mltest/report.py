"""ML report card — reuses reporting/models.py + reporting/html.py directly rather
than building a parallel renderer, per the plan's own instruction ("model report
card, same HTML shell"). ML findings already use the same `Finding` model as the
web-crawl detectors (see mltest/suites/artifact.py's module docstring for why); a
`RunReport` built from them renders through the exact same template unmodified.
"""
from __future__ import annotations

from datetime import datetime, timezone

from qaura.reporting.models import Finding, RunReport, RunSummary, Severity

# Gate verdict: FAIL if anything CRITICAL/HIGH survived, WARN if only MEDIUM/LOW,
# PASS if clean. Matches the plan's "report card with pass/warn/fail gates" language.
_FAIL_SEVERITIES = {Severity.CRITICAL, Severity.HIGH}
_WARN_SEVERITIES = {Severity.MEDIUM, Severity.LOW}


def overall_gate(findings: list[Finding]) -> str:
    severities = {f.severity for f in findings}
    if severities & _FAIL_SEVERITIES:
        return "FAIL"
    if severities & _WARN_SEVERITIES:
        return "WARN"
    return "PASS"


def build_run_report(
    target: str, findings: list[Finding], mode: str = "ml",
    metrics: dict | None = None, started_at: str | None = None,
) -> RunReport:
    now = datetime.now(timezone.utc).isoformat()
    summary = RunSummary(
        target_url=target, started_at=started_at or now, finished_at=now,
        mode=mode, personas=[], coverage=metrics or {}, llm_usage=None,
    )
    return RunReport(summary=summary, findings=findings)
