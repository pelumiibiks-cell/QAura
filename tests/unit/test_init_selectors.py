"""Selector scoring and rejection — pure, no browser.

This is where the "will this config still work after a redeploy" question is actually
decided, so the rejection tables are spelled out as concrete strings from real
frameworks rather than as properties. A regex that looks obviously correct and silently
stops matching `sc-bdVaJa` is exactly the failure this file exists to catch.
"""
import pytest

from qaura.browser.numerics import (
    build_numeric_elements,
    choose_selector,
    is_hashy_class,
    looks_generated_identifier,
    score_candidate,
)


@pytest.mark.parametrize("cls", [
    "css-1x2y3z",              # emotion
    "sc-bdVaJa",               # styled-components
    "Cart_total__2xY9k",       # CSS modules
    "styles_wrapper__a1b2c",
    "text-sm", "px-4", "bg-blue-500", "flex", "items-center",  # Tailwind utilities
    "w-[calc(100%-2rem)]",     # Tailwind arbitrary value
    "md:flex", "hover:underline",  # Tailwind variants
    "container", "sr-only", "row", "btn",  # Bootstrap-ish structural noise
    "a1b2c3d4e5",              # high-entropy token
    "",
])
def test_rejects_unstable_classes(cls):
    assert is_hashy_class(cls), f"{cls!r} should be rejected as unstable"


@pytest.mark.parametrize("cls", [
    "cart-total", "line-item-price", "checkout-summary", "order_total",
    "invoice-line", "product-price",
])
def test_accepts_semantic_classes(cls):
    assert not is_hashy_class(cls), f"{cls!r} should be usable"


@pytest.mark.parametrize("cls,expected_hashy", [
    # Tailwind's utility namespace collides with ordinary e-commerce vocabulary, so the
    # prefix alone cannot decide. What follows it has to look like a utility VALUE.
    ("order-1", True), ("order-first", True), ("order-total", False), ("order-summary", False),
    ("row-2", True), ("row-count", False),
    ("col-4", True), ("col-span-2", True), ("col-heading", False),
    ("line-through", False), ("line-item-price", False),
    ("content-center", True), ("content-body", False),
    ("text-sm", True), ("text-red-500", True), ("text-invoice", False),
    ("w-full", True), ("w-widget", False),
    ("border-dashed", True), ("border-invoice", False),
])
def test_utility_prefix_needs_a_utility_value(cls, expected_hashy):
    assert is_hashy_class(cls) is expected_hashy, (
        f"{cls!r}: expected hashy={expected_hashy}"
    )


@pytest.mark.parametrize("token", [
    ":r3:",                                       # React 18 useId
    "radix-abc123", "headlessui-menu-1", "ember1234", "mat-input-0",
    "550e8400-e29b-41d4-a716-446655440000",       # UUID
    "12345",                                      # bare numeric
    "deadbeefcafe",                               # hex run
    "a1b2c3d4e5f6",                               # mixed-id shape
])
def test_rejects_generated_ids(token):
    assert looks_generated_identifier(token)


@pytest.mark.parametrize("token", ["cart-total", "checkout", "user_name", "main-nav"])
def test_accepts_authored_ids(token):
    assert not looks_generated_identifier(token)


def _cand(sel, kind, count=1, scoped=None, raw=None):
    entry = {"sel": sel, "kind": kind, "count": count, "scoped": count if scoped is None else scoped}
    if raw is not None:
        entry["raw"] = raw
    return entry


def test_prefers_testid_over_id_over_class():
    candidates = [
        _cand("#cart-total", "id", raw="cart-total"),
        _cand(".cart-total", "class", raw="cart-total"),
        _cand('[data-testid="cart-total"]', "testid"),
    ]
    chosen = choose_selector(candidates, has_label=True, shared_label=False)
    assert chosen.selector == '[data-testid="cart-total"]'
    assert chosen.kind == "testid"


