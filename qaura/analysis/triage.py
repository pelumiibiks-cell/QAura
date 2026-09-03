"""LLM triage: a second opinion on each surviving (post-dedupe) finding before it
reaches the report — plan design decision #6. `Tier.TRIAGE` via structured output
(`TriageVerdict`, llm/schemas.py). The model can only argue to raise/lower/keep the
severity the detector already assigned (never invent a fresh one from nothing) —
the detector's judgment is the anchor, the LLM is a check on it, not a replacement.
"""
from __future__ import annotations

from qaura.llm.base import LLMProvider, Tier
from qaura.llm.budget import Budget
from qaura.llm.schemas import TriageVerdict
from qaura.reporting.models import Finding, Severity

# Worst first — matches Severity's own declaration order in reporting/models.py.
_SEVERITY_ORDER = [Severity.INFO, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]

# A false-positive verdict below this confidence isn't acted on — an unsure "probably
# not real" from the model is worse to silently drop than to just leave in the report
# for a human to judge; same reasoning as detectors/flow.py's CONFIDENCE_THRESHOLD.
FALSE_POSITIVE_CONFIDENCE_THRESHOLD = 0.7


def _adjust_severity(current: Severity, adjustment: str) -> Severity:
    try:
        idx = _SEVERITY_ORDER.index(current)
    except ValueError:
        return current
    if adjustment == "raise":
        idx = min(idx + 1, len(_SEVERITY_ORDER) - 1)
    elif adjustment == "lower":
        idx = max(idx - 1, 0)
    return _SEVERITY_ORDER[idx]


def _build_prompt(finding: Finding) -> str:
    lines = [
        f"Title: {finding.title}",
        f"Detector: {finding.detector}",
        f"Current severity: {finding.severity.value}",
        f"Occurrences this run: {finding.occurrence_count}",
        f"Description: {finding.description}",
    ]
    if finding.reproducibility:
        lines.append(f"Reproducibility (from replay): {finding.reproducibility}")
    return "\n".join(lines)


async def triage_finding(provider: LLMProvider, finding: Finding, budget: Budget | None = None) -> TriageVerdict | None:
    if not provider.available:
        return None

    response = provider.complete(
        tier=Tier.TRIAGE,
        system=(
            "You are triaging an automated QA finding before it reaches a human report. "
            "Judge whether this looks like a real bug or a likely false positive (a detector "
            "artifact, a benign edge case, expected behavior misread as a bug), and whether "
            "the assigned severity looks right, too high, or too low given the description."
        ),
        input=_build_prompt(finding),
        schema=TriageVerdict,
    )
    if budget is not None:
        budget.record(Tier.TRIAGE, response)
    return response.parsed


async def apply_triage(provider: LLMProvider, finding: Finding, budget: Budget | None = None) -> TriageVerdict | None:
    """Mutates `finding` in place (severity adjustment, a triage note appended to the
    description) and returns the verdict so the caller can decide whether to drop a
    high-confidence false positive from the report — this module doesn't drop
    findings itself, since deciding what "surviving the report" means is a
    cli.py/reporting concern, not triage's."""
    verdict = await triage_finding(provider, finding, budget=budget)
    if verdict is None:
        return None

    finding.severity = _adjust_severity(finding.severity, verdict.severity_adjustment)
    finding.description += f"\n\nTriage note ({verdict.confidence:.0%} confidence): {verdict.reasoning}"
    return verdict


def is_confident_false_positive(verdict: TriageVerdict | None) -> bool:
    return bool(
        verdict is not None
        and verdict.is_likely_false_positive
        and verdict.confidence >= FALSE_POSITIVE_CONFIDENCE_THRESHOLD
    )
