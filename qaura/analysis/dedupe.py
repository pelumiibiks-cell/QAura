"""Finding fingerprint + clustering. Motivated by concrete data, not a hypothetical:
Phase 4's live verification run against buggy_app produced 15 near-identical
`invariant` findings for one underlying bug (the crawler re-checks invariants after
every action once the page is already in the broken state) — a report with 15 rows
for one bug is unreadable, and it's what the plan's "Findings must survive triage and
replay" design decision exists to prevent.

Fingerprint = (detector, url_template, normalized title). `core/state.py:url_template`
already exists and does exactly the URL normalization needed here (drops query
strings, collapses opaque IDs) — reused rather than reimplemented. Title
normalization collapses standalone numbers (task counts, contrast ratios, byte
counts — anything that varies run-to-run for the same underlying issue) while
leaving quoted element names/text alone, since two DIFFERENT elements failing the
same rule are legitimately different findings, not duplicates.
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
from dataclasses import dataclass

from qaura.core.state import url_template
from qaura.reporting.models import Finding, Severity

_DIGIT_RUN_RE = re.compile(r"\b\d+(\.\d+)?\b")
_QUOTED_RE = re.compile(r"(\"[^\"]*\"|'[^']*')")
_SEVERITY_RANK = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3, Severity.INFO: 4}


def normalize_title(title: str) -> str:
    # Digits inside quotes belong to an element's name ("Item 2"), so only collapse the rest
    parts = _QUOTED_RE.split(title)
    return "".join(part if i % 2 else _DIGIT_RUN_RE.sub("#", part) for i, part in enumerate(parts)).strip().lower()


def fingerprint(finding: Finding) -> str:
    key = f"{finding.detector}|{url_template(finding.url)}|{normalize_title(finding.title)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


@dataclass
class DedupeResult:
    findings: list[Finding]
    total_before: int
    total_after: int

    @property
    def merged_count(self) -> int:
        return self.total_before - self.total_after


def dedupe(findings: list[Finding]) -> DedupeResult:
    """Groups findings by fingerprint and keeps one copy per group: the most severe
    member, earliest first on a tie, so a later HIGH duplicate of a LOW finding isn't
    lost. `occurrence_count` is the sum of the group's counts, so running dedupe on
    already-deduped findings doesn't reset them. Inputs are never mutated. Order of the
    surviving findings follows first appearance, not severity or detector — callers
    sort for display if they want a different order."""
    groups: dict[str, list[Finding]] = {}
    order: list[str] = []
    for f in findings:
        key = fingerprint(f)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(f)

    result: list[Finding] = []
    for key in order:
        group = sorted(
            groups[key],
            key=lambda f: (_SEVERITY_RANK.get(f.severity, len(_SEVERITY_RANK)), f.created_at),
        )
        count = sum(f.occurrence_count for f in group)
        result.append(dataclasses.replace(group[0], occurrence_count=count))

    return DedupeResult(findings=result, total_before=len(findings), total_after=len(result))
