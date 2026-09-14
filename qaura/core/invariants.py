"""Declarative invariant engine — plan design decision #4, the only detector that
catches a bug like "checkout total goes stale after a coupon is applied": nothing
crashes, no console error, no failed network call, and even the LLM's own stated
`expectation` for whatever action triggered it might still look satisfied (the page
DID update, it just computed the wrong number). A business rule is the only thing
that can catch that class of bug.

Two pieces: pulling named numeric values off the live page via CSS selectors
(`extract_values`), and evaluating a restricted expression over them
(`evaluate_expression` / the `_SafeEval` AST walker) — see config.py:InvariantConfig
for why the expression language is deliberately narrow rather than arbitrary Python.
"""
from __future__ import annotations

import ast
import re

from playwright.async_api import Page

from qaura.config import InvariantConfig
from qaura.reporting.models import Evidence, Finding, ReproStep, Severity

# Parses a number out of currency/formatted text: "$1,234.56" -> 1234.56, "-3" -> -3.0,
# "12%" -> 12.0. Deliberately permissive on surrounding text (symbols, commas,
# whitespace) since target apps format numbers all sorts of ways and the invariant
# author shouldn't have to fight that in their selector.
_NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*")


class InvariantError(RuntimeError):
    """Raised for configuration problems (bad expression syntax, disallowed
    construct, a referenced name with no matching element) — distinct from the
    invariant simply evaluating to False, which is a Finding, not an error."""


def parse_number(text: str) -> float | None:
    match = _NUMBER_RE.search(text.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


async def extract_values(page: Page, invariant: InvariantConfig) -> dict[str, float | list[float]]:
    """For each name -> selector in invariant.values: one matching element yields a
    single float, multiple yield a list of floats. Elements with no parseable number
    are skipped (not zero-filled — a missing/unparseable value should make the
    invariant inconclusive, not silently wrong)."""
    root = page
    if invariant.container_selector:
        container = page.locator(invariant.container_selector).first
        if await container.count() == 0:
            raise InvariantError(f"container_selector {invariant.container_selector!r} matched nothing")
        root = container

    result: dict[str, float | list[float]] = {}
    for name, selector in invariant.values.items():
        locator = root.locator(selector) if invariant.container_selector else page.locator(selector)
        count = await locator.count()
        if count == 0:
            raise InvariantError(f"values[{name!r}] selector {selector!r} matched nothing")
        texts = [await locator.nth(i).text_content() or "" for i in range(count)]
        numbers = [n for n in (parse_number(t) for t in texts) if n is not None]
        if not numbers:
            raise InvariantError(f"values[{name!r}] matched {count} element(s) but none had a parseable number")
        result[name] = numbers[0] if count == 1 else numbers
    return result


# --- restricted expression evaluator -----------------------------------------------


def _tolerant_sum(x):
    """extract_values() collapses a single-element selector match to a bare float
    (convenient for scalars like `total`), but a `values` entry conceptually meant
    as a list — like `line_items` — collapses the same way when a page only has one
    line item. `sum(30.0)` then crashes with "not iterable", found live against the
    buggy_app fixture the moment it had exactly one cart item. `sum` of a single
    value is just that value, so tolerate it instead of forcing every invariant
    author to special-case "what if there's only one.\""""
    if isinstance(x, (list, tuple)):
        return sum(x)
    return x


_ALLOWED_BUILTINS = {"sum": _tolerant_sum, "min": min, "max": max, "abs": abs, "len": len, "round": round}
_ALLOWED_NODES = (
    ast.Expression, ast.Compare, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Call,
    ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple,
    ast.And, ast.Or, ast.Not,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd,
)


class _SafeEval(ast.NodeVisitor):
    """Walks the AST once to reject anything not in _ALLOWED_NODES / not a whitelisted
    call target / not a known variable, THEN evaluates via a second pass using Python's
    own eval() with a locked-down namespace (no builtins at all except the whitelist).
    The AST walk is what actually makes this safe — eval() with an empty __builtins__
    plus a pre-validated AST containing nothing but comparisons/arithmetic/whitelisted
    calls cannot do attribute access, imports, comprehensions, or arbitrary calls,
    because those node types were already rejected before eval() ever runs."""

    def __init__(self, allowed_names: set[str], list_names: set[str] | None = None) -> None:
        self.allowed_names = allowed_names
        self.list_names = list_names or set()

    def _is_sequence(self, node: ast.AST) -> bool:
        return isinstance(node, (ast.List, ast.Tuple)) or (isinstance(node, ast.Name) and node.id in self.list_names)

    def generic_visit(self, node: ast.AST) -> None:
        if not isinstance(node, _ALLOWED_NODES):
            raise InvariantError(f"disallowed expression construct: {type(node).__name__}")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult) and (
            self._is_sequence(node.left) or self._is_sequence(node.right)
        ):
            # [0] * 10**9 exhausts memory, and repeating a list is never a meaningful business rule
            raise InvariantError("sequence repetition (e.g. [0] * n) is not allowed")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_BUILTINS:
                raise InvariantError("only sum/min/max/abs/len/round may be called")
            if node.keywords:
                raise InvariantError("keyword arguments are not allowed")
        if isinstance(node, ast.Name) and node.id not in self.allowed_names and node.id not in _ALLOWED_BUILTINS:
            raise InvariantError(f"unknown name in expression: {node.id!r}")
        super().generic_visit(node)


