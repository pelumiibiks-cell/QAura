"""Unusual-input corpus for fuzzing form fields. Used by the heuristic crawler
(Phase 2, no LLM needed — these are static) and available to personas later (the
malicious persona in particular leans on the injection-shaped entries). Values here are
inert by design: injection-*shaped* strings that would reveal unescaped rendering or a
crash, not functioning exploits — this project reports findings, it doesn't weaponize
them (per the plan's malicious-persona description).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FuzzValue:
    label: str          # short id for reports/dedupe, e.g. "empty", "sql_marker"
    value: str
    category: str        # "boundary" | "encoding" | "injection_marker" | "format"


BOUNDARY_VALUES = [
    FuzzValue("empty", "", "boundary"),
    FuzzValue("whitespace_only", "   ", "boundary"),
    FuzzValue("single_char", "a", "boundary"),
    FuzzValue("very_long", "A" * 10_000, "boundary"),
    FuzzValue("zero", "0", "boundary"),
    FuzzValue("negative_one", "-1", "boundary"),
    FuzzValue("max_int32", "2147483647", "boundary"),
    FuzzValue("over_max_int32", "2147483648", "boundary"),
    FuzzValue("float_where_int_expected", "1.5", "boundary"),
]

ENCODING_VALUES = [
    FuzzValue("unicode_emoji", "😀🔥💥", "encoding"),
    FuzzValue("rtl_arabic", "مرحبا بالعالم", "encoding"),
    FuzzValue("zero_width_chars", "a​b‌c‍d", "encoding"),
    FuzzValue("null_byte", "a\x00b", "encoding"),
    FuzzValue("newlines_embedded", "line1\nline2\r\nline3", "encoding"),
    FuzzValue("combining_diacritics", "é́́́́", "encoding"),
    FuzzValue("surrogate_pair_edge", "𝟙𝟚𝟛", "encoding"),
]

# Shaped like common injection payloads for the purpose of checking whether input is
# escaped/parameterized downstream — not intended to function as an actual exploit
# against any real interpreter. The security detector (Phase 4) checks whether these
# come back unescaped in the DOM/response, which is the actual finding.
INJECTION_MARKER_VALUES = [
    FuzzValue("html_tag_marker", "<qaura-marker>test</qaura-marker>", "injection_marker"),
    FuzzValue("script_tag_marker", "<script>window.__qaura_marker=1</script>", "injection_marker"),
    FuzzValue("attr_break_marker", '"><qaura-marker/>', "injection_marker"),
    FuzzValue("sql_quote_marker", "qaura' OR '1'='1", "injection_marker"),
    FuzzValue("template_injection_marker", "{{7*7}}${7*7}", "injection_marker"),
    FuzzValue("path_traversal_marker", "../../../../etc/passwd", "injection_marker"),
]

FORMAT_MISMATCH_VALUES = [
    FuzzValue("text_in_email_field", "not-an-email", "format"),
    FuzzValue("email_missing_domain", "user@", "format"),
    FuzzValue("phone_with_letters", "555-CALL-NOW", "format"),
    FuzzValue("date_out_of_range", "9999-99-99", "format"),
    FuzzValue("negative_quantity", "-5", "format"),
]

ALL_VALUES: list[FuzzValue] = (
    BOUNDARY_VALUES + ENCODING_VALUES + INJECTION_MARKER_VALUES + FORMAT_MISMATCH_VALUES
)


# Found live against buggy_app, in two rounds: giving a spinbutton (<input
# type=number>) BOUNDARY_VALUES/FORMAT_MISMATCH_VALUES wholesale caused Playwright's
# fill() to THROW on genuinely non-numeric strings — browsers reject non-numeric
# input for type=number at the value-setter level, Playwright surfaces that as an
# error, and heuristic.py reported each as a "crash" finding: QAura fuzzing itself
# into false positives, not real app bugs. The first fix (subtracting the
# obviously-textual FORMAT_MISMATCH entries) was still incomplete — BOUNDARY_VALUES
# itself has non-numeric entries too ("a", "A"*10000) that a shared textbox/spinbutton
# list can't avoid. An explicit allow-list is the actual fix: every value here is
# confirmed parseable as a number by a real <input type=number>, not derived by
# filtering a list built for free-text fields.
SPINBUTTON_VALUES: list[FuzzValue] = [
    FuzzValue("empty", "", "boundary"),
    FuzzValue("whitespace_only", "   ", "boundary"),
    FuzzValue("zero", "0", "boundary"),
    FuzzValue("negative_one", "-1", "boundary"),
    FuzzValue("max_int32", "2147483647", "boundary"),
    FuzzValue("over_max_int32", "2147483648", "boundary"),
    FuzzValue("float_where_int_expected", "1.5", "boundary"),
    FuzzValue("negative_quantity", "-5", "format"),
]


def values_for_role(role: str) -> list[FuzzValue]:
    """Trims the corpus per element role so e.g. a checkbox never gets a `fill`-shaped
    value. Callers should still guard on role before calling FILL at all — this just
    narrows *which* fuzz values make sense for roles that do take text."""
    if role in ("textbox", "searchbox"):
        return ALL_VALUES
    if role in ("spinbutton",):
        return SPINBUTTON_VALUES
    return []


CATEGORY_ORDER = ["boundary", "encoding", "injection_marker", "format"]


def valid_value_for(role: str, name: str, input_type: str | None = None) -> str:
    """A plausible, realistic value for a field — the opposite job of the fuzz corpus.
    Phase B: this is what lets the crawler fill a whole form with coherent data and
    actually reach a real submission, instead of every field ending up with fuzz junk
    that a "Save record"-style handler will reject or mangle before the business logic
    is ever exercised. Keyed off input_type first (most reliable), falling back to a
    substring match against the accessible name for the common label patterns a
    generated form field name follows (works for both `<input type=text name=...>`
    forms and free-text ARIA-labeled fields with no `type` at all)."""
    name_l = (name or "").lower()
    it = (input_type or "").lower()

    if it == "email" or "email" in name_l:
        return "qa@qaura.invalid"
    if it == "password" or "password" in name_l:
        return "QaTest12345!"
    if it == "date" or ("date" in name_l and "update" not in name_l):
        return "2024-01-15"
    if it == "url" or "website" in name_l:
        return "https://qaura.invalid/"
    if it == "tel" or "phone" in name_l:
        return "+1-555-0100"
    if it == "number" or role == "spinbutton":
        return "42"
    if role == "searchbox" or "search" in name_l:
        return "test"
    if any(k in name_l for k in ("id", "number", "pin", "tin", "account")):
        return "123456789"
    if "name" in name_l:
        return "QA Test"
    return "QA test value"


def hostile_value_for(role: str, seed: int = 0) -> FuzzValue:
    """One deliberately unusual value for a field being fuzzed while the rest of its
    form holds valid data (Phase B's fuzz pass: one corrupted field at a time, so a
    resulting error is attributable to that specific field). Skips the plain boundary
    category (empty/whitespace) by default, since those are the least interesting
    values to isolate in a form that otherwise submits successfully — `seed` cycles
    through the encoding/injection/format values so repeated calls for different
    fields don't all get the same payload."""
    pool = [v for v in values_for_role(role) if v.category in ("injection_marker", "encoding", "format")]
    if not pool:
        pool = values_for_role(role)
    if not pool:
        return FuzzValue("empty", "", "boundary")
    return pool[seed % len(pool)]


def diverse_slice(role: str, n: int) -> list[FuzzValue]:
    """Picks up to `n` values, round-robin across categories, rather than the first
    n of a role's full list. A field only fuzzed with `values_for_role(role)[:4]`
    would get 4 boundary values and never try a single encoding/injection/format
    value, since BOUNDARY_VALUES happens to sort first in ALL_VALUES — this is what a
    budget-constrained crawler should use instead, so a small slice still samples
    every category at least once before repeating any of them."""
    pool = values_for_role(role)
    by_cat: dict[str, list[FuzzValue]] = {c: [v for v in pool if v.category == c] for c in CATEGORY_ORDER}
    result: list[FuzzValue] = []
    i = 0
    while len(result) < n and any(by_cat.values()):
        cat = CATEGORY_ORDER[i % len(CATEGORY_ORDER)]
        if by_cat[cat]:
            result.append(by_cat[cat].pop(0))
        i += 1
        if i > n * len(CATEGORY_ORDER) + len(CATEGORY_ORDER):
            break  # safety valve, should never actually trigger given the loop condition
    return result[:n]
