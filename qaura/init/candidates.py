"""Invariant synthesis: propose business rules from the numbers on a page, then throw
away the ones that don't survive contact with the evidence.

The generation half is the easy half. The hard requirement is that a wrong invariant is
worse than no invariant: it becomes a permanent false positive that fires on every future
run, and the person reading that report has no way to tell it apart from a real bug. So
generation is followed by two filters that between them reject far more than they keep.

The first is a pure sanity pass that runs the config through the same restricted-AST
validator core/invariants.py uses at runtime, so nothing the engine would refuse can ever
be written. The second replays each candidate against the HTML actually captured during
recon and keeps only what genuinely held.

Validation runs offline, against `page.set_content(snapshot)` in a request-blocked
context, rather than by re-crawling. That makes it deterministic, free, repeatable, and
testable against a fixed HTML string, and it means validating twenty candidates costs
zero extra requests to the target.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from playwright.async_api import Page

from qaura.browser.numerics import NumericElement
from qaura.config import InvariantConfig
from qaura.core.invariants import InvariantError, evaluate_expression, extract_values, validate_invariant
from qaura.llm.base import LLMProvider, Tier
from qaura.llm.budget import Budget, BudgetExceeded
from qaura.llm.schemas import InvariantCandidate, InvariantCandidateList

_log = logging.getLogger(__name__)

MAX_INVENTORY_ENTRIES = 60
MAX_GROUPS = 3

SYSTEM_PROMPT = """You propose business-rule invariants for an automated QA agent.

You will be given an inventory of numbers found on one page of a web app. Each entry has
an index (n1, n2, ...), a label, the raw text, the parsed value, and how many elements
its selector matches.

Propose invariants: relationships between these numbers that must ALWAYS hold if the app
is working correctly. A good invariant catches a bug that nothing else would notice — a
total that goes stale after a discount is applied, a count that stops matching the rows
it counts. It does not crash, log an error, or fail a network request, so only a business
rule can catch it.

Rules you must follow:
- Reference numbers ONLY by their inventory index. Never write a CSS selector.
- Every invariant needs at least two values. A single-value range check like `total >= 0`
  is not an invariant; it is a guess about the domain and it will produce false alarms.
- `expression` may use only: the value names you declared, the operators
  + - * / < <= > >= == != and or not, and the functions sum, min, max, abs, len, round.
  No attribute access, no comprehensions, no other function calls, no keyword arguments.
- An entry whose selector matches several elements is a repeated group (line items, table
  rows). Referencing it gives you a LIST, so use sum(name) over it.
- Compare computed numbers with a TOLERANCE, never with ==. Money is stored in floats and
  `total == subtotal - discount` will fail on rounding alone. Write
  `abs(total - (subtotal - discount)) <= 0.01` instead.
- Propose only rules you are confident about from the labels. If the page does not show a
  clear arithmetic relationship, return an empty list. Proposing nothing is a correct and
  useful answer; a wrong rule becomes a permanent false alarm.
