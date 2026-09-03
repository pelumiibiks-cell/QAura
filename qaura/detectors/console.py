"""Console detector: browser console.error() calls that don't rise to an uncaught
pageerror (crash.py handles those) but still indicate something broke — a caught
exception that was logged instead of surfaced, a failed assertion, a library warning
that's actually diagnostic of a real problem.
"""
from __future__ import annotations

from qaura.browser.recorder import Recorder
from qaura.reporting.models import Evidence, Finding, ReproStep, Severity


def detect(recorder: Recorder, url: str, repro_steps: list[ReproStep], persona: str = "heuristic") -> list[Finding]:
    findings: list[Finding] = []
    for entry in recorder.console_errors:
        findings.append(Finding(
            title=f"Console error: {entry.text[:80]}",
            detector="console",
            severity=Severity.MEDIUM,
            persona=persona,
            url=url,
            description=(
                f"A console.error was logged"
                f"{f' at {entry.location}' if entry.location else ''}: {entry.text}"
            ),
            repro_steps=list(repro_steps),
            evidence=Evidence(console_errors=[entry.text]),
        ))
    return findings
