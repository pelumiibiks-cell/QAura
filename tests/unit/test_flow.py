from qaura.browser.observe import ElementInfo, PageModel
from qaura.detectors import flow
from qaura.llm.base import LLMResponse, Usage
from qaura.llm.schemas import DivergenceJudgement


class FakeProvider:
    def __init__(self, parsed=None, available=True):
        self._parsed = parsed
        self._available = available

    @property
    def available(self):
        return self._available

    def complete(self, *, tier, system, input, schema=None, images=None, session=None):
        return LLMResponse(text="{}", parsed=self._parsed, usage=Usage(), session_id=None)


def _page(url="http://shop.test/cart") -> PageModel:
    return PageModel(url=url, title="t", elements=[ElementInfo(ref="e1", role="button", name="Checkout")])


def test_check_returns_none_without_expectation():
    provider = FakeProvider(parsed=DivergenceJudgement(satisfied=False, explanation="x", confidence=0.9))
    result = flow.check(provider, None, _page(), _page(), "click e1", [], "curious")
    assert result is None


def test_check_returns_none_when_provider_unavailable():
    provider = FakeProvider(available=False)
    result = flow.check(provider, "checkout page loads", _page(), _page(), "click e1", [], "curious")
    assert result is None


def test_check_returns_none_when_satisfied():
    provider = FakeProvider(parsed=DivergenceJudgement(satisfied=True, explanation="all good", confidence=0.95))
    result = flow.check(provider, "checkout page loads", _page(), _page(), "click e1", [], "curious")
    assert result is None


def test_check_returns_finding_when_not_satisfied_above_threshold():
    provider = FakeProvider(parsed=DivergenceJudgement(satisfied=False, explanation="still on cart page", confidence=0.85))
    result = flow.check(provider, "checkout page loads", _page(), _page(), "click e1", [], "curious")
    assert result is not None
    assert result.detector == "flow"
    assert "still on cart page" in result.description


def test_check_returns_none_when_confidence_too_low():
    provider = FakeProvider(parsed=DivergenceJudgement(satisfied=False, explanation="maybe", confidence=0.3))
    result = flow.check(provider, "checkout page loads", _page(), _page(), "click e1", [], "curious")
    assert result is None


def test_check_returns_none_when_response_unparsed():
    provider = FakeProvider(parsed=None)
    result = flow.check(provider, "checkout page loads", _page(), _page(), "click e1", [], "curious")
    assert result is None
