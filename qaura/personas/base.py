"""Persona = a system prompt plus a few behavioral knobs. Kept deliberately thin —
the actual differentiation between personas is almost entirely in what the system
prompt tells the LLM to prioritize; the knobs here are just the parts that need to be
checked in code rather than left to the model to self-regulate (destructive-action
appetite in particular must never depend on the model choosing correctly).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Persona:
    name: str
    system_prompt: str
    # Whether this persona is allowed to attempt destructive-classified actions at
    # all — still gated by config.guardrails.allow_destructive on top of this; a
    # persona saying True here doesn't bypass the run-wide guardrail config, it just
    # means it won't be needlessly restricted below what the run already allows.
    attempts_destructive: bool = False
    prefers_unvisited: bool = True
    detector_hints: tuple[str, ...] = field(default_factory=tuple)  # informational, for reports
