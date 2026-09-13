"""Config model: env vars (via .env) layered under an optional qaura.yaml.

Precedence, highest first: process env vars > .env file > qaura.yaml > field defaults.
pydantic-settings gives us the first two for free; qaura.yaml is merged in manually
before construction since its keys are nested (personas, guardrails, model tiers) rather
than flat env-var shaped.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_FILENAMES = ("qaura.yaml", "qaura.yml")


class ConfigError(RuntimeError):
    """A qaura.yaml (or --config file) that exists but can't be used — malformed
    YAML or a value that doesn't fit QAuraConfig's schema. Distinct from the file
    simply not existing, which load_config() treats as "use defaults", not an error."""


class ModelTiers(BaseModel):
    """Gemini model IDs per role. Never hardcode a model ID at a call site — read it
    from here, since `qaura doctor` is what confirms these are actually reachable.

    `planner_fallback` was originally `gemini-2.5-pro` — confirmed live via
    `qaura doctor` that this model is fully retired on this account
    (`404: no longer available to new users`), not just quota-limited. Changed to
    `gemini-pro-latest`, a rolling alias Google keeps pointed at a current Pro model,
    which is less likely to go stale the same way a dated snapshot did. Also
    live-confirmed reachable + callable. This is a different problem from the
    quota-0 issue on `planner`/`triage`/`localize`/`fix` (GeminiProvider's runtime
    fallback in llm/gemini.py handles that one) — a dead model ID needs fixing here,
    at the source, not papered over at call time."""

    planner: str = "gemini-3.1-pro-preview"
    planner_fallback: str = "gemini-pro-latest"
    triage: str = "gemini-3.1-pro-preview"
    localize: str = "gemini-3.1-pro-preview"
    fix: str = "gemini-3.1-pro-preview"
    element_classify: str = "gemini-3.5-flash-lite"
    invariant_candidates: str = "gemini-3.5-flash-lite"
    visual_confirm: str = "gemini-3.7-flash"


class GuardrailConfig(BaseModel):
    """Executed in code by qaura/core/guardrails.py — never trust the prompt alone."""

    allowed_domains: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=lambda: ["/**"])
    blocked_paths: list[str] = Field(default_factory=list)
    destructive_patterns: list[str] = Field(
        default_factory=lambda: [
            "delete", "remove", "purge", "cancel", "deactivate",
            "pay", "place order", "checkout", "confirm order",
            "unsubscribe", "close account", "terminate",
            # Signing out discards an authenticated session and strands the rest of
            # the run on the login wall — never click it during exploration.
            "sign out", "signout", "log out", "logout", "log off",
        ]
    )
    allow_destructive: bool = False
    max_actions_per_run: int = 500
    max_requests_per_second: float = 5.0
    max_wall_clock_seconds: int = 1800
    max_llm_calls_per_run: int = 300
    fake_email_domain: str = "qaura.invalid"
    test_card_number: str = "4242424242424242"  # Stripe test card


class InvariantConfig(BaseModel):
    """Declarative business-rule check, evaluated after every action (core/invariants.py).
    This is the plan's design decision #4 — the only mechanism that catches a bug like
    'checkout total goes stale after a coupon' (no crash, no console error, no failed
    network call, nothing an LLM divergence check would obviously flag either).

    `values` maps a name to a CSS selector. A selector matching exactly one element
    extracts a single number (parsed from its text content); a selector matching
    multiple elements extracts a list of numbers, letting `expression` use `sum(...)`
    over it. `expression` is NOT arbitrary Python — core/invariants.py evaluates it
    through a restricted AST walker (comparisons, arithmetic, boolean ops, and a small
    whitelist of builtins: sum/min/max/abs/len/round). No comprehensions, no attribute
    access, no arbitrary calls — a user hand-writes these in qaura.yaml, and this file
    format should not be a footgun even though it's config, not application code.

    This is a narrower design than the original plan sketch (which imagined arbitrary
    nested objects like `cart.total` and generator expressions like
    `sum(item.price * item.qty for item in cart.items)`) — that shape isn't safely
    evaluable without either a real object model extracted from the page (its own
    project) or actual eval() (a real footgun for a config file). Flat named numeric
    values plus a restricted comparison expression covers the plan's own examples
    (cart total vs. line-item sum, discount-never-increases) without either problem."""

    name: str
    description: str
    container_selector: str | None = None  # if set, `values` selectors are scoped to
                                              # the first match of this selector
    values: dict[str, str] = Field(default_factory=dict)  # name -> CSS selector
    expression: str


class PersonaConfig(BaseModel):
    enabled: list[str] = Field(
        default_factory=lambda: [
            "curious", "impatient", "malicious", "power_user", "accessibility",
        ]
    )


class AuthRole(BaseModel):
    name: str
    storage_state_path: str | None = None
    is_admin: bool = False  # true for a role that's EXPECTED to reach `admin_paths`


class QAuraConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="QAURA_",
        extra="ignore",
    )

    # Gemini credential — read directly as GEMINI_API_KEY, not QAURA_-prefixed,
    # since that's the exact name genai.Client() looks for with no args.
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")

    target_url: str | None = None
    repo_path: str | None = None
    output_dir: str = "runs"
    seed: int = 0

    model_tiers: ModelTiers = Field(default_factory=ModelTiers)
    guardrails: GuardrailConfig = Field(default_factory=GuardrailConfig)
    personas: PersonaConfig = Field(default_factory=PersonaConfig)
    invariants: list[InvariantConfig] = Field(default_factory=list)
    auth_roles: list[AuthRole] = Field(default_factory=list)
    # Paths that should require an admin-level session — Phase E wiring for the
    # previously-implemented-but-never-called check_cross_role_access(): `qaura run
    # --role <non-admin>` visits each of these under that session afterward and
    # reports a finding if nothing looks like it blocked access. Empty by default
    # (opt-in — QAura can't infer which routes are privileged from the page alone).
    admin_paths: list[str] = Field(default_factory=list)

    llm_mode: Literal["auto", "gemini", "heuristic"] = "auto"

    @property
    def has_llm_credential(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def effective_llm_mode(self) -> Literal["gemini", "heuristic"]:
        if self.llm_mode == "heuristic":
            return "heuristic"
        if self.llm_mode == "gemini":
            return "gemini"
        return "gemini" if self.has_llm_credential else "heuristic"


def _find_config_file(start: Path) -> Path | None:
    for name in DEFAULT_CONFIG_FILENAMES:
        candidate = start / name
        if candidate.exists():
            return candidate
    return None


def load_config(config_path: str | Path | None = None, cwd: Path | None = None) -> QAuraConfig:
    """Load qaura.yaml (if present) merged under env-derived settings.

    yaml values are treated as defaults; actual env vars (including .env) still win,
    since QAuraConfig's own env loading runs after we seed it with yaml-derived kwargs.
    """
    cwd = cwd or Path.cwd()
    path = Path(config_path) if config_path else _find_config_file(cwd)

    yaml_data: dict = {}
    if path and path.exists():
        with open(path, "r", encoding="utf-8") as f:
            try:
                yaml_data = yaml.safe_load(f) or {}
            except yaml.YAMLError as e:
                raise ConfigError(f"{path}: invalid YAML — {e}") from e
        if not isinstance(yaml_data, dict):
            raise ConfigError(
                f"{path}: expected a YAML mapping at the top level (key: value pairs), "
                f"got {type(yaml_data).__name__}"
            )

    try:
        cfg = QAuraConfig(**yaml_data)
    except ValidationError as e:
        raise ConfigError(f"{path or '(no config file)'}: {e}") from e

    # Deferred import: core/invariants.py imports this module
    from qaura.core.invariants import InvariantError, validate_invariant

    for invariant in cfg.invariants:
        try:
            validate_invariant(invariant)
        except InvariantError as e:
            raise ConfigError(f"{path or '(no config file)'}: invariant {invariant.name!r}: {e}") from e
    return cfg
