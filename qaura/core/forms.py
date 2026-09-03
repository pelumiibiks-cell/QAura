"""Groups a PageModel's elements into forms via ElementInfo.form_key (Phase A), so the
crawler can fill every field and submit as one coherent unit instead of fuzzing fields
one at a time and never reaching the submit button. Deliberately outside heuristic.py —
the LLM-driven orchestrator can reuse this for the same reason once it wants form-aware
exploration too.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from qaura.browser.observe import ElementInfo, PageModel

FILLABLE_ROLES = {"textbox", "searchbox", "spinbutton"}
CHECKABLE_ROLES = {"checkbox", "switch", "radio"}
SELECTABLE_ROLES = {"combobox", "listbox"}

# Substrings checked against a button's accessible name (case-insensitive) to pick the
# submit control out of a form's buttons. Checked before falling back to input_type —
# a form often has more than one button (e.g. "Save record" and "Upload PDF"), and the
# name is a much stronger signal than type=submit, which the browser defaults onto
# almost every <button> regardless of intent (confirmed live: buggy_app's plain
# <button onclick=...> elements report input_type == "submit" even outside a <form>).
SUBMIT_NAME_HINTS = [
    "save", "submit", "search", "add", "create", "apply", "upload",
    "register", "log in", "login", "sign in", "sign up", "update",
    "continue", "confirm", "send",
]


@dataclass
class FormGroup:
    key: str                              # ElementInfo.form_key shared by every member
    fields: list[ElementInfo] = field(default_factory=list)   # non-button members
    buttons: list[ElementInfo] = field(default_factory=list)  # every button in the group
    submit: ElementInfo | None = None      # the button chosen to submit this form

    @property
    def fillable(self) -> list[ElementInfo]:
        return [f for f in self.fields if f.role in FILLABLE_ROLES]

    @property
    def checkable(self) -> list[ElementInfo]:
        return [f for f in self.fields if f.role in CHECKABLE_ROLES]


def _pick_submit(buttons: list[ElementInfo]) -> ElementInfo | None:
    if not buttons:
        return None
    for b in buttons:
        name_lower = (b.name or "").lower()
        if any(hint in name_lower for hint in SUBMIT_NAME_HINTS):
            return b
    submit_typed = [b for b in buttons if (b.input_type or "").lower() == "submit"]
    return submit_typed[-1] if submit_typed else buttons[-1]


def group_forms(model: PageModel) -> list[FormGroup]:
    """Clusters elements sharing a non-null form_key. Elements outside any <form>/
    <fieldset> ancestor (form_key is None) are left for the ordinary per-element crawl
    path in heuristic.py — this only handles genuine forms."""
    buckets: dict[str, list[ElementInfo]] = {}
    for el in model.elements:
        if not el.form_key:
            continue
        buckets.setdefault(el.form_key, []).append(el)

    groups: list[FormGroup] = []
    for key, elements in buckets.items():
        buttons = [e for e in elements if e.role == "button"]
        fields = [e for e in elements if e.role != "button"]
        groups.append(FormGroup(key=key, fields=fields, buttons=buttons, submit=_pick_submit(buttons)))
    return groups
