"""Candidate generation and the sanity filter.

The filter is the load-bearing part. A wrong invariant is worse than no invariant — it
becomes a permanent false positive that fires on every future run and looks exactly like
a real bug in the report — so everything that can be rejected without a browser is
rejected here.
"""

from qaura.browser.numerics import NumericElement
from qaura.config import InvariantConfig
from qaura.init.candidates import (
    InventoryEntry,
    build_inventory,
    generate,
    group_inventory,
    render_inventory,
    sanity_filter,
)
from qaura.llm.base import LLMResponse, Usage
from qaura.llm.schemas import CandidateValue, InvariantCandidate, InvariantCandidateList


def _element(index, selector, label=None, container='[data-testid="cart"]', number=10.0,
             scoped=1, score=110, volatile=False, dom_index=0):
    return NumericElement(
        index=index, selector=selector, selector_kind="testid", selector_score=score,
        match_count=scoped, scoped_count=scoped, container_selector=container,
        raw_text=f"${number}", number=number, label=label, label_source="prev-sibling-text",
        tag="span", dom_index=dom_index, volatile=volatile,
    )


def _entries():
    return [
        InventoryEntry(element=_element(1, '[data-testid="cart-total"]', "Total")),
        InventoryEntry(element=_element(2, '[data-testid="cart-subtotal"]', "Subtotal")),
        InventoryEntry(element=_element(3, '[data-testid="cart-discount"]', "Discount", number=0.0)),
    ]


def _candidate(**overrides):
    base = dict(
        name="total_matches",
        description="Total equals subtotal minus discount",
        container_index=1,
        values=[
            CandidateValue(name="total", element_index=1),
            CandidateValue(name="subtotal", element_index=2),
            CandidateValue(name="discount", element_index=3),
        ],
        expression="abs(total - (subtotal - discount)) <= 0.01",
        rationale="Arithmetic identity of a cart",
    )
    base.update(overrides)
    return InvariantCandidate(**base)


def test_valid_candidate_becomes_an_invariant_config():
    result = sanity_filter(_candidate(), _entries())
    assert isinstance(result, InvariantConfig)
    assert result.values["total"] == '[data-testid="cart-total"]'
    assert result.container_selector == '[data-testid="cart"]'


def test_index_is_mapped_to_the_measured_selector():
    """The model picks an index, never a selector, which is what makes selector
    hallucination structurally impossible."""
    result = sanity_filter(_candidate(), _entries())
    assert set(result.values.values()) == {
        '[data-testid="cart-total"]',
        '[data-testid="cart-subtotal"]',
        '[data-testid="cart-discount"]',
    }


def test_out_of_range_index_is_rejected():
    bad = _candidate(values=[
        CandidateValue(name="total", element_index=1),
        CandidateValue(name="ghost", element_index=99),
    ], expression="total == ghost")
    result = sanity_filter(bad, _entries())
    assert not isinstance(result, InvariantConfig)
    assert "99" in result.reason


def test_single_value_candidate_is_rejected():
    """`total >= 0` is a guess about the domain, not an invariant, and it false-positives
    on any legitimate negative."""
    bad = _candidate(values=[CandidateValue(name="total", element_index=1)],
                     expression="total >= 0")
    result = sanity_filter(bad, _entries())
    assert "at least two values" in result.reason


def test_duplicate_value_names_are_rejected():
    bad = _candidate(values=[
        CandidateValue(name="total", element_index=1),
        CandidateValue(name="total", element_index=2),
    ], expression="total == total")
    assert "duplicate" in sanity_filter(bad, _entries()).reason


def test_unused_value_is_rejected():
    """Dead weight in `values` still has to resolve at runtime — extract_values() raises
    when a selector matches nothing — so it turns into permanent inconclusiveness."""
    bad = _candidate(values=[
        CandidateValue(name="total", element_index=1),
        CandidateValue(name="subtotal", element_index=2),
        CandidateValue(name="unused", element_index=3),
    ], expression="total <= subtotal")
    assert "never used" in sanity_filter(bad, _entries()).reason


def test_unknown_name_in_expression_is_rejected():
    bad = _candidate(expression="total == subtotal - discount + mystery")
    assert "unknown names" in sanity_filter(bad, _entries()).reason


