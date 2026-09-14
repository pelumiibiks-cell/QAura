"""Numeric-element inventory: finds the numbers on a page and works out how to address
each one with a CSS selector that will still work next week.

`qaura init` needs this because of a gap between two existing modules. PageModel
(browser/observe.py) is built from the accessibility tree and carries roles and names but
no selectors at all, while InvariantConfig.values (config.py) is name -> CSS selector.
Nothing in the codebase bridged those, so synthesizing an invariant was impossible.

The split of work here is deliberate: the JS payload gathers raw material and counts
matches, and every judgement call — which selector to prefer, whether a class name is a
build artifact, whether an id is real — happens in Python, where it is pure, cheap to
test, and reuses the "does this look like an opaque identifier" rules that core/state.py
already had to solve for URL templating.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

from playwright.async_api import Page

from qaura.core.invariants import parse_number
from qaura.core.state import _HEX_RUN_RE, _MIXED_ID_RE, _NUMERIC_RE, _UUID_RE

_log = logging.getLogger(__name__)

SelectorKind = Literal["testid", "test-attr", "id", "aria", "class", "structural"]

# Base stability scores. The ordering encodes one idea: prefer whatever the site's own
# authors chose as a deliberate, human-meaningful handle, and fall back to structure only
# when there is nothing else. A test id survives a redesign; an nth-of-type path does not.
_BASE_SCORES: dict[str, int] = {
    "testid": 100,
    "test-attr": 95,
    "id": 75,
    "aria": 55,
    "class": 45,
    "structural": 20,
}

MIN_SELECTOR_SCORE = 20

# Frameworks that mint element ids at runtime. These are stable within one page load and
# worthless across two, which is the worst possible property for a config file.
_FRAMEWORK_ID_RES = (
    re.compile(r"^:r[0-9a-z]+:$", re.IGNORECASE),   # React 18 useId
    re.compile(r"^radix-", re.IGNORECASE),
    re.compile(r"^headlessui-", re.IGNORECASE),
    re.compile(r"^ember\d+$", re.IGNORECASE),
    re.compile(r"^(mat|ng|cdk)-", re.IGNORECASE),
    re.compile(r"^__next", re.IGNORECASE),
    re.compile(r"^(v-|el-)[0-9a-f]{4,}$", re.IGNORECASE),
)

# CSS-in-JS and CSS-modules emitters, which append a content hash to every class.
_CSS_IN_JS_RE = re.compile(r"^(css|sc|jsx|emotion|styled|chakra|mui|Mui)-[A-Za-z0-9]{4,}$")
_CSS_MODULES_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9]+__[A-Za-z0-9]{4,}$")
_TRAILING_HASH_RE = re.compile(r"__[A-Za-z0-9]{4,}$")

# Characters that force CSS.escape. Their presence means the class is almost certainly a
# Tailwind arbitrary value (`w-[calc(100%-2rem)]`) or a variant (`md:flex`, `hover:underline`),
# neither of which is a semantic handle for the element.
_NEEDS_ESCAPE_RE = re.compile(r"[\[\]()/:.%#!<>,\s]")

# Utility classes describe presentation, not identity, so they make poor selectors even
# when unique. Matching them needs BOTH halves though: the prefix alone is far too blunt,
# because Tailwind's utility namespace collides with ordinary domain vocabulary. `order-`,
# `row-`, `col-`, `content-` and `line-` are all real utility prefixes AND the natural
# start of `order-total`, `row-count`, `line-item-price` — exactly the class names an
# e-commerce app writes, which is the domain this feature most needs to work on. So a
# class is only a utility if what follows the prefix is a utility VALUE: a number, a size
# token, a colour, a direction.
_UTILITY_PREFIX_RE = re.compile(
    r"^(?:text|bg|border|rounded|shadow|flex|grid|gap|p[xytblrse]?|m[xytblrse]?|w|h|min|max"
    r"|items|justify|self|font|leading|tracking|space|col|row|order|opacity|z|inset|top"
    r"|bottom|left|right|overflow|cursor|transition|duration|ease|d|align|float|position"
    r"|pull|offset|basis|grow|shrink|place|content|divide|ring|outline|fill|stroke|line"
    r"|list|whitespace|break|indent|decoration)-(.+)$",
    re.IGNORECASE,
)
_UTILITY_VALUE_RE = re.compile(
    r"^(?:\d+(?:\.\d+)?|\d+/\d+|\[.*\]"
    r"|xs|sm|md|lg|xl|\d?xl|full|auto|none|screen|fit|px|min|max|initial|revert"
    r"|left|right|top|bottom|center|start|end|between|around|evenly|baseline|stretch"
    r"|first|last|normal|wrap|nowrap|reverse|solid|dashed|dotted|double|hidden|visible"
    r"|bold|semibold|medium|light|thin|black|italic|tight|wide|loose|snug|relaxed"
    r"|(?:slate|gray|grey|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald"
    r"|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose|white|black|transparent"
    r"|current|inherit|primary|secondary|success|danger|warning|info|muted|dark|light)"
    r"(?:-\d{2,3})?"
    r")$",
    re.IGNORECASE,
)


# Tailwind nests one level: `col-span-2`, `inset-x-0`, `space-y-4`. Stripping a known
# sub-modifier lets the value test still reach the actual value.
_UTILITY_SUBMODIFIER_RE = re.compile(
    r"^(?:span|start|end|offset|auto|x|y|t|b|l|r|s|e|width|height|size|opacity|reverse)-(.+)$",
    re.IGNORECASE,
)


def _is_utility_class(cls: str) -> bool:
    match = _UTILITY_PREFIX_RE.match(cls)
    if not match:
        return False
    remainder = match.group(1)
    if _UTILITY_VALUE_RE.match(remainder):
        return True
    nested = _UTILITY_SUBMODIFIER_RE.match(remainder)
    return bool(nested and _UTILITY_VALUE_RE.match(nested.group(1)))
# Single-word utilities, which the prefix rule cannot see because they have no value half.
_UTILITY_EXACT = frozenset({
    "container", "row", "col", "btn", "card", "badge", "alert", "sr-only", "clearfix",
    "hidden", "visible", "active", "disabled", "show", "fade", "collapse", "wrapper",
    "inner", "outer", "content", "wrap", "block", "inline", "small", "large", "center",
    "flex", "grid", "table", "static", "relative", "absolute", "fixed", "sticky",
    "italic", "underline", "truncate", "uppercase", "lowercase", "capitalize",
    "antialiased", "border", "rounded", "shadow", "invisible", "isolate", "contents",
})

# Mixed letter+digit tokens that are legitimate rather than hash-like. Kept deliberately
# tiny — the entropy test it exempts is the main defence against build-generated classes,
# so every addition here weakens it.
_MIXED_TOKEN_ALLOWLIST = frozenset({
    "h1", "h2", "h3", "h4", "h5", "h6", "col12", "col6", "col4", "col3", "md5", "sha1",
    "utf8", "base64", "oauth2", "h264", "mp3", "mp4", "id3", "x2", "x3", "s3", "ec2",
})


def looks_generated_identifier(token: str) -> bool:
    """True if `token` looks machine-minted rather than authored. Reuses core/state.py's
    regexes rather than restating them: that module already had to answer exactly this
    question for URL path segments, its rules are already tested, and two divergent
    definitions of "looks like an id" in one codebase is a bug waiting to happen."""
    if not token:
        return True
    if _UUID_RE.match(token) or _NUMERIC_RE.match(token) or _HEX_RUN_RE.match(token):
        return True
    if _MIXED_ID_RE.match(token):
        return True
    return any(rx.search(token) for rx in _FRAMEWORK_ID_RES)


def _token_looks_hashed(token: str) -> bool:
    if token.lower() in _MIXED_TOKEN_ALLOWLIST:
        return False
    if len(token) < 5:
        return False
    has_alpha = any(c.isalpha() for c in token)
    has_digit = any(c.isdigit() for c in token)
    return has_alpha and has_digit


def is_hashy_class(cls: str) -> bool:
    """True if `cls` is a build artifact or a presentation utility, either of which makes
    a selector that breaks on the next deploy or means nothing to a human reading the
    config."""
    if not cls or _NEEDS_ESCAPE_RE.search(cls):
        return True
    if not (3 <= len(cls) <= 40):
        return True
    if _CSS_IN_JS_RE.match(cls) or _CSS_MODULES_RE.match(cls) or _TRAILING_HASH_RE.search(cls):
        return True
    if cls.lower() in _UTILITY_EXACT or _is_utility_class(cls):
        return True
    if any(_token_looks_hashed(tok) for tok in re.split(r"[-_]", cls)):
        return True
    # Anything left must read like an authored name.
    return not re.fullmatch(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*", cls, re.IGNORECASE)


@dataclass(frozen=True)
class SelectorCandidate:
    selector: str
    kind: SelectorKind
    match_count: int
    scoped_count: int
    score: int
    rejected_reason: str | None = None

    @property
    def usable(self) -> bool:
        return self.rejected_reason is None and self.score >= MIN_SELECTOR_SCORE


@dataclass
class NumericElement:
    """One number on the page, with the selector that addresses it and everything the
    candidate generator needs to reason about it."""

    index: int                      # the "n7" handle an LLM addresses; assigned by scan order
    selector: str
    selector_kind: SelectorKind
    selector_score: int
    match_count: int                # matches across the whole document
    scoped_count: int               # matches within container_selector, which is what
                                    # core/invariants.extract_values() actually counts
    container_selector: str | None
    raw_text: str
    number: float
    label: str | None
    label_source: str | None
    tag: str
    dom_index: int
    volatile: bool = False          # value changed between two loads of the same URL
    viewport_fragile: bool = False  # present in only one tested viewport

    @property
    def handle(self) -> str:
        return f"n{self.index}"

    @property
    def is_group(self) -> bool:
        """A selector matching several elements that share a label is a repeated group —
        line items, table rows. extract_values() turns these into a list, which is what
        makes `sum(line_items)` possible."""
        return self.scoped_count > 1

    def to_prompt_line(self) -> str:
        bits = [f"{self.handle:<4}"]
        bits.append(f'label={self.label!r}' if self.label else "label=None")
        bits.append(f"text={self.raw_text!r}")
        bits.append(f"value={self.number}")
        bits.append(f"selector={self.selector}")
        bits.append(f"matches={self.scoped_count}")
        if self.container_selector:
            bits.append(f"container={self.container_selector}")
        if self.is_group:
            bits.append("(repeated group)")
        return "  ".join(bits)


# Gathers raw material only. Every preference decision is made in Python — this returns
# all plausible selectors with their live match counts and lets the scorer choose.
_NUMERIC_SCAN_JS = r"""() => {
  const SKIP_TAGS = new Set(['INPUT','TEXTAREA','SELECT','OPTION','SCRIPT','STYLE','NOSCRIPT','TEMPLATE']);
  const TEST_ATTRS = ['data-testid','data-test-id','data-test','data-qa','data-cy','data-automation-id'];
  const LANDMARKS = new Set(['SECTION','MAIN','ARTICLE','NAV','ASIDE','FORM','TABLE','DIALOG','HEADER','FOOTER']);
  const DIGIT = /\d/;

  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/[^a-zA-Z0-9_-]/g, '\\$&');
  const countOf = (sel) => { try { return document.querySelectorAll(sel).length; } catch (e) { return -1; } };

  // An id is only usable as `#id` when it needs no escaping; otherwise the attribute
  // form is the safe spelling. React's useId emits ':r3:', and '#:r3:' is a parse error.
  const idSelector = (id) => /^[A-Za-z][A-Za-z0-9_-]*$/.test(id) ? ('#' + id) : ('[id="' + id.replace(/"/g, '\\"') + '"]');
  const attrSelector = (name, value) => '[' + name + '="' + String(value).replace(/"/g, '\\"') + '"]';

  const ownText = (el) => {
    let out = '';
    for (const node of el.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) out += node.nodeValue;
    }
    return out.replace(/\s+/g, ' ').trim();
  };

  const stableAncestorSelector = (el) => {
    let cur = el.parentElement;
    let hops = 0;
    while (cur && cur !== document.body && hops < 8) {
      for (const a of TEST_ATTRS) {
        const v = cur.getAttribute(a);
        if (v) return { sel: attrSelector(a, v), kind: 'testid', node: cur };
      }
      if (cur.id) return { sel: idSelector(cur.id), kind: 'id', node: cur };
      const role = cur.getAttribute('role');
      if (role) return { sel: attrSelector('role', role), kind: 'aria', node: cur };
      if (LANDMARKS.has(cur.tagName)) return { sel: cur.tagName.toLowerCase(), kind: 'structural', node: cur };
      cur = cur.parentElement;
      hops++;
    }
    return null;
  };

  // Path from the nearest stable ancestor down to `el`, capped at 4 hops. Uses
  // :nth-of-type rather than :nth-child so inserting a sibling of a different tag
  // (a wrapper div, a comment-turned-element) doesn't shift the index.
  const structuralSelector = (el, anchor) => {
    const parts = [];
    let cur = el;
    let hops = 0;
    while (cur && cur !== document.body && hops < 4) {
      if (anchor && cur === anchor.node) break;
      const parent = cur.parentElement;
      if (!parent) break;
      const tag = cur.tagName.toLowerCase();
      const sameTag = Array.from(parent.children).filter((c) => c.tagName === cur.tagName);
      parts.unshift(sameTag.length > 1 ? tag + ':nth-of-type(' + (sameTag.indexOf(cur) + 1) + ')' : tag);
      cur = parent;
      hops++;
    }
    if (!parts.length) return null;
    const prefix = (anchor && cur === anchor.node) ? anchor.sel + ' > ' : '';
    return prefix + parts.join(' > ');
  };

  const labelFor = (el) => {
    if (el.id) {
      const lbl = document.querySelector('label[for="' + el.id.replace(/"/g, '\\"') + '"]');
      if (lbl && lbl.textContent.trim()) return { text: lbl.textContent.trim(), source: 'label-for' };
    }
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return { text: aria.trim(), source: 'aria-label' };
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const ref = document.getElementById(labelledBy.split(/\s+/)[0]);
      if (ref && ref.textContent.trim()) return { text: ref.textContent.trim(), source: 'aria-labelledby' };
    }
    // A table cell is labelled by its column header.
    const cell = el.closest('td, th');
    if (cell && cell.parentElement && cell.parentElement.parentElement) {
      const table = cell.closest('table');
      const idx = Array.from(cell.parentElement.children).indexOf(cell);
      if (table) {
        const head = table.querySelector('thead tr, tr');
        if (head && head.children[idx] && head.children[idx] !== cell) {
          const t = head.children[idx].textContent.trim();
          if (t) return { text: t, source: 'th' };
        }
      }
    }
    const dt = el.closest('dd') && el.closest('dd').previousElementSibling;
    if (dt && dt.tagName === 'DT' && dt.textContent.trim()) {
      return { text: dt.textContent.trim(), source: 'dt' };
    }
    // "Subtotal: <span>$10.00</span>" — the label is the text immediately before us.
    // Keep walking past separator-only nodes: a markup like
    // "Widget x <input> = <span>$10.00</span>" puts a bare " = " directly before the
    // number, and stopping there yields an empty label rather than "Widget".
    let prev = el.previousSibling;
    while (prev) {
      if (prev.nodeType === Node.TEXT_NODE) {
        const cleaned = prev.nodeValue.replace(/[\s:=×x*+\-]+$/, '').trim();
        if (cleaned) return { text: cleaned, source: 'prev-sibling-text' };
      }
      if (prev.nodeType === Node.ELEMENT_NODE) {
        const cleaned = prev.textContent.replace(/[\s:=]+$/, '').trim();
        if (cleaned) return { text: cleaned, source: 'prev-sibling-el' };
      }
      prev = prev.previousSibling;
    }
    const parent = el.parentElement;
    if (parent) {
      const mine = el.textContent || '';
      const rest = (parent.textContent || '').replace(mine, '').replace(/[\s:=]+$/, '').trim();
      if (rest) return { text: rest.slice(0, 80), source: 'parent-text-minus-number' };
    }
    const title = el.getAttribute('title');
    if (title && title.trim()) return { text: title.trim(), source: 'title' };
    return { text: null, source: null };
  };

  const out = [];
  let domIndex = 0;
  for (const el of document.querySelectorAll('body *')) {
    domIndex++;
    if (SKIP_TAGS.has(el.tagName)) continue;

    // Direct child text only. extract_values() reads text_content(), which on a
    // container returns the whole subtree — so a wrapper div would yield the
    // concatenation of every number inside it, and parse_number would take the first.
    const text = ownText(el);
    if (!text || !DIGIT.test(text)) continue;

    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;

    const anchor = stableAncestorSelector(el);
    const candidates = [];

    for (const a of TEST_ATTRS) {
      const v = el.getAttribute(a);
      if (v) {
        const sel = attrSelector(a, v);
        candidates.push({ sel, kind: a === 'data-testid' ? 'testid' : 'test-attr', count: countOf(sel) });
      }
    }
    if (el.id) {
      const sel = idSelector(el.id);
      candidates.push({ sel, kind: 'id', count: countOf(sel), raw: el.id });
    }
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) {
      const sel = attrSelector('aria-label', aria.trim());
      candidates.push({ sel, kind: 'aria', count: countOf(sel) });
    }
    for (const cls of Array.from(el.classList)) {
      const sel = '.' + esc(cls);
      candidates.push({ sel, kind: 'class', count: countOf(sel), raw: cls });
    }
    const structural = structuralSelector(el, anchor);
    if (structural) candidates.push({ sel: structural, kind: 'structural', count: countOf(structural) });

    // Match counts scoped to the container, since that is what extract_values() sees
    // when container_selector is set.
    if (anchor) {
      let root = null;
      try { root = document.querySelector(anchor.sel); } catch (e) { root = null; }
      for (const c of candidates) {
        try { c.scoped = root ? root.querySelectorAll(c.sel).length : c.count; } catch (e) { c.scoped = c.count; }
      }
    } else {
      for (const c of candidates) c.scoped = c.count;
    }

    const label = labelFor(el);
    out.push({
      ownText: text.slice(0, 120),
      tag: el.tagName.toLowerCase(),
      candidates,
      container: anchor ? { sel: anchor.sel, kind: anchor.kind } : null,
      label: label.text,
      labelSource: label.source,
      domIndex,
    });
  }
  return out;
}"""


def score_candidate(raw: dict, *, has_label: bool, shared_label: bool) -> SelectorCandidate:
    """Turns one raw JS candidate into a scored, possibly-rejected selector.

    `shared_label` distinguishes the two reasons a selector matches more than one
    element. If every match carries the same label it is a repeated group (line items),
    which is legitimate and is exactly how a `sum(...)` invariant gets its list. If the
    matches differ, the selector is simply too coarse to address this value.
    """
    kind: SelectorKind = raw.get("kind", "structural")
    selector = raw.get("sel", "")
    count = int(raw.get("count", 0) or 0)
    scoped = int(raw.get("scoped", count) or 0)
    token = raw.get("raw")

    if not selector or count <= 0:
        return SelectorCandidate(selector, kind, count, scoped, 0, "selector matches nothing")

    if kind == "id" and token and looks_generated_identifier(token):
        return SelectorCandidate(selector, kind, count, scoped, 0, f"generated id {token!r}")
    if kind == "class" and token and is_hashy_class(token):
        return SelectorCandidate(selector, kind, count, scoped, 0, f"unstable class {token!r}")

    score = _BASE_SCORES.get(kind, 0)
    if scoped == 1:
        score += 10
    elif shared_label:
        score += 5
    elif has_label:
        # A labelled value should be uniquely addressable. If it isn't, this selector is
        # picking up unrelated elements and an invariant built on it would compare the
        # wrong numbers.
        score -= 25

    if kind == "class" and scoped > 1 and not shared_label:
        return SelectorCandidate(selector, kind, count, scoped, score, "non-unique, non-repeating class")

    return SelectorCandidate(selector, kind, count, scoped, score)


def choose_selector(
    candidates: list[dict], *, has_label: bool, shared_label: bool
) -> SelectorCandidate | None:
    """Highest score wins; ties break toward the shorter selector, then source order."""
    scored = [score_candidate(c, has_label=has_label, shared_label=shared_label) for c in candidates]
    usable = [c for c in scored if c.usable]
    if not usable:
        return None
    return sorted(usable, key=lambda c: (-c.score, len(c.selector)))[0]


def _shared_label_map(entries: list[dict]) -> dict[str, bool]:
    """For each selector string, whether every element proposing it carries the same
    label. Computed across the whole page because one element cannot tell on its own
    whether it is part of a repeated group."""
    by_selector: dict[str, set[str | None]] = {}
    for entry in entries:
        label = (entry.get("label") or "").strip() or None
        for cand in entry.get("candidates", []):
            sel = cand.get("sel")
            if sel:
                by_selector.setdefault(sel, set()).add(label)
    return {sel: len(labels) == 1 for sel, labels in by_selector.items()}


def build_numeric_elements(entries: list[dict]) -> list[NumericElement]:
    """Pure transform from the JS payload to the inventory. Separated from
    scan_numeric_elements() so the whole selection pipeline can be tested against a
    literal payload with no browser involved."""
    shared = _shared_label_map(entries)
    elements: list[NumericElement] = []

    for entry in entries:
        number = parse_number(entry.get("ownText", "") or "")
        if number is None:
            continue

        # Normalize "" to None: an empty label is the absence of one, and letting the two
        # spellings coexist means every downstream check has to remember both.
        label = (entry.get("label") or "").strip() or None
        has_label = label is not None
        cands = entry.get("candidates", [])
        best: SelectorCandidate | None = None
        for cand in cands:
            single = choose_selector(
                [cand], has_label=has_label, shared_label=shared.get(cand.get("sel", ""), False)
            )
            if single and (best is None or single.score > best.score
                           or (single.score == best.score and len(single.selector) < len(best.selector))):
                best = single
        if best is None:
            continue

        container = entry.get("container") or {}
        container_sel = container.get("sel")
        # A container that *is* the element's own selector adds nothing and would make
        # extract_values() scope a selector to itself.
        if container_sel == best.selector:
            container_sel = None

        elements.append(NumericElement(
            index=len(elements) + 1,
            selector=best.selector,
            selector_kind=best.kind,
            selector_score=best.score,
            match_count=best.match_count,
            scoped_count=best.scoped_count,
            container_selector=container_sel,
            raw_text=entry.get("ownText", ""),
            number=number,
            label=label,
            label_source=entry.get("labelSource") if label else None,
            tag=entry.get("tag", ""),
            dom_index=int(entry.get("domIndex", 0) or 0),
        ))

    return elements


async def scan_numeric_elements(page: Page) -> list[NumericElement]:
    """Every visible number on the page that can be addressed by a usable selector.

    Form controls are excluded at the JS level and it matters: core/invariants.py's
    extract_values() reads text_content(), which is empty for an <input>, so an
    invariant referencing one would raise InvariantError on every evaluation and be
    permanently inconclusive rather than obviously broken.
    """
    try:
        entries = await page.evaluate(_NUMERIC_SCAN_JS)
    except Exception:
        _log.debug("numeric scan failed on %s", page.url, exc_info=True)
        return []
    return build_numeric_elements(entries or [])