def test_generated_id_is_rejected_leaving_the_class():
    candidates = [_cand("#:r3:", "id", raw=":r3:"), _cand(".order-total", "class", raw="order-total")]
    chosen = choose_selector(candidates, has_label=True, shared_label=False)
    assert chosen.selector == ".order-total"


def test_falls_back_to_structural_when_nothing_else_survives():
    candidates = [
        _cand(".css-1x2y3z", "class", raw="css-1x2y3z"),
        _cand("#ember42", "id", raw="ember42"),
        _cand("section:nth-of-type(2) > span", "structural"),
    ]
    chosen = choose_selector(candidates, has_label=True, shared_label=False)
    assert chosen.kind == "structural"


def test_multi_match_with_a_label_is_demoted():
    """A labelled value that several elements answer to means the selector is too coarse
    to address it, so an invariant built on it would compare the wrong numbers."""
    unique = score_candidate(_cand('[data-testid="x"]', "testid", count=1),
                             has_label=True, shared_label=False)
    coarse = score_candidate(_cand('[data-testid="x"]', "testid", count=4, scoped=4),
                             has_label=True, shared_label=False)
    assert coarse.score < unique.score


def test_multi_match_with_a_shared_label_is_promoted():
    """The repeated-group case: several line items all labelled the same way is exactly
    what makes sum(line_items) possible, so it must not be penalized."""
    shared = score_candidate(_cand('[data-testid="line-total"]', "testid", count=3, scoped=3),
                             has_label=True, shared_label=True)
    unshared = score_candidate(_cand('[data-testid="line-total"]', "testid", count=3, scoped=3),
                               has_label=True, shared_label=False)
    assert shared.score > unshared.score
    assert shared.usable


def test_non_unique_non_repeating_class_is_rejected_outright():
    result = score_candidate(_cand(".value", "class", count=5, scoped=5, raw="value"),
                             has_label=True, shared_label=False)
    assert not result.usable


def test_selector_matching_nothing_is_rejected():
    result = score_candidate(_cand("#gone", "id", count=0, raw="gone"),
                             has_label=False, shared_label=False)
    assert not result.usable
    assert "matches nothing" in result.rejected_reason


def test_build_numeric_elements_end_to_end():
    payload = [{
        "ownText": "$1,234.56",
        "tag": "span",
        "candidates": [
            _cand('[data-testid="cart-total"]', "testid"),
            _cand("#cart-total", "id", raw="cart-total"),
        ],
        "container": {"sel": '[data-testid="cart"]', "kind": "testid"},
        "label": "Total",
        "labelSource": "prev-sibling-text",
        "domIndex": 12,
    }]
    elements = build_numeric_elements(payload)
    assert len(elements) == 1
    element = elements[0]
    assert element.handle == "n1"
    assert element.selector == '[data-testid="cart-total"]'
    assert element.number == pytest.approx(1234.56)  # comma-formatted currency parses
    assert element.container_selector == '[data-testid="cart"]'
    assert element.label == "Total"


def test_element_without_a_parseable_number_is_dropped():
    payload = [{
        "ownText": "no digits here",
        "tag": "span",
        "candidates": [_cand('[data-testid="x"]', "testid")],
        "container": None, "label": None, "labelSource": None, "domIndex": 1,
    }]
    assert build_numeric_elements(payload) == []


def test_empty_label_is_normalized_to_none():
    payload = [{
        "ownText": "10",
        "tag": "span",
        "candidates": [_cand('[data-testid="x"]', "testid")],
        "container": None, "label": "   ", "labelSource": "prev-sibling-text", "domIndex": 1,
    }]
    element = build_numeric_elements(payload)[0]
    assert element.label is None
    assert element.label_source is None


def test_container_equal_to_own_selector_is_dropped():
    """Otherwise extract_values() would scope the selector to the element it selects."""
    payload = [{
        "ownText": "5",
        "tag": "span",
        "candidates": [_cand('[data-testid="x"]', "testid")],
        "container": {"sel": '[data-testid="x"]', "kind": "testid"},
        "label": "Count", "labelSource": "th", "domIndex": 3,
    }]
    assert build_numeric_elements(payload)[0].container_selector is None
