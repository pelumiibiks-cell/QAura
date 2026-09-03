"""Network detector: failed or 4xx XHR/fetch requests. This is the closest the
no-LLM phase gets to catching bug #4 from the plan's fixture list ("a fetch that 404s
silently and leaves a spinner forever") — full expectation-divergence detection (did
the UI actually get stuck) needs the planner's `expectation` field from Phase 3, but a
failed background request is itself worth flagging even without knowing what the page
was supposed to do about it.

Only `xhr`/`fetch` resource types are considered — a 404 on an optional analytics
beacon or a missing favicon is normal and not worth a finding; scoping to XHR/fetch
targets requests the page's own logic actually depends on.
"""
from __future__ import annotations

from qaura.browser.recorder import Recorder
from qaura.reporting.models import Evidence, Finding, ReproStep, Severity

RELEVANT_RESOURCE_TYPES = {"xhr", "fetch"}


def detect(recorder: Recorder, url: str, repro_steps: list[ReproStep], persona: str = "heuristic") -> list[Finding]:
    findings: list[Finding] = []

    for entry in recorder.network:
        if entry.resource_type not in RELEVANT_RESOURCE_TYPES:
            continue

        if entry.status is not None and entry.status >= 500:
            continue  # crash.py already reports 5xx; avoid double-counting the same request

        if entry.ok is False and entry.status is not None and 400 <= entry.status < 500:
            findings.append(Finding(
                title=f"Failed API call: {entry.method} {_short_url(entry.url)} -> {entry.status}",
                detector="network",
                severity=Severity.MEDIUM,
                persona=persona,
                url=url,
                description=(
                    f"{entry.method} {entry.url} returned HTTP {entry.status}. The page's UI "
                    f"was not observed to report this failure to the user — check whether the "
                    f"triggering action fails silently."
                ),
                repro_steps=list(repro_steps),
                evidence=Evidence(network_failures=[f"{entry.method} {entry.url} -> {entry.status}"]),
            ))
        elif entry.ok is False and entry.status is None:
            findings.append(Finding(
                title=f"Request failed (no response): {entry.method} {_short_url(entry.url)}",
                detector="network",
                severity=Severity.MEDIUM,
                persona=persona,
                url=url,
                description=(
                    f"{entry.method} {entry.url} never received a response"
                    f"{f' ({entry.failure_text})' if entry.failure_text else ''} "
                    f"— aborted, blocked, or a connection-level failure."
                ),
                repro_steps=list(repro_steps),
                evidence=Evidence(network_failures=[
                    f"{entry.method} {entry.url} -> {entry.failure_text or 'no response'}"
                ]),
            ))

    return findings


def _short_url(url: str) -> str:
    return url if len(url) <= 60 else url[:57] + "..."
