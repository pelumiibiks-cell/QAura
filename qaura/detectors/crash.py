"""Crash detector: uncaught page errors (`pageerror` events, i.e. Recorder.crashes) and
5xx responses. Purely passive — reads what Recorder already captured, doesn't drive
the browser itself. This is the simplest detector and needs no LLM.
"""
from __future__ import annotations

from qaura.browser.recorder import Recorder
from qaura.reporting.models import Evidence, Finding, ReproStep, Severity


def detect(recorder: Recorder, url: str, repro_steps: list[ReproStep], persona: str = "heuristic") -> list[Finding]:
    findings: list[Finding] = []

    for crash in recorder.crashes:
        findings.append(Finding(
            title=f"Uncaught page error: {crash.message[:80]}",
            detector="crash",
            severity=Severity.HIGH,
            persona=persona,
            url=url,
            description=f"An uncaught JavaScript error occurred on the page: {crash.message}",
            repro_steps=list(repro_steps),
            evidence=Evidence(page_error=crash.message),
        ))

    for entry in recorder.network:
        if entry.status is not None and entry.status >= 500:
            findings.append(Finding(
                title=f"Server error {entry.status} on {entry.method} {_short_url(entry.url)}",
                detector="crash",
                severity=Severity.HIGH,
                persona=persona,
                url=url,
                description=(
                    f"{entry.method} {entry.url} returned HTTP {entry.status}, indicating an "
                    f"unhandled server-side error rather than a normal failure response."
                ),
                repro_steps=list(repro_steps),
                evidence=Evidence(network_failures=[f"{entry.method} {entry.url} -> {entry.status}"]),
            ))

    return findings


def _short_url(url: str) -> str:
    return url if len(url) <= 60 else url[:57] + "..."
