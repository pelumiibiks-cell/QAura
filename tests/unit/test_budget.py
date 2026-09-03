import pytest

from qaura.llm.base import LLMResponse, Tier, Usage
from qaura.llm.budget import Budget, BudgetExceeded


def _response(input_tokens: int, output_tokens: int) -> LLMResponse:
    return LLMResponse(
        text="", parsed=None,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens,
                     total_tokens=input_tokens + output_tokens),
        session_id=None,
    )


def test_records_calls_and_tokens_by_tier():
    b = Budget()
    b.record(Tier.PLANNER, _response(10, 5))
    b.record(Tier.ELEMENT_CLASSIFY, _response(3, 1))
    assert b.calls == 2
    assert b.input_tokens == 13
    assert b.output_tokens == 6
    assert b.total_tokens == 19
    assert b.by_tier == {"planner": 1, "element_classify": 1}


def test_check_passes_under_cap():
    b = Budget(max_calls=5, max_tokens=1000)
    b.record(Tier.PLANNER, _response(10, 5))
    b.check()  # should not raise


def test_check_raises_on_call_cap():
    b = Budget(max_calls=1)
    b.record(Tier.PLANNER, _response(1, 1))
    b.record(Tier.PLANNER, _response(1, 1))
    with pytest.raises(BudgetExceeded) as exc_info:
        b.check()
    assert exc_info.value.kind == "calls"


def test_check_raises_on_token_cap():
    b = Budget(max_tokens=10)
    b.record(Tier.PLANNER, _response(8, 5))
    with pytest.raises(BudgetExceeded) as exc_info:
        b.check()
    assert exc_info.value.kind == "tokens"


def test_check_raises_exactly_at_cap_not_one_over():
    # Regression: check() used to compare with `>`, and every real call site invokes
    # check() BEFORE attempting the next call (see budget.py's docstring) — so a cap
    # of 1 let a 2nd call through before check() ever caught it. It should stop
    # once exactly `max_calls` calls have already been recorded.
    b = Budget(max_calls=1)
    b.record(Tier.PLANNER, _response(1, 1))
    assert b.calls == 1
    with pytest.raises(BudgetExceeded):
        b.check()


def test_no_caps_never_raises():
    b = Budget()
    for _ in range(100):
        b.record(Tier.PLANNER, _response(1000, 1000))
    b.check()  # no caps set, should never raise
