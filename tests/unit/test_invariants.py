import pytest
from playwright.async_api import async_playwright

from qaura.config import InvariantConfig
from qaura.core.invariants import InvariantError, check, evaluate_expression, parse_number, validate_invariant


# --- parse_number --------------------------------------------------------------

def test_parse_number_plain():
    assert parse_number("42") == 42.0


def test_parse_number_currency():
    assert parse_number("$1,234.56") == 1234.56


def test_parse_number_negative():
    assert parse_number("-3.5") == -3.5


def test_parse_number_embedded_in_text():
    assert parse_number("Total: $27.00 USD") == 27.00


def test_parse_number_none_when_no_digits():
    assert parse_number("no number here") is None


# --- evaluate_expression: correctness -------------------------------------------

def test_evaluate_expression_simple_equality_true():
    assert evaluate_expression("total == subtotal", {"total": 10.0, "subtotal": 10.0}) is True


def test_evaluate_expression_simple_equality_false():
    assert evaluate_expression("total == subtotal", {"total": 10.0, "subtotal": 12.0}) is False


def test_evaluate_expression_sum_over_list():
    values = {"total": 30.0, "line_items": [10.0, 10.0, 10.0]}
    assert evaluate_expression("total == sum(line_items)", values) is True


def test_evaluate_expression_sum_mismatch_is_false():
    values = {"total": 25.0, "line_items": [10.0, 10.0, 10.0]}
    assert evaluate_expression("total == sum(line_items)", values) is False


def test_evaluate_expression_le_comparison():
    assert evaluate_expression("total <= subtotal", {"total": 9.0, "subtotal": 10.0}) is True
    assert evaluate_expression("total <= subtotal", {"total": 11.0, "subtotal": 10.0}) is False


def test_evaluate_expression_boolean_and():
    values = {"total": 10.0, "subtotal": 10.0, "min_order": 5.0}
    assert evaluate_expression("total == subtotal and total >= min_order", values) is True


# --- evaluate_expression: safety (this is the part that actually matters) -------

def test_evaluate_expression_rejects_attribute_access():
    with pytest.raises(InvariantError):
        evaluate_expression("total.__class__", {"total": 1.0})


def test_evaluate_expression_rejects_dunder_import():
    with pytest.raises(InvariantError):
        evaluate_expression("__import__('os')", {})


def test_evaluate_expression_rejects_list_comprehension():
    with pytest.raises(InvariantError):
        evaluate_expression("sum([x for x in [1,2,3]])", {})


def test_evaluate_expression_rejects_unknown_function_call():
    with pytest.raises(InvariantError):
        evaluate_expression("eval('1')", {})


def test_evaluate_expression_rejects_unknown_name():
    with pytest.raises(InvariantError):
        evaluate_expression("total == mystery_value", {"total": 1.0})


def test_evaluate_expression_rejects_lambda():
    with pytest.raises(InvariantError):
        evaluate_expression("(lambda: 1)()", {})


def test_evaluate_expression_rejects_syntax_error():
    with pytest.raises(InvariantError):
        evaluate_expression("total ==", {"total": 1.0})


def test_evaluate_expression_sum_tolerates_single_scalar_value():
    # Real bug found live against buggy_app: a cart with exactly one line item makes
    # extract_values() collapse "line_items" to a bare float (single selector match),
    # and sum() on a plain float used to crash with "not iterable". sum() of a single
    # value should just be that value.
    values = {"total": 10.0, "line_items": 10.0}
    assert evaluate_expression("total == sum(line_items)", values) is True


def test_evaluate_expression_allows_whitelisted_builtins():
    values = {"a": [3.0, 1.0, 2.0]}
    assert evaluate_expression("max(a) == 3", values) is True
    assert evaluate_expression("min(a) == 1", values) is True
    assert evaluate_expression("len(a) == 3", values) is True
    assert evaluate_expression("round(2.6) == 3", values) is True
    assert evaluate_expression("abs(-5) == 5", values) is True


# --- extract_values + check(): needs a real page ---------------------------------

CART_HTML = """<!doctype html><html><body>
<div data-testid="cart">
  <span data-testid="cart-subtotal">$30.00</span>
  <span data-testid="cart-total">{total}</span>
  <div><span data-testid="line-total">$10.00</span></div>
  <div><span data-testid="line-total">$10.00</span></div>
  <div><span data-testid="line-total">$10.00</span></div>
</div>
</body></html>"""


@pytest.fixture
async def page():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        pg = await browser.new_page()
        yield pg
        await browser.close()


async def test_check_passes_when_total_matches_line_items(page):
    await page.set_content(CART_HTML.format(total="$30.00"))
    invariant = InvariantConfig(
        name="cart_total_matches_line_items", description="d",
        container_selector="[data-testid=cart]",
        values={"total": "[data-testid=cart-total]", "line_items": "[data-testid=line-total]"},
        expression="total == sum(line_items)",
    )
    finding = await check(page, invariant, page.url, [], "heuristic")
    assert finding is None


