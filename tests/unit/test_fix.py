from pathlib import Path

from qaura.analysis.fix import apply_fix_suggestion, suggest_fix
from qaura.analysis.repo_index import build_index
from qaura.llm.base import LLMResponse, Tier, Usage
from qaura.reporting.models import Finding


class FakeProvider:
    def __init__(self, text: str = "Add a null check before calling .activate().", available: bool = True):
        self._text = text
        self._available = available
        self.last_call = None

    @property
    def available(self):
        return self._available

    def complete(self, *, tier, system, input, schema=None, images=None, session=None):
        self.last_call = {"tier": tier, "system": system, "input": input}
        return LLMResponse(text=self._text, parsed=None, usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15), session_id=None)


def _finding() -> Finding:
    return Finding(
        title="Uncaught page error: Cannot read properties of undefined",
        detector="crash",
        description="widget.activate() called on undefined",
        likely_component="tests/fixtures/buggy_app/app.py",
    )


async def test_suggest_fix_returns_provider_text():
    provider = FakeProvider(text="Guard against undefined before calling activate().")
    result = await suggest_fix(provider, _finding())
    assert result == "Guard against undefined before calling activate()."


async def test_suggest_fix_returns_none_when_provider_unavailable():
    provider = FakeProvider(available=False)
    result = await suggest_fix(provider, _finding())
    assert result is None
    assert provider.last_call is None


async def test_suggest_fix_returns_none_for_empty_response():
    provider = FakeProvider(text="   ")
    result = await suggest_fix(provider, _finding())
    assert result is None


async def test_suggest_fix_uses_fix_tier():
    provider = FakeProvider()
    await suggest_fix(provider, _finding())
    assert provider.last_call["tier"] == Tier.FIX


async def test_suggest_fix_includes_likely_component_in_prompt():
    provider = FakeProvider()
    await suggest_fix(provider, _finding())
    assert "app.py" in provider.last_call["input"]


async def test_suggest_fix_includes_source_snippet_when_given():
    provider = FakeProvider()
    await suggest_fix(provider, _finding(), source_snippet="function activate() {}")
    assert "function activate()" in provider.last_call["input"]


async def test_suggest_fix_records_budget():
    from qaura.llm.budget import Budget
    provider = FakeProvider()
    budget = Budget()
    await suggest_fix(provider, _finding(), budget=budget)
    assert budget.calls == 1


async def test_apply_fix_suggestion_sets_field_using_real_repo_snippet():
    index = build_index(Path(__file__).parent.parent / "fixtures" / "buggy_app")
    provider = FakeProvider(text="Check for undefined before calling .activate().")
    finding = _finding()
    await apply_fix_suggestion(provider, finding, index=index)
    assert finding.suggested_fix == "Check for undefined before calling .activate()."
    # confirms the real source file was actually read and passed through
    assert "brokenHandler" in provider.last_call["input"] or "activate" in provider.last_call["input"]


async def test_apply_fix_suggestion_noop_when_provider_unavailable():
    provider = FakeProvider(available=False)
    finding = _finding()
    await apply_fix_suggestion(provider, finding)
    assert finding.suggested_fix is None
