"""Provider-agnostic LLM interface. No call site outside llm/gemini.py should import
google.genai directly — swapping providers later, or adding a second one, means touching
one file."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel


class Tier(str, Enum):
    """Maps to a role in config.ModelTiers, not directly to a model ID.

    INVARIANT_CANDIDATES is used by `qaura init` (init/candidates.py) to propose
    business-rule invariants from a page's numeric inventory. It was a reserved tier for
    a long time before that existed.

    LOCALIZE is still reserved: analysis/localize.py is purely deterministic (substring
    search, no LLM), so nothing passes this tier to provider.complete() yet. `qaura
    doctor`'s reachability probe skips it rather than spending a live API call confirming
    quota for a tier nothing uses."""

    PLANNER = "planner"
    TRIAGE = "triage"
    LOCALIZE = "localize"
    FIX = "fix"
    ELEMENT_CLASSIFY = "element_classify"
    INVARIANT_CANDIDATES = "invariant_candidates"
    VISUAL_CONFIRM = "visual_confirm"


@dataclass
class ImagePart:
    mime_type: str
    data: bytes


@dataclass
class Usage:
    """Field names confirmed against a live call to google-genai's Interactions API
    (see llm/gemini.py:_extract_usage). Populate what each provider actually
    returns; leave the rest at 0."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    raw: dict = field(default_factory=dict)


@dataclass
class LLMResponse:
    text: str
    parsed: BaseModel | None
    usage: Usage
    session_id: str | None  # provider's conversation-chain handle, e.g. interaction.id
    raw: Any = None


class LLMProvider(Protocol):
    """One call shape covers plain completion, structured output, and vision input.
    `session` threads a provider-native conversation chain (Gemini's
    previous_interaction_id) so multi-step personas don't resend full history."""

    def complete(
        self,
        *,
        tier: Tier,
        system: str,
        input: str,
        schema: type[BaseModel] | None = None,
        images: list[ImagePart] | None = None,
        session: str | None = None,
    ) -> LLMResponse: ...

    @property
    def available(self) -> bool:
        """False for the null/heuristic provider, or a real provider with no credential."""
        ...