async def test_check_flags_violation_when_total_is_stale(page):
    # this is the plan's canonical example: coupon applied, quantity changed, total
    # never recomputed — line items sum to 30 but the displayed total says 40
    await page.set_content(CART_HTML.format(total="$40.00"))
    invariant = InvariantConfig(
        name="cart_total_matches_line_items", description="Cart total must match line items",
        container_selector="[data-testid=cart]",
        values={"total": "[data-testid=cart-total]", "line_items": "[data-testid=line-total]"},
        expression="total == sum(line_items)",
    )
    finding = await check(page, invariant, page.url, [], "heuristic")
    assert finding is not None
    assert finding.detector == "invariant"
    assert finding.severity.value == "high"
    assert "cart_total_matches_line_items" in finding.title


async def test_check_discount_never_increases_total(page):
    await page.set_content(CART_HTML.format(total="$35.00"))  # 35 > subtotal 30 -- violation
    invariant = InvariantConfig(
        name="discount_never_increases_total", description="d",
        container_selector="[data-testid=cart]",
        values={"total": "[data-testid=cart-total]", "subtotal": "[data-testid=cart-subtotal]"},
        expression="total <= subtotal",
    )
    finding = await check(page, invariant, page.url, [], "heuristic")
    assert finding is not None


SINGLE_ITEM_CART_HTML = """<!doctype html><html><body>
<div data-testid="cart">
  <span data-testid="cart-subtotal">$10.00</span>
  <span data-testid="line-total">$10.00</span>
</div>
</body></html>"""


async def test_check_subtotal_matches_single_line_item(page):
    # Same shape as the live bug found against buggy_app: exactly ONE line item, so
    # extract_values() collapses line_items to a scalar float rather than a list —
    # this is what actually exercises _tolerant_sum through the real check() path,
    # not just evaluate_expression() directly.
    await page.set_content(SINGLE_ITEM_CART_HTML)
    invariant = InvariantConfig(
        name="subtotal_matches_line_items", description="d",
        container_selector="[data-testid=cart]",
        values={"subtotal": "[data-testid=cart-subtotal]", "line_items": "[data-testid=line-total]"},
        expression="subtotal == sum(line_items)",
    )
    finding = await check(page, invariant, page.url, [], "heuristic")
    assert finding is None  # $10 subtotal == sum of the single $10 line item — holds


async def test_check_returns_none_when_container_not_present():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        pg = await browser.new_page()
        await pg.set_content("<html><body><p>no cart here</p></body></html>")
        invariant = InvariantConfig(
            name="cart_total_matches_line_items", description="d",
            container_selector="[data-testid=cart]",
            values={"total": "[data-testid=cart-total]"},
            expression="total > 0",
        )
        finding = await check(pg, invariant, pg.url, [], "heuristic")
        assert finding is None  # inconclusive, not a violation
        await browser.close()


# --- runtime errors and load-time validation -------------------------------------

def test_evaluate_expression_division_by_zero_raises_invariant_error():
    with pytest.raises(InvariantError, match="could not be evaluated"):
        evaluate_expression("total / count > 1", {"total": 1.0, "count": 0.0})


def test_evaluate_expression_list_compared_to_number_raises_invariant_error():
    with pytest.raises(InvariantError, match="could not be evaluated"):
        evaluate_expression("line_items > 0", {"line_items": [1.0, 2.0]})


@pytest.mark.parametrize("expression", ["len([0] * 1000000000) > 0", "len(1000000000 * [0]) > 0", "len((0,) * 99) > 0"])
def test_evaluate_expression_rejects_sequence_repetition(expression):
    with pytest.raises(InvariantError, match="repetition"):
        evaluate_expression(expression, {})


def test_validate_invariant_accepts_known_names():
    validate_invariant(InvariantConfig(name="n", description="d", values={"a": "#a"}, expression="a > 0"))


def test_validate_invariant_rejects_unknown_name():
    with pytest.raises(InvariantError, match="unknown name"):
        validate_invariant(InvariantConfig(name="n", description="d", values={"a": "#a"}, expression="b > 0"))


def test_validate_invariant_rejects_bad_syntax():
    with pytest.raises(InvariantError, match="syntax"):
        validate_invariant(InvariantConfig(name="n", description="d", values={"a": "#a"}, expression="a >"))


async def test_check_is_inconclusive_when_expression_divides_by_zero(page):
    await page.set_content('<span id="t">$10</span><span id="c">0</span>')
    invariant = InvariantConfig(
        name="average_above_one", description="d",
        values={"t": "#t", "c": "#c"}, expression="t / c > 1",
    )
    assert await check(page, invariant, page.url, [], "heuristic") is None