def _parse(expression: str) -> ast.Expression:
    try:
        return ast.parse(expression, mode="eval")
    except SyntaxError as e:
        raise InvariantError(f"invalid expression syntax: {e}") from e


def validate_invariant(invariant: InvariantConfig) -> None:
    """Syntax and name checks that need no page values, so a broken rule fails when the config loads."""
    _SafeEval(allowed_names=set(invariant.values)).visit(_parse(invariant.expression))


def evaluate_expression(expression: str, values: dict[str, float | list[float]]) -> bool:
    tree = _parse(expression)
    list_names = {name for name, value in values.items() if isinstance(value, list)}
    _SafeEval(allowed_names=set(values), list_names=list_names).visit(tree)

    code = compile(tree, filename="<invariant>", mode="eval")
    try:
        result = eval(code, {"__builtins__": {}}, {**_ALLOWED_BUILTINS, **values})  # noqa: S307 — pre-validated AST, see _SafeEval
    except (ArithmeticError, TypeError, ValueError) as e:
        # e.g. a zero on the page used as a divisor, or a list compared to a number
        raise InvariantError(f"expression could not be evaluated: {type(e).__name__}: {e}") from e
    return bool(result)


# --- top-level check ------------------------------------------------------------


async def check(
    page: Page,
    invariant: InvariantConfig,
    url: str,
    repro_steps: list[ReproStep],
    persona: str,
) -> Finding | None:
    """Returns a Finding if the invariant is violated, None if it holds OR if it
    couldn't be evaluated on this page (missing elements — most invariants only apply
    on specific pages, e.g. a cart invariant is inconclusive everywhere except the
    cart page, and that's expected, not an error worth surfacing)."""
    try:
        values = await extract_values(page, invariant)
        holds = evaluate_expression(invariant.expression, values)
    except InvariantError:
        return None  # inconclusive on this page — not a violation, not a bug report

    if holds:
        return None

    readable_values = ", ".join(f"{k}={v}" for k, v in values.items())
    return Finding(
        title=f"Invariant violated: {invariant.name}",
        detector="invariant",
        severity=Severity.HIGH,
        persona=persona,
        url=url,
        description=(
            f"{invariant.description} — expression '{invariant.expression}' was False. "
            f"Values observed: {readable_values}"
        ),
        repro_steps=list(repro_steps),
        evidence=Evidence(),
    )
