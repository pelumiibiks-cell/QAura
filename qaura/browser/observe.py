"""Builds a distilled PageModel from a live Page — Playwright's ARIA snapshot joined
with a live-locator query for state that the snapshot text doesn't carry. Raw HTML is
never sent to the LLM; this is what goes instead. Keeps a typical page around 1-2k
tokens instead of 50k+, and gives the planner (Phase 3) stable `ref` handles to act on
instead of fragile CSS selectors.

API note: Playwright's old `page.accessibility.snapshot()` (a nested-dict tree) was
removed in this installed version (1.62.0) — confirmed live, it raised AttributeError
when first tried. Its replacement is `page.aria_snapshot()`, which returns a YAML-ish
*string*, not a dict. `_parse_aria_snapshot()` below turns that string into the same
walk-and-collect shape the rest of this module expects, using PyYAML for the structural
parsing (indentation/nesting) and a regex for the `role "name" [attrs]` mini-grammar
inside each node. Verified against a live snapshot containing a textbox with a value,
a checked checkbox, a disabled button, and a combobox with a selected option — see
`test_parse_aria_snapshot_*` in tests/unit/test_observe.py for the fixtures.

Design note from the plan: two pages with the same interactive-element *signature* but
different URLs (e.g. /product/1 vs /product/2) should fingerprint to the same state in
core/state.py (Phase 2) — that's why `signature()` exists here rather than only in
core/state.py: the fingerprint input is a property of the PageModel itself.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field

import yaml
from playwright.async_api import Page

_log = logging.getLogger(__name__)

# One evaluate() per enriched element instead of a separate tagName call — same
# number of round trips as before, but now also returns form identity and
# validation attributes so core/forms.py can group fields and fuzz them realistically.
_ENRICH_JS = """el => {
    const tag = el.tagName.toLowerCase();
    let formKey = null;
    const form = el.closest('form');
    if (form) {
        formKey = form.id ? ('#' + form.id) : (form.getAttribute('name') || form.getAttribute('action') || 'form');
    } else {
        const fs = el.closest('fieldset');
        if (fs) formKey = fs.id ? ('#' + fs.id) : (fs.getAttribute('name') || 'fieldset');
    }
    return {
        tag,
        formKey,
        inputType: el.type || null,
        required: !!el.required,
        maxlength: (el.maxLength != null && el.maxLength >= 0) ? el.maxLength : null,
        pattern: el.pattern || null,
        min: el.min || null,
        max: el.max || null,
    };
}"""

# Roles worth surfacing to the planner/heuristic crawler as "interactive". Deliberately
# excludes purely presentational roles (img, heading, text, etc.) — those matter for the
# visual/a11y detectors (Phase 4), not for deciding what to click.
INTERACTIVE_ROLES = {
    "button", "link", "textbox", "checkbox", "radio", "combobox", "listbox",
    "menuitem", "menuitemcheckbox", "menuitemradio", "option", "searchbox",
    "slider", "spinbutton", "switch", "tab", "treeitem",
}

# Matches the descriptor Playwright puts on each aria_snapshot node/key, e.g.
#   button "Submit"
#   checkbox "Subscribe" [checked]
#   textbox "Email"
#   combobox                          (no name)
#   heading "Example Domain" [level=1]
_DESCRIPTOR_RE = re.compile(
    r'^(?P<role>[a-zA-Z][\w-]*)'
    r'(?:\s+"(?P<name>[^"]*)")?'
    r'(?:\s*\[(?P<attrs>[^\]]*)\])?\s*$'
)


@dataclass
class _AriaNode:
    role: str
    name: str
    attrs: dict[str, str | bool]
    value: str | None  # the text after ": " for leaf nodes like `textbox "Email": a@b.com`


def _parse_descriptor(text: str) -> _AriaNode | None:
    m = _DESCRIPTOR_RE.match(text.strip())
    if not m:
        return None
    attrs: dict[str, str | bool] = {}
    if m.group("attrs"):
        for part in m.group("attrs").split(","):
            part = part.strip()
            if not part:
                continue
            if "=" in part:
                k, v = part.split("=", 1)
                attrs[k.strip()] = v.strip()
            else:
                attrs[part] = True
    return _AriaNode(role=m.group("role"), name=m.group("name") or "", attrs=attrs, value=None)


def _walk_parsed(node) -> list[_AriaNode]:
    """Walks the structure yaml.safe_load produced from an aria_snapshot string.
    A node is either:
      - a plain string: a leaf descriptor with no children (e.g. a heading)
      - a dict with one key: the key is a descriptor string, the value is either a
        child list (recurse) or a plain string (the node's own text/value — e.g. a
        textbox's current value, or a paragraph's text content)
    Property-only children some roles emit (e.g. `- /url: ...` under a link) start
    with `/` and aren't real accessibility nodes — skipped.
    """
    results: list[_AriaNode] = []
    if isinstance(node, str):
        parsed = _parse_descriptor(node)
        if parsed:
            results.append(parsed)
        return results
    if isinstance(node, dict):
        for key, value in node.items():
            if key.startswith("/"):
                continue  # property line, e.g. /url — not an accessibility node
            parsed = _parse_descriptor(key)
            if parsed is None:
                continue
            if isinstance(value, str):
                parsed.value = value
                results.append(parsed)
            elif isinstance(value, list):
                results.append(parsed)
                for child in value:
                    results.extend(_walk_parsed(child))
            else:
                results.append(parsed)
        return results
    if isinstance(node, list):
        for child in node:
            results.extend(_walk_parsed(child))
        return results
    return results


def parse_aria_snapshot(snapshot_text: str) -> list[_AriaNode]:
    """Public entry point so tests can exercise the parser without a live browser."""
    parsed_yaml = yaml.safe_load(snapshot_text)
    if parsed_yaml is None:
        return []
    return _walk_parsed(parsed_yaml)


@dataclass
class ElementInfo:
    ref: str                      # stable handle, e.g. "e12" — assigned by build order
    role: str
    name: str                     # accessible name
    value: str | None = None
    enabled: bool = True
    visible: bool = True
    checked: bool | None = None   # tri-state: True/False/None (not applicable)
    bbox: tuple[float, float, float, float] | None = None  # x, y, width, height
    tag: str | None = None        # underlying HTML tag, for the destructive-pattern
                                    # classifier in guardrails.py (Phase 2) to also check
                                    # e.g. a <button type=submit> without an accessible name
    index: int = 0                 # position among elements sharing this (role, name) —
                                    # a get_by_role(role, name=name) locator can match more
                                    # than one element (duplicate/empty accessible names are
                                    # common), so every action executor addresses via
                                    # `.nth(index)` rather than assuming a unique match
    form_key: str | None = None    # identity of the nearest <form>/<fieldset> ancestor,
                                    # for core/forms.py to group fields into one submission
    input_type: str | None = None  # the underlying <input type=...> (email/number/date/
                                    # file/password/tel/url/search/...), for realistic fuzz
    required: bool = False
    maxlength: int | None = None
    pattern: str | None = None
    min: str | None = None
    max: str | None = None

    def to_prompt_line(self) -> str:
        bits = [self.ref, self.role]
        if self.name:
            bits.append(f'"{self.name}"')
        if self.value:
            bits.append(f"value={self.value!r}")
        if self.checked is not None:
            bits.append(f"checked={self.checked}")
        if not self.enabled:
            bits.append("disabled")
        return " ".join(bits)


@dataclass
class PageModel:
    url: str
    title: str
    elements: list[ElementInfo] = field(default_factory=list)

    def signature(self) -> str:
        """Sorted, order-independent hash of (role, name) pairs. Used by core/state.py
        for state fingerprinting — two loads of the same template page should produce
        the same signature even if element ordering in the DOM shifts slightly."""
        parts = sorted(f"{e.role}:{e.name}" for e in self.elements)
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

    def to_prompt(self) -> str:
        lines = [f"URL: {self.url}", f"Title: {self.title}", "Interactive elements:"]
        if not self.elements:
            lines.append("  (none found)")
        for e in self.elements:
            lines.append(f"  {e.to_prompt_line()}")
        return "\n".join(lines)

    def find(self, ref: str) -> ElementInfo | None:
        return next((e for e in self.elements if e.ref == ref), None)


CHECKABLE_ROLES = {"checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio"}


async def build_page_model(page: Page) -> PageModel:
    """`page.aria_snapshot()` gives us role/name/value/checked/disabled for the whole
    tree in one call (parsed via parse_aria_snapshot above); we still need a live
    locator per matched node to read visibility and a bounding box, since the snapshot
    text doesn't carry pixel geometry. Role-based locators keep this independent of the
    target site's DOM shape rather than depending on CSS selectors.
    """
    snapshot_text = await page.aria_snapshot()
    nodes = parse_aria_snapshot(snapshot_text)

    elements: list[ElementInfo] = []
    ref_counter = 0
    for node in nodes:
        if node.role not in INTERACTIVE_ROLES:
            continue
        ref_counter += 1
        checked: bool | None = None
        if node.role in CHECKABLE_ROLES:
            checked = bool(node.attrs.get("checked", False))
        elements.append(ElementInfo(
            ref=f"e{ref_counter}",
            role=node.role,
            name=node.name,
            value=node.value,
            checked=checked,
            enabled=not bool(node.attrs.get("disabled", False)),
        ))

    # Enrich with visibility + bbox + form/validation metadata via role-based
    # locators, grouped by (role, name) so duplicate accessible names (several
    # "Delete" buttons in a list, or several unlabeled textboxes) are each enriched
    # via `.nth(index)` instead of being skipped outright — Phase A finding: a
    # get_by_role(...).count() != 1 used to mean the element was silently left with
    # default visible/bbox values, AND actions.py's own locator had no way to
    # disambiguate them either, which surfaced as bogus "crash" findings (a
    # Playwright strict-mode violation on click/fill was caught as an unexpected
    # exception, not recognized as an addressing problem). `index` recorded here is
    # what actions.py's _locator_for uses to resolve the exact same element.
    groups: dict[tuple[str, str], list[ElementInfo]] = {}
    for el in elements:
        groups.setdefault((el.role, el.name), []).append(el)

    for (role, name), group in groups.items():
        try:
            locator = page.get_by_role(role, name=name, exact=True)
            count = await locator.count()
        except Exception:
            # Every element in this (role, name) group keeps form_key=None, which
            # makes core/forms.py:group_forms() drop them entirely — so a failure
            # here silently disables form-filling for the whole group with nothing
            # in the run's output showing it happened. --verbose surfaces it.
            _log.debug("locator enrichment failed for role=%r name=%r", role, name, exc_info=True)
            continue
        for idx, el in enumerate(group):
            if idx >= count:
                continue  # aria tree had more entries than live DOM matches right now
            el.index = idx
            item = locator.nth(idx)
            try:
                el.visible = await item.is_visible()
                box = await item.bounding_box()
                if box:
                    el.bbox = (box["x"], box["y"], box["width"], box["height"])
                extra = await item.evaluate(_ENRICH_JS)
                el.tag = extra.get("tag")
                el.form_key = extra.get("formKey")
                el.input_type = extra.get("inputType")
                el.required = bool(extra.get("required", False))
                el.maxlength = extra.get("maxlength")
                el.pattern = extra.get("pattern")
                el.min = extra.get("min")
                el.max = extra.get("max")
            except Exception:
                # A locator can throw on detached/animating elements — non-fatal, the
                # element stays in the model with default visible/bbox values.
                _log.debug("per-element enrichment failed for %s ref=%s", el.role, el.ref, exc_info=True)
                continue

    return PageModel(url=page.url, title=await page.title(), elements=elements)