def test_unparseable_expression_is_rejected():
    assert "does not parse" in sanity_filter(_candidate(expression="total ==="), _entries()).reason


def test_comprehension_is_rejected_by_the_engines_own_validator():
    bad = _candidate(expression="sum([x for x in [total, subtotal, discount]]) > 0")
    result = sanity_filter(bad, _entries())
    assert not isinstance(result, InvariantConfig)


def test_dunder_call_is_rejected():
    bad = _candidate(
        values=[CandidateValue(name="total", element_index=1),
                CandidateValue(name="subtotal", element_index=2)],
        expression="__import__('os') and total == subtotal",
    )
    result = sanity_filter(bad, _entries())
    assert not isinstance(result, InvariantConfig)


def test_volatile_elements_never_reach_the_inventory():
    """A number that changed between two loads of the same URL is a clock or a live
    counter; any rule referencing it is a false positive on a timer."""
    class _S:
        url = "http://x/"
        numerics = [
            _element(1, '[data-testid="stable"]', "Stable"),
            _element(2, '[data-testid="clock"]', "Now", volatile=True),
        ]

    entries = build_inventory([_S()])
    assert [e.element.selector for e in entries] == ['[data-testid="stable"]']


def test_inventory_dedupes_across_states_and_renumbers():
    class _S:
        def __init__(self, url):
            self.url = url
            self.numerics = [_element(1, '[data-testid="cart-total"]', "Total"),
                             _element(2, '[data-testid="cart-subtotal"]', "Subtotal")]

    entries = build_inventory([_S("http://x/a"), _S("http://x/b")])
    assert len(entries) == 2
    assert [e.index for e in entries] == [1, 2]
    assert all(len(e.seen_in_states) == 2 for e in entries)


def test_inventory_ranks_stable_selectors_first():
    class _S:
        url = "http://x/"
        numerics = [
            _element(1, "div:nth-of-type(2) > span", "Weak", score=20, dom_index=1),
            _element(2, '[data-testid="strong"]', "Strong", score=110, dom_index=2),
        ]

    entries = build_inventory([_S()])
    assert entries[0].element.selector == '[data-testid="strong"]'


def test_grouping_is_by_container():
    entries = [
        InventoryEntry(element=_element(1, "#a", "A", container="#cart")),
        InventoryEntry(element=_element(2, "#b", "B", container="#cart")),
        InventoryEntry(element=_element(3, "#c", "C", container="#footer")),
    ]
    groups = group_inventory(entries)
    assert len(groups) == 2
    assert {len(g) for g in groups} == {1, 2}


def test_rendered_inventory_marks_repeated_groups():
    entries = [InventoryEntry(element=_element(1, '[data-testid="line"]', "Item", scoped=3))]
    assert "repeated group" in render_inventory(entries)


class _StubProvider:
    """Same shape as the fake providers in test_triage.py / test_planner.py."""

    def __init__(self, payload, fail=False):
        self.payload = payload
        self.fail = fail
        self.calls = 0

    @property
    def available(self):
        return True

    def complete(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider exploded")
        return LLMResponse(text="", parsed=self.payload, usage=Usage(), session_id=None)


def test_generate_returns_parsed_candidates():
    from qaura.llm.budget import Budget

    provider = _StubProvider(InvariantCandidateList(candidates=[_candidate()]))
    result = generate(provider, _entries(), Budget(max_calls=5))
    assert len(result) == 1
    assert result[0].name == "total_matches"


def test_generate_degrades_to_nothing_when_the_provider_fails():
    """The guardrails half of the config needs no LLM and is still worth writing, so a
    provider failure must not fail the command."""
    from qaura.llm.budget import Budget

    provider = _StubProvider(None, fail=True)
    assert generate(provider, _entries(), Budget(max_calls=5)) == []


def test_generate_respects_the_call_budget():
    from qaura.llm.budget import Budget

    provider = _StubProvider(InvariantCandidateList(candidates=[]))
    generate(provider, _entries(), Budget(max_calls=0))
    assert provider.calls == 0


def test_generate_skips_groups_too_small_to_relate():
    from qaura.llm.budget import Budget

    provider = _StubProvider(InvariantCandidateList(candidates=[]))
    single = [InventoryEntry(element=_element(1, "#only", "Only", container="#a"))]
    generate(provider, single, Budget(max_calls=5))
    assert provider.calls == 0
