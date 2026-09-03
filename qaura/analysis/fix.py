"""Suggested-fix generation, Tier.FIX. Plain text, not structured output —
Finding.suggested_fix is a prose field, and forcing a fix suggestion through a JSON
schema would add validation risk for no benefit here (unlike the planner, where
structured output feeds directly into an Action that must be exactly right).
"""
from __future__ import annotations

from qaura.analysis.repo_index import RepoIndex
from qaura.llm.base import LLMProvider, Tier
from qaura.llm.budget import Budget
from qaura.reporting.models import Finding

MAX_SNIPPET_CHARS = 4000  # keep the source excerpt well within a reasonable prompt size


def _read_snippet(index: RepoIndex, likely_component: str) -> str | None:
    first_file = likely_component.split(",")[0].strip()
    try:
        return (index.root / first_file).read_text(encoding="utf-8", errors="ignore")[:MAX_SNIPPET_CHARS]
    except OSError:
        return None


async def suggest_fix(
    provider: LLMProvider,
    finding: Finding,
    source_snippet: str | None = None,
    budget: Budget | None = None,
) -> str | None:
    if not provider.available:
        return None

    prompt = f"Bug: {finding.title}\nDetector: {finding.detector}\nDescription: {finding.description}\n"
    if finding.likely_component:
        prompt += f"Likely responsible file(s): {finding.likely_component}\n"
    if source_snippet:
        prompt += f"\nRelevant source:\n```\n{source_snippet}\n```\n"
    prompt += "\nSuggest a concise, specific fix. A few sentences, or a small code change if the cause is obvious from the source shown. If the source isn't shown or isn't enough to diagnose confidently, say what additional information would be needed instead of guessing."

    response = provider.complete(
        tier=Tier.FIX,
        system=(
            "You are an experienced software engineer reviewing an automated QA finding. "
            "Be concise and concrete — no filler, no restating the bug description back."
        ),
        input=prompt,
    )
    if budget is not None:
        budget.record(Tier.FIX, response)

    text = response.text.strip() if response.text else ""
    return text or None


async def apply_fix_suggestion(
    provider: LLMProvider,
    finding: Finding,
    index: RepoIndex | None = None,
    budget: Budget | None = None,
) -> None:
    snippet = _read_snippet(index, finding.likely_component) if index and finding.likely_component else None
    suggestion = await suggest_fix(provider, finding, snippet, budget=budget)
    if suggestion:
        finding.suggested_fix = suggestion
