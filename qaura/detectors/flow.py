"""Expectation-divergence detector — plan design decision #3, and the single
highest-value detector for bugs with no crash/console/network signal at all (a page
that just silently does the wrong thing). Only runs when the action came from the LLM
planner (core/planner.py), since only planned actions carry an `expectation` string;
the heuristic crawler (Phase 2) has nothing to compare against and this detector is a
no-op for it.

Uses a cheap tier (ELEMENT_CLASSIFY) since this runs after every single planned action
— it's high-volume by construction, same bucket as element classification in the
plan's model-tier design.
"""
from __future__ import annotations

from qaura.browser.observe import PageModel
from qaura.llm.base import LLMParseError, LLMProvider, Tier
from qaura.llm.budget import Budget
from qaura.llm.schemas import DivergenceJudgement
from qaura.reporting.models import Evidence, Finding, ReproStep, Severity

# Below this confidence, treat the judgement as too uncertain to report — an unsure
# "no" from the model is worse to surface as a finding than to just skip; false
# positives here erode trust in every other finding in the report.
CONFIDENCE_THRESHOLD = 0.6


def check(
    provider: LLMProvider,
    expectation: str | None,
    before: PageModel,
    after: PageModel,
    action_description: str,
    repro_steps: list[ReproStep],
    persona: str,
    budget: Budget | None = None,
) -> Finding | None:
    if not expectation or not provider.available:
        return None

    prompt = (
        f"Action taken: {action_description}\n"
        f"Stated expectation: {expectation}\n\n"
        f"Page state BEFORE the action:\n{before.to_prompt()}\n\n"
        f"Page state AFTER the action:\n{after.to_prompt()}\n\n"
        f"Judge whether the AFTER state actually satisfies the stated expectation."
    )

    try:
        response = provider.complete(
            tier=Tier.ELEMENT_CLASSIFY,
            system=(
                "You are judging whether a web page's state after an action matches what "
                "was expected. Be strict: partial or ambiguous satisfaction should count as "
                "NOT satisfied. Judge only the stated expectation, not general page quality."
            ),
            input=prompt,
            schema=DivergenceJudgement,
        )
    except LLMParseError as e:
        if budget is not None:
            budget.record(Tier.ELEMENT_CLASSIFY, e.as_response())
        return None
    if budget is not None:
        budget.record(Tier.ELEMENT_CLASSIFY, response)

    if response.parsed is None:
        return None

    judgement: DivergenceJudgement = response.parsed
    if judgement.satisfied or judgement.confidence < CONFIDENCE_THRESHOLD:
        return None

    return Finding(
        title=f"Expectation not met: {expectation[:80]}",
        detector="flow",
        severity=Severity.MEDIUM,
        persona=persona,
        url=after.url,
        description=(
            f"After '{action_description}', the expectation '{expectation}' was not met. "
            f"{judgement.explanation}"
        ),
        repro_steps=list(repro_steps),
        evidence=Evidence(),
    )
