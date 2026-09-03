import pytest

from qaura.browser.actions import ActionKind
from qaura.browser.observe import ElementInfo, PageModel
from qaura.core.planner import PlannerError, plan_next_action
from qaura.llm.base import LLMResponse, Usage
from qaura.llm.schemas import PlannedAction, PlannerResponse


class FakeProvider:
    """Implements just enough of LLMProvider for planner tests — no live API calls."""

    def __init__(self, response: LLMResponse) -> None:
        self._response = response
        self.last_call = None

    @property
    def available(self) -> bool:
        return True

    def complete(self, *, tier, system, input, schema=None, images=None, session=None):
        self.last_call = {"tier": tier, "system": system, "input": input, "session": session}
        return self._response


def _page() -> PageModel:
    return PageModel(url="http://shop.test/cart", title="Cart", elements=[
        ElementInfo(ref="e1", role="button", name="Checkout"),
        ElementInfo(ref="e2", role="textbox", name="Coupon code"),
    ])


def _planned_response(ref="e1", action="click", value=None, expectation="something happens") -> LLMResponse:
    parsed = PlannerResponse(
        action=PlannedAction(ref=ref, action=action, value=value, expectation=expectation, reasoning="because"),
    )
    return LLMResponse(text="{}", parsed=parsed, usage=Usage(), session_id="sess-123")


def test_plan_next_action_returns_valid_action():
    provider = FakeProvider(_planned_response(ref="e1", action="click"))
    action, planned, session = plan_next_action(provider, "be curious", _page(), set())
    assert action.kind == ActionKind.CLICK
    assert action.ref == "e1"
    assert action.expectation == "something happens"
    assert session == "sess-123"


def test_plan_next_action_carries_fill_value():
    provider = FakeProvider(_planned_response(ref="e2", action="fill", value="SAVE10"))
    action, planned, _ = plan_next_action(provider, "be curious", _page(), set())
    assert action.kind == ActionKind.FILL
    assert action.value == "SAVE10"


def test_plan_next_action_rejects_invented_ref():
    provider = FakeProvider(_planned_response(ref="e999", action="click"))
    with pytest.raises(PlannerError, match="invented ref"):
        plan_next_action(provider, "be curious", _page(), set())


def test_plan_next_action_rejects_unknown_action_kind():
    provider = FakeProvider(_planned_response(ref="e1", action="teleport"))
    with pytest.raises(PlannerError, match="unknown action kind"):
        plan_next_action(provider, "be curious", _page(), set())


def test_plan_next_action_rejects_unparsed_response():
    bad_response = LLMResponse(text="not json", parsed=None, usage=Usage(), session_id=None)
    provider = FakeProvider(bad_response)
    with pytest.raises(PlannerError, match="no parsable"):
        plan_next_action(provider, "be curious", _page(), set())


def test_plan_next_action_raises_if_provider_unavailable():
    class UnavailableProvider:
        available = False
        def complete(self, **kwargs):
            raise AssertionError("should never be called")

    with pytest.raises(PlannerError, match="unavailable"):
        plan_next_action(UnavailableProvider(), "be curious", _page(), set())


def test_plan_next_action_passes_session_through_to_provider_call():
    provider = FakeProvider(_planned_response())
    plan_next_action(provider, "be curious", _page(), set(), session="prior-session-id")
    assert provider.last_call["session"] == "prior-session-id"


def test_build_prompt_includes_exercised_refs_hint():
    from qaura.core.planner import build_prompt
    prompt = build_prompt(_page(), {"e1"})
    assert "e1" in prompt
    assert "Already tried" in prompt
