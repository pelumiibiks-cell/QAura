"""State fingerprinting and the exploration state graph. Plan design decision #2:
crawl a *state graph*, not a URL list — /product/1 and /product/2 collapse to one state
if their interactive-element signature matches, so coverage is measured in distinct UI
states rather than inflated by every possible ID in the URL.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from qaura.browser.observe import ElementInfo, PageModel

# A path segment gets replaced with a placeholder if it looks like an opaque
# identifier rather than a meaningful route name. Deliberately conservative — a false
# "this is an id" call just means two genuinely different pages collapse into one
# state, which is recoverable (the graph still explores both, just tags them the
# same); a false "this is NOT an id" call means state explosion, which is worse for
# coverage tracking. Order matters: check UUID before hex-run before numeric.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_NUMERIC_RE = re.compile(r"^\d+$")
_HEX_RUN_RE = re.compile(r"^[0-9a-f]{8,}$", re.IGNORECASE)
_MIXED_ID_RE = re.compile(r"^(?=.*\d)(?=.*[a-zA-Z])[0-9a-zA-Z_-]{10,}$")


def normalize_path(path: str) -> str:
    segments = [s for s in path.split("/")]
    normalized = []
    for seg in segments:
        if not seg:
            normalized.append(seg)
            continue
        if _UUID_RE.match(seg) or _NUMERIC_RE.match(seg) or _HEX_RUN_RE.match(seg) or _MIXED_ID_RE.match(seg):
            normalized.append("{id}")
        else:
            normalized.append(seg)
    return "/".join(normalized)


def url_template(url: str) -> str:
    """Origin + normalized path. Query string and fragment are dropped — they
    typically encode filters/pagination/state that the element signature already
    captures better than a template could."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{normalize_path(parts.path)}"


@dataclass(frozen=True)
class StateFingerprint:
    template: str
    element_signature: str

    def key(self) -> str:
        return hashlib.sha256(f"{self.template}#{self.element_signature}".encode()).hexdigest()[:16]

    def __str__(self) -> str:
        return f"{self.template} [{self.element_signature}]"


def fingerprint(page_model: PageModel) -> StateFingerprint:
    return StateFingerprint(
        template=url_template(page_model.url),
        element_signature=page_model.signature(),
    )


@dataclass
class StateNode:
    """Tracks exploration progress by (role, name) SIGNATURE, not by `ref` — Phase 7
    finding: `PageModel` refs (e1, e2, ...) are assigned fresh on every single
    observation and are NOT stable across revisits to "the same" state, let alone
    across separate runs where the target app's DOM has changed at all (confirmed
    live: replaying a saved report against a fixture that had gained two new
    elements earlier on the page silently manipulated the wrong elements, since
    every subsequent ref had shifted by two). `exercised_refs` from Phase 2-5 had
    this same latent issue for anything beyond single-visit coverage counting; kept
    as a separate `exercised_refs` field for backward-compatible ref-based lookups
    within one live crawl session, but `exercised_signatures`/`element_signatures`
    are what the coverage report (this phase's actual goal) and any cross-run
    comparison should use."""

    fingerprint: StateFingerprint
    example_url: str
    visit_count: int = 0
    exercised_refs: set[str] = field(default_factory=set)
    exercised_signatures: set[tuple[str, str]] = field(default_factory=set)  # (role, name)
    element_signatures: set[tuple[str, str]] = field(default_factory=set)     # every (role, name) ever seen here
    element_count: int = 0

    def unexercised_element_count(self) -> int:
        return len(self.unexercised_signatures())

    def unexercised_signatures(self) -> set[tuple[str, str]]:
        return self.element_signatures - self.exercised_signatures


@dataclass
class Edge:
    from_key: str
    to_key: str
    action_summary: str  # e.g. "click e3 'Add to cart'" — for coverage reporting/debugging


class StateGraph:
    """Tracks distinct states, transitions between them, and per-state exploration
    progress. Drives the heuristic crawler's priority queue (Phase 2) and later the
    coverage report (Phase 7: "what was never reached")."""

    def __init__(self) -> None:
        self.nodes: dict[str, StateNode] = {}
        self.edges: list[Edge] = []
        self._current_key: str | None = None

    def visit(self, page_model: PageModel) -> StateNode:
        fp = fingerprint(page_model)
        key = fp.key()
        node = self.nodes.get(key)
        if node is None:
            node = StateNode(fingerprint=fp, example_url=page_model.url, element_count=len(page_model.elements))
            self.nodes[key] = node
        node.visit_count += 1
        node.element_count = max(node.element_count, len(page_model.elements))
        node.element_signatures |= {(e.role, e.name) for e in page_model.elements}
        self._current_key = key
        return node

    def record_action(self, from_key: str, to_key: str, action_summary: str) -> None:
        self.edges.append(Edge(from_key=from_key, to_key=to_key, action_summary=action_summary))

    def mark_exercised(self, state_key: str, ref: str, element: ElementInfo | None = None) -> None:
        """`element` is optional only for backward compatibility with older
        call sites during migration — always pass it when available. Without it,
        `exercised_signatures` isn't updated for this call, which understates
        coverage in the (role, name)-based report."""
        node = self.nodes.get(state_key)
        if node is None:
            return
        node.exercised_refs.add(ref)
        if element is not None:
            node.exercised_signatures.add((element.role, element.name))

    def is_new_state(self, page_model: PageModel) -> bool:
        return fingerprint(page_model).key() not in self.nodes

    def unexplored_states(self) -> list[StateNode]:
        """States with at least one element never interacted with — what the curious
        persona / heuristic crawler should prioritize."""
        return [n for n in self.nodes.values() if n.unexercised_element_count() > 0]

    def coverage_summary(self) -> dict:
        total_elements = sum(n.element_count for n in self.nodes.values())
        exercised = sum(len(n.exercised_refs) for n in self.nodes.values())
        return {
            "states": len(self.nodes),
            "edges": len(self.edges),
            "total_elements": total_elements,
            "exercised_elements": exercised,
            "unexplored_states": len(self.unexplored_states()),
        }

    def unreached_elements(self) -> list[tuple[str, str, str]]:
        """(state_template, role, name) for every element that was observed but
        never interacted with, across all states — the actual "what did we miss"
        list for the Phase 7 coverage report. Uses (role, name) signatures, not
        refs, since refs aren't stable across observations (see StateNode's
        docstring) — this is what makes the list meaningful to a human reading a
        report, not just an internal bookkeeping count."""
        result: list[tuple[str, str, str]] = []
        for node in self.nodes.values():
            for role, name in sorted(node.unexercised_signatures()):
                result.append((node.fingerprint.template, role, name))
        return result