"""


@dataclass
class InventoryEntry:
    element: NumericElement
    seen_in_states: list[str] = field(default_factory=list)
    values_seen: list[float] = field(default_factory=list)

    @property
    def index(self) -> int:
        return self.element.index


@dataclass
class RejectReason:
    candidate: InvariantCandidate
    reason: str


@dataclass
class CandidateVerdict:
    invariant: InvariantConfig
    status: Literal["accepted", "rejected_violated", "unverified"]
    conclusive_count: int = 0
    holds_count: int = 0
    sample_values: dict = field(default_factory=dict)
    violating_state_url: str | None = None
    reason: str = ""
    selector_kinds: dict[str, str] = field(default_factory=dict)
    selector_scores: dict[str, int] = field(default_factory=dict)

    @property
    def confidence(self) -> str:
        if self.status != "accepted":
            return "low"
        if self.conclusive_count >= 3:
            return "high"
        if self.conclusive_count == 2:
            return "medium"
        return "low"

    @property
    def durability(self) -> str:
        """Weakest link across the rule's selectors — a rule is only as stable as its
        most fragile reference."""
        if not self.selector_scores:
            return "unknown"
        worst = min(self.selector_scores.values())
        if worst >= 95:
            return "durable"
        if worst >= 55:
            return "moderate"
        return "fragile"


def build_inventory(states) -> list[InventoryEntry]:
    """Collapses every state's numeric scan into one deduped, ranked inventory.

    Volatile entries are dropped rather than demoted. A number that changed between two
    loads of the same URL is a clock or a live counter, and any rule referencing it is a
    false positive waiting for the next tick — there is no score low enough to make that
    acceptable, so it does not reach the model at all.
    """
    by_key: dict[tuple[str | None, str], InventoryEntry] = {}

    for state in states:
        for element in state.numerics:
            if element.volatile:
                continue
            key = (element.container_selector, element.selector)
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = InventoryEntry(
                    element=element, seen_in_states=[state.url], values_seen=[element.number]
                )
                continue
            existing.seen_in_states.append(state.url)
            existing.values_seen.append(element.number)
            if element.selector_score > existing.element.selector_score:
                existing.element = element

    entries = sorted(
        by_key.values(),
        key=lambda e: (
            -e.element.selector_score,
            e.element.container_selector or "",
            e.element.dom_index,
        ),
    )[:MAX_INVENTORY_ENTRIES]

    # Renumber so handles are contiguous and stable for the prompt.
    for position, entry in enumerate(entries, start=1):
        entry.element.index = position
    return entries


def group_inventory(entries: list[InventoryEntry]) -> list[list[InventoryEntry]]:
    """One call per container. Numbers inside the same container are the ones plausibly
    related to each other, and scoping the prompt keeps the model from inventing a rule
    linking a cart total to a footer copyright year."""
    groups: dict[str | None, list[InventoryEntry]] = {}
    for entry in entries:
        groups.setdefault(entry.element.container_selector, []).append(entry)
    ordered = sorted(groups.values(), key=lambda g: -max(e.element.selector_score for e in g))
    return ordered[:MAX_GROUPS]


def render_inventory(entries: list[InventoryEntry]) -> str:
    lines = ["Numbers found on this page:"]
    lines.extend(f"  {entry.element.to_prompt_line()}" for entry in entries)
    return "\n".join(lines)


def generate(
    provider: LLMProvider, entries: list[InventoryEntry], budget: Budget
) -> list[InvariantCandidate]:
    """Asks the model for candidates, one call per container group.

    Any provider failure degrades to zero candidates rather than failing the command: the
    guardrails and personas half of the generated config needs no LLM at all and is still
    worth writing.
    """
    if not entries:
        return []

    out: list[InvariantCandidate] = []
    for group in group_inventory(entries):
        if len(group) < 2:
            continue
        try:
            budget.check()
        except BudgetExceeded:
            _log.debug("invariant candidate budget exhausted")
            break
        try:
            response = provider.complete(
                tier=Tier.INVARIANT_CANDIDATES,
                system=SYSTEM_PROMPT,
                input=render_inventory(group),
                schema=InvariantCandidateList,
            )
        except Exception:
            _log.warning("invariant candidate generation failed", exc_info=True)
            continue
        try:
            budget.record(Tier.INVARIANT_CANDIDATES, response)
        except Exception:
            _log.debug("budget bookkeeping failed", exc_info=True)
        parsed = response.parsed
        if isinstance(parsed, InvariantCandidateList):
            out.extend(parsed.candidates)
    return out


def sanity_filter(
    candidate: InvariantCandidate, entries: list[InventoryEntry]
) -> InvariantConfig | RejectReason:
    """Everything that can be rejected without a browser. The last check is the important
    one: it runs the proposed expression through the very validator core/invariants.py
    uses, so a config this function accepts cannot later blow up at runtime."""
    by_index = {entry.index: entry for entry in entries}

    if len(candidate.values) < 2:
        return RejectReason(candidate, "needs at least two values; a one-value check is a guess")

    names = [v.name for v in candidate.values]
    if len(set(names)) != len(names):
        return RejectReason(candidate, "duplicate value names")

    values: dict[str, str] = {}
    for value in candidate.values:
        entry = by_index.get(value.element_index)
        if entry is None:
            return RejectReason(candidate, f"element index {value.element_index} is not in the inventory")
        values[value.name] = entry.element.selector

    # A declared-but-unused name still has to resolve at runtime, and extract_values()
    # raises if its selector matches nothing — so dead weight in `values` turns into
    # permanent inconclusiveness rather than a harmless no-op.
    try:
        import ast

        tree = ast.parse(candidate.expression, mode="eval")
    except SyntaxError as e:
        return RejectReason(candidate, f"expression does not parse: {e}")

    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    unused = set(values) - used
    if unused:
        return RejectReason(candidate, f"values never used in the expression: {sorted(unused)}")

    from qaura.core.invariants import _ALLOWED_BUILTINS

    unknown = used - set(values) - set(_ALLOWED_BUILTINS)
    if unknown:
        return RejectReason(candidate, f"expression references unknown names: {sorted(unknown)}")

    container = None
    if candidate.container_index is not None:
        entry = by_index.get(candidate.container_index)
        container = entry.element.container_selector if entry else None
    if container is None:
        containers = {by_index[v.element_index].element.container_selector for v in candidate.values}
        container = containers.pop() if len(containers) == 1 else None

    config = InvariantConfig(
        name=candidate.name,
        description=candidate.description,
        container_selector=container,
        values=values,
        expression=candidate.expression,
    )

    # Structural check only; plugging in 1.0 for every value rejected valid rules like a / (b - c)
    try:
        validate_invariant(config)
    except InvariantError as e:
        return RejectReason(candidate, f"expression rejected by the invariant engine: {e}")

    return config


async def validate(page: Page, invariants: list[InvariantConfig], snapshots) -> list[CandidateVerdict]:
    """Replays each candidate against every captured snapshot.

    `page` must belong to a context with install_offline_routes() applied — set_content()
    on real-world HTML will otherwise try to fetch every script, font and tracking pixel
    the page referenced, which is both slow and a second round of traffic to a target we
    promised to only read.
    """
    verdicts: list[CandidateVerdict] = []

    for invariant in invariants:
        conclusive = 0
        holds = 0
        sample: dict = {}
        violating_url: str | None = None
        last_error = ""

        for snapshot in snapshots:
            try:
                await page.set_content(snapshot.html, wait_until="domcontentloaded")
            except Exception:
                continue
            try:
                values = await extract_values(page, invariant)
                result = evaluate_expression(invariant.expression, values)
            except InvariantError as e:
                last_error = str(e)
                continue
            except Exception as e:
                last_error = str(e)
                continue

            conclusive += 1
            if not sample:
                sample = dict(values)
            if result:
                holds += 1
            elif violating_url is None:
                violating_url = snapshot.url
                sample = dict(values)

        if conclusive == 0:
            verdicts.append(CandidateVerdict(
                invariant=invariant, status="unverified",
                reason=last_error or "never evaluable on any captured page",
            ))
        elif holds < conclusive:
            verdicts.append(CandidateVerdict(
                invariant=invariant, status="rejected_violated",
                conclusive_count=conclusive, holds_count=holds,
                sample_values=sample, violating_state_url=violating_url,
                reason=f"held on {holds} of {conclusive} page(s) where it could be evaluated",
            ))
        else:
            verdicts.append(CandidateVerdict(
                invariant=invariant, status="accepted",
                conclusive_count=conclusive, holds_count=holds, sample_values=sample,
                reason=f"held on all {conclusive} page(s) where it could be evaluated",
            ))

    return verdicts


def annotate_durability(verdicts: list[CandidateVerdict], entries: list[InventoryEntry]) -> None:
    """Records each rule's selector kinds and scores so the emitted config can say which
    rules are load-bearing and which are provisional. A broken selector is already safe —
    extract_values() raises and check() treats it as inconclusive — but it is silent, and
    an invariant that quietly stopped applying is indistinguishable from one that passes."""
    by_selector = {e.element.selector: e.element for e in entries}
    for verdict in verdicts:
        for name, selector in verdict.invariant.values.items():
            element = by_selector.get(selector)
            if element is not None:
                verdict.selector_kinds[name] = element.selector_kind
                verdict.selector_scores[name] = element.selector_score
