"""Turns a PageModel + persona prompt into one Action via the LLM planner. Replaces
core/heuristic.py's `_pick_candidate`/`_actions_for` for LLM-mode runs — same
downstream consumer (browser/actions.py:execute), different decision-making.

One action per call, not a multi-step plan: the orchestrator re-observes after every
action anyway (needed for divergence checking against `expectation`), so asking the
model for a sequence up front would mostly get discarded and re-planned after step one.
"""
from __future__ import annotations

from qaura.browser.actions import Action, ActionKind
from qaura.browser.observe import PageModel
from qaura.llm.base import LLMProvider, Tier
from qaura.llm.budget import Budget
from qaura.llm.schemas import PlannedAction, PlannerResponse

_ACTION_KIND_MAP = {
    "click": ActionKind.CLICK,
    "dblclick": ActionKind.DBLCLICK,
    "fill": ActionKind.FILL,
    "select": ActionKind.SELECT,
    "check": ActionKind.CHECK,
    "uncheck": ActionKind.UNCHECK,
    "key": ActionKind.KEY,
}


class PlannerError(RuntimeError):
    """The model returned something unusable — an invented ref, an unknown action
    kind, or a malformed response. Distinct from an LLMProvider/network error; the
    orchestrator should treat this as 'skip this turn, try again' rather than a fatal
    failure, since a single bad planner turn doesn't mean the model is broken."""


def build_prompt(model: PageModel, exercised_refs: set[str]) -> str:
    lines = [model.to_prompt()]
    if exercised_refs:
        lines.append(f"\nAlready tried this visit: {', '.join(sorted(exercised_refs))}")
        lines.append("Prefer a ref not in that list unless you have a specific reason to repeat one.")
    return "\n".join(lines)


def plan_next_action(
    provider: LLMProvider,
    persona_system_prompt: str,
    page_model: PageModel,
    exercised_refs: set[str],
    session: str | None = None,
    budget: Budget | None = None,
) -> tuple[Action, PlannedAction, str | None]:
    """Returns (Action for browser/actions.py, the raw PlannedAction for logging/
    repro-step text, the provider's new session id to carry forward). If `budget` is
    given, the call is recorded against it (even when the response fails to parse —
    a wasted call still spent tokens) before this function raises or returns."""
    if not provider.available:
        raise PlannerError("plan_next_action called with an unavailable LLMProvider")

    response = provider.complete(
        tier=Tier.PLANNER,
        system=persona_system_prompt,
        input=build_prompt(page_model, exercised_refs),
        schema=PlannerResponse,
        session=session,
    )
    if budget is not None:
        budget.record(Tier.PLANNER, response)

    if response.parsed is None:
        raise PlannerError(f"planner returned no parsable structured output: {response.text!r}")

    planned: PlannedAction = response.parsed.action

    if page_model.find(planned.ref) is None:
        raise PlannerError(
            f"planner invented ref {planned.ref!r} — not present in the PageModel it was given"
        )

    kind = _ACTION_KIND_MAP.get(planned.action.lower())
    if kind is None:
        raise PlannerError(f"planner returned unknown action kind: {planned.action!r}")

    action = Action(kind=kind, ref=planned.ref, value=planned.value, expectation=planned.expectation)
    return action, planned, response.session_id
