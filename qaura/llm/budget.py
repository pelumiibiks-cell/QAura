"""Accumulates Gemini usage across a run and hard-stops once a cap is hit. Field names
confirmed live via `qaura doctor` against google-genai 2.20.0 — see gemini.py:_extract_usage.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from qaura.llm.base import LLMResponse, Tier


class BudgetExceeded(RuntimeError):
    def __init__(self, kind: str, used: float, cap: float) -> None:
        self.kind = kind
        self.used = used
        self.cap = cap
        super().__init__(f"Budget exceeded ({kind}): used {used}, cap {cap}")


@dataclass
class Budget:
    """Two independent caps, either can trip first. `max_calls` guards against a
    runaway loop making thousands of cheap calls; `max_tokens` guards against a smaller
    number of very expensive ones. Set either to None to disable that cap."""

    max_calls: int | None = None
    max_tokens: int | None = None

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    by_tier: dict[str, int] = field(default_factory=dict)  # tier -> call count

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def record(self, tier: Tier, response: LLMResponse) -> None:
        self.calls += 1
        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens
        self.by_tier[tier.value] = self.by_tier.get(tier.value, 0) + 1

    def check(self) -> None:
        """Call BEFORE attempting the next LLM call (every call site in the codebase
        does this — check(), then complete(), then record()) — raises once max_calls
        calls have already been recorded, so the orchestrator can stop cleanly before
        spending one more rather than after. Previously this compared with `>`, which
        against a pre-call check permits exactly one call over the configured cap."""
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise BudgetExceeded("calls", self.calls, self.max_calls)
        if self.max_tokens is not None and self.total_tokens >= self.max_tokens:
            raise BudgetExceeded("tokens", self.total_tokens, self.max_tokens)

    def summary(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "by_tier": dict(self.by_tier),
        }
