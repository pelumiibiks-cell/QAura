"""Structured-output schemas for the Gemini planner (Phase 3) and later triage/localize
(Phase 5). Kept separate from llm/base.py since these are specific to what QAura asks
the model for, not part of the provider-agnostic interface itself.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class PlannedAction(BaseModel):
    """One action the planner wants taken next. `ref` must be one of the refs listed
    in the PageModel prompt it was given — the planner never invents a ref, and
    core/planner.py validates this before handing the action to browser/actions.py.
    """

    ref: str = Field(description="The element ref to act on, e.g. 'e3'. Must be a ref that was listed.")
    action: str = Field(description="One of: click, dblclick, fill, select, check, uncheck, key")
    value: str | None = Field(default=None, description="Text to fill, option to select, or key to press. Omit for click/dblclick/check/uncheck.")
    expectation: str = Field(description="A short, checkable statement of what should be true on the page after this action succeeds.")
    reasoning: str = Field(description="One sentence: why this action, from this persona's perspective.")


class PlannerResponse(BaseModel):
    """The planner picks exactly one action per turn (not a multi-step plan) — the
    orchestrator re-observes after every action anyway (design decision #3, divergence
    checking needs a fresh PageModel per action), so asking for a whole sequence
    up front would just be discarded/re-planned after the first step regardless."""

    action: PlannedAction
    persona_note: str | None = Field(
        default=None, description="Optional aside on why this persona finds this page interesting right now."
    )


class DivergenceJudgement(BaseModel):
    """Used by the expectation-divergence detector: did the page, after an action,
    actually satisfy the expectation the planner set for it?"""

    satisfied: bool
    explanation: str = Field(description="One sentence explaining the judgement.")
    confidence: float = Field(ge=0.0, le=1.0)


class CandidateValue(BaseModel):
    """One named number in a proposed invariant. The model picks an ELEMENT INDEX from
    the inventory it was shown rather than writing a CSS selector, which is what makes
    selector hallucination structurally impossible: qaura maps the index back to the
    selector it measured itself, and an out-of-range index is rejected outright instead
    of becoming a config entry that silently matches nothing."""

    name: str = Field(description="snake_case identifier used in `expression`, e.g. 'subtotal'.")
    element_index: int = Field(
        description="Index of an element from the inventory, e.g. 7 for n7. Never invent one."
    )


class InvariantCandidate(BaseModel):
    """A proposed business rule, in the shape config.InvariantConfig will have to accept.

    `values` is a list of objects rather than a dict[str, int] deliberately — a dict with
    a typed value produces a JSON Schema relying on additionalProperties, which Gemini's
    structured-output path handles inconsistently, whereas a list of objects always
    serializes cleanly.
    """

    name: str = Field(description="snake_case rule name, e.g. 'subtotal_matches_line_items'.")
    description: str = Field(description="One sentence a human can check the rule against.")
    container_index: int | None = Field(
        default=None, description="Inventory index whose container scopes this rule, or null."
    )
    values: list[CandidateValue] = Field(description="At least two named values.")
    expression: str = Field(
        description=(
            "A boolean expression over the value names. Only comparisons, arithmetic, "
            "and sum/min/max/abs/len/round. Compare money and other computed numbers "
            "with a tolerance, e.g. abs(total - (subtotal - discount)) <= 0.01, never ==."
        )
    )
    rationale: str = Field(description="One sentence: why this rule should hold.")


class InvariantCandidateList(BaseModel):
    candidates: list[InvariantCandidate]


class TriageVerdict(BaseModel):
    """Used by analysis/triage.py: a second opinion on a finding before it reaches
    the report. Deliberately does NOT let the model invent a brand-new severity out
    of nothing — it can only argue for raising/lowering what the detector already
    assigned, via `severity_adjustment`, keeping the detector's own judgment as the
    anchor rather than fully deferring to the LLM."""

    is_likely_false_positive: bool
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in this verdict, not in the bug itself.")
    severity_adjustment: str = Field(
        description="One of: lower, keep, raise — relative to the finding's current severity."
    )
    reasoning: str = Field(description="One or two sentences explaining the verdict.")
