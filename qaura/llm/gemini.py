"""Gemini implementation of LLMProvider, via the google-genai SDK's Interactions API
(client.interactions.create — NOT the older client.models.generate_content).

Two things were wrong on first live contact and are now corrected (see docs/PROGRESS.md
Phase 0/3 notes for the full story, not repeated here):
  - Usage field names: real ones are `total_input_tokens`/`total_output_tokens`, not
    `input_tokens`/`output_tokens` (confirmed via `qaura doctor`, Phase 0).
  - System prompt kwarg is `system_instruction`, not `instructions` — confirmed by
    inspecting `inspect.getdoc(client.interactions.create)` and the generated
    TypedDict in `google.genai._gaos.types.interactions.createmodelinteraction`
    (Phase 3). `response_format` itself (`{"type": "text", "mime_type": ...,
    "schema": ...}`) was already correct — only the system-prompt kwarg needed
    fixing. Do NOT pass `response_mime_type` — it's deprecated on this SDK version;
    the mime type belongs inside `response_format` only.

Quota fallback: some accounts have zero free-tier quota for Pro-tier models
(confirmed live: `gemini-3.1-pro` returned 429 "limit: 0"). Rather than let every
Pro-tier call in the run fail, `complete()` catches a 429 rate-limit error, logs a
warning once per tier, and permanently downgrades that tier to the `element_classify`
model (Flash-Lite — confirmed working) for the rest of this provider instance's life.
This does not change `config.py`'s `ModelTiers` defaults, which stay Pro-for-planning
per the user's explicit choice — it's a runtime resilience measure, not a design
change, and a fresh run (or fixed billing) goes back to using Pro normally.
"""
from __future__ import annotations

import json
import logging
import time

from pydantic import BaseModel

from qaura.llm.base import ImagePart, LLMResponse, Tier, Usage

_log = logging.getLogger("qaura.llm.gemini")

# Bounded retry for anything that isn't the 429-quota path (already handled
# separately, permanently, per tier). A transient network blip or a 5xx shouldn't
# cost a whole crawl/persona-run/triage-pass its one LLM call — but nothing here
# should retry forever either, since every retry is a real API call against the
# run's own action/wall-clock budget.
_MAX_RETRIES = 2
_RETRY_BACKOFF_SECONDS = 1.0
_REQUEST_TIMEOUT_MS = 60_000


def _rate_limit_error_class():
    """Deferred, defensive import — RateLimitError lives in a private (`_gaos`)
    module path that could move in a future google-genai release. Returns None if
    unavailable, so a caller checking `isinstance(e, cls)` degrades to "don't treat
    this as a rate limit" rather than raising an unrelated ImportError. One shared
    helper instead of three separate copies of the same try/except."""
    try:
        from google.genai._gaos.lib.compat_errors import RateLimitError
        return RateLimitError
    except ImportError:
        return None


class GeminiProvider:
    def __init__(self, api_key: str | None, model_tiers) -> None:
        self._api_key = api_key
        self._model_tiers = model_tiers
        self._client = None  # lazy: don't import/construct google.genai until needed
        self._quota_downgraded: set[Tier] = set()
        self._warned_no_rate_limit_class = False

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def _client_or_raise(self):
        if not self.available:
            raise RuntimeError("GeminiProvider used with no GEMINI_API_KEY set.")
        if self._client is None:
            from google import genai

            # Pass explicitly rather than relying on env var pickup, since config.py
            # is the single source of truth for where the key came from (.env, env,
            # or qaura.yaml is NOT a valid source for a secret).
            try:
                from google.genai import types
                # Previously no timeout at all — a hung connection to Gemini blocked
                # the crawl/persona-run/analysis step indefinitely rather than
                # failing fast into the retry loop below. Defensive like the
                # RateLimitError import elsewhere in this file: a future SDK version
                # changing this shape shouldn't break client construction, just skip
                # setting an explicit timeout.
                http_options = types.HttpOptions(timeout=_REQUEST_TIMEOUT_MS)
                self._client = genai.Client(api_key=self._api_key, http_options=http_options)
            except Exception:
                _log.debug("could not set an explicit request timeout on genai.Client", exc_info=True)
                self._client = genai.Client(api_key=self._api_key)
        return self._client

    def _model_for(self, tier: Tier) -> str:
        if tier in self._quota_downgraded and tier != Tier.ELEMENT_CLASSIFY:
            return self._model_tiers.element_classify
        return getattr(self._model_tiers, tier.value)

    def _create_with_quota_fallback(self, client, tier: Tier, model: str, kwargs: dict):
        """Isolates the 429-quota-exceeded retry so `complete()` stays readable.
        Import of the error class is deferred and defensive: it lives in a private
        (`_gaos`) module path that could move in a future google-genai release —
        if the import itself fails, this just re-raises whatever the SDK threw
        rather than masking a real error behind an ImportError.
        """
        try:
            return client.interactions.create(model=model, **kwargs)
        except Exception as e:
            rate_limit_cls = _rate_limit_error_class()
            if rate_limit_cls is None:
                if not self._warned_no_rate_limit_class:
                    # This is the mechanism that makes zero-Pro-quota accounts
                    # (a real, previously-hit case — see config.py's model_tiers
                    # comment) usable at all. If the SDK ever moves this class, this
                    # used to fail silently — no error, no downgrade, just every
                    # Pro-tier call raising for the rest of the run with no
                    # explanation of why the fallback that's supposed to catch it
                    # didn't.
                    _log.warning(
                        "Could not import google.genai's RateLimitError — quota-fallback "
                        "downgrade will not work this run. If Gemini calls start failing "
                        "with 429s, check whether the installed google-genai version moved "
                        "this class and update llm/gemini.py."
                    )
                    self._warned_no_rate_limit_class = True
                raise

            if not isinstance(e, rate_limit_cls) or tier in self._quota_downgraded:
                raise

            fallback_model = self._model_tiers.element_classify
            _log.warning(
                "Gemini tier %r (model %r) hit a quota/rate limit — falling back to "
                "%r (element_classify) for the rest of this run. Original error: %s",
                tier.value, model, fallback_model, e,
            )
            self._quota_downgraded.add(tier)
            if model == fallback_model:
                raise  # already on the fallback model, nothing cheaper to retry with
            return client.interactions.create(model=fallback_model, **kwargs)

    def _create_with_retry(self, client, tier: Tier, model: str, kwargs: dict):
        """Bounded retry with exponential backoff around _create_with_quota_fallback,
        for transient failures (network blips, 5xx) — NOT for rate limits, which
        already have their own permanent per-tier downgrade above and would just fail
        the same way again immediately. Previously there was no retry (or request
        timeout — see _client_or_raise) at all: a single bad connection propagated
        straight out of whichever call site made it — a per-action detector, a
        persona's planner turn, a triage/fix pass — ending that crawl/run/pass
        outright rather than degrading."""
        rate_limit_cls = _rate_limit_error_class()
        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return self._create_with_quota_fallback(client, tier, model, kwargs)
            except Exception as e:
                last_error = e
                is_rate_limit = rate_limit_cls is not None and isinstance(e, rate_limit_cls)
                if is_rate_limit or attempt == _MAX_RETRIES:
                    raise
                _log.debug(
                    "Gemini call failed (attempt %d/%d) — retrying: %s",
                    attempt + 1, _MAX_RETRIES + 1, e,
                )
                time.sleep(_RETRY_BACKOFF_SECONDS * (2**attempt))
        raise last_error  # pragma: no cover — loop always returns or raises above

    def complete(
        self,
        *,
        tier: Tier,
        system: str,
        input: str,
        schema: type[BaseModel] | None = None,
        images: list[ImagePart] | None = None,
        session: str | None = None,
    ) -> LLMResponse:
        client = self._client_or_raise()
        model = self._model_for(tier)

        parts: list = []
        if images:
            for img in images:
                parts.append({
                    "type": "image",
                    "mime_type": img.mime_type,
                    "data": img.data,
                })
        content = input if not parts else [*parts, {"type": "text", "text": input}]

        kwargs: dict = {"input": content}
        if system:
            kwargs["system_instruction"] = system
        if session:
            kwargs["previous_interaction_id"] = session
        if schema is not None:
            kwargs["response_format"] = {
                "type": "text",
                "mime_type": "application/json",
                "schema": schema.model_json_schema(),
            }

        interaction = self._create_with_retry(client, tier, model, kwargs)

        text = getattr(interaction, "output_text", "") or ""
        parsed = None
        if schema is not None and text:
            parsed = schema.model_validate_json(text)

        usage = _extract_usage(interaction)
        session_id = getattr(interaction, "id", None)

        return LLMResponse(
            text=text, parsed=parsed, usage=usage, session_id=session_id, raw=interaction,
        )


def _extract_usage(interaction) -> Usage:
    """Confirmed against a live `qaura doctor` call (2026-08-30) against
    google-genai 2.20.0's Interactions API. The real usage object has these fields
    (raw dump from that run):
        input_tokens_by_modality, total_cached_tokens, total_input_tokens,
        total_output_tokens, total_thought_tokens, total_tokens,
        total_tool_use_tokens, raw_prompt_token
    `total_input_tokens`/`total_output_tokens` are the ones that matter here — the
    earlier guessed names (`input_tokens`, `prompt_tokens`, ...) do not exist on this
    object and silently returned 0. Candidate names are still tried in fallback order
    in case a future SDK version renames these, but the *_total_* names are primary."""
    raw_usage = getattr(interaction, "usage", None)
    if raw_usage is None:
        return Usage()

    def _get(*names: str) -> int:
        for n in names:
            v = getattr(raw_usage, n, None)
            if isinstance(v, int):
                return v
        return 0

    input_tokens = _get("total_input_tokens", "input_tokens", "prompt_tokens")
    output_tokens = _get("total_output_tokens", "output_tokens", "completion_tokens")
    total_tokens = _get("total_tokens", "total_token_count") or (input_tokens + output_tokens)

    raw_dict = {}
    try:
        raw_dict = raw_usage.model_dump() if hasattr(raw_usage, "model_dump") else vars(raw_usage)
    except Exception:
        raw_dict = {"repr": repr(raw_usage)}

    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        raw=raw_dict,
    )


def _probe_model_quota(client, model_id: str) -> str:
    """Returns 'ok', 'quota_blocked', or an error message string. A minimal, cheap
    call — this is the only reliable way to learn about quota, since a model
    appearing in `models.list()` says nothing about whether THIS account's plan can
    actually call it (confirmed live: gemini-3.1-pro was listed as reachable and
    still 429'd with limit:0)."""
    try:
        client.interactions.create(model=model_id, input="ok")
        return "ok"
    except Exception as e:
        rate_limit_cls = _rate_limit_error_class()
        if rate_limit_cls is not None and isinstance(e, rate_limit_cls):
            return "quota_blocked"
        return str(e)[:120]


def run_doctor(cfg, console) -> None:
    """`qaura doctor`. Three checks, each reported independently so a partial failure
    (e.g. key valid but a specific model unreachable) is still actionable:
      1. Is GEMINI_API_KEY present at all.
      2. Can we construct a client and list models.
      3. Does one real interactions.create() call succeed, and what does usage look like.
    """
    if not cfg.gemini_api_key:
        console.print("[red]No GEMINI_API_KEY found[/red] (checked .env and environment).")
        console.print("QAura will run in [yellow]heuristic (no-LLM) mode[/yellow] until one is set.")
        console.print("Set it in .env as: GEMINI_API_KEY=<your key>")
        return

    console.print(f"[green]GEMINI_API_KEY found[/green] ({len(cfg.gemini_api_key)} chars, not printed).")

    try:
        from google import genai
    except ImportError:
        console.print("[red]google-genai is not installed.[/red] Run: pip install -e \".[dev,ml]\"")
        raise SystemExit(1)

    try:
        client = genai.Client(api_key=cfg.gemini_api_key)
    except Exception as e:
        console.print(f"[red]Failed to construct genai.Client:[/red] {e}")
        raise SystemExit(1)

    console.print("\n[bold]Reachable models:[/bold]")
    reachable: list[str] = []
    try:
        models = client.models.list()
        for m in models:
            name = getattr(m, "name", None) or getattr(m, "id", str(m))
            reachable.append(name)
        for name in reachable[:30]:
            console.print(f"  {name}")
        if len(reachable) > 30:
            console.print(f"  ... and {len(reachable) - 30} more")
    except Exception as e:
        console.print(f"[yellow]Could not list models:[/yellow] {e}")
        console.print("(Non-fatal — some accounts/SDK versions don't expose models.list().)")

    console.print("\n[bold]Checking configured tier models — reachability AND quota:[/bold]")
    console.print("[dim](makes one minimal live call per distinct model; this is what actually caught the Pro-tier quota-0 issue — a reachable-list check alone would have missed it)[/dim]")
    # localize excluded: configured (ModelTiers has a field for it) but nothing calls
    # provider.complete() with that tier — analysis/localize.py is deterministic (see
    # llm/base.py:Tier's docstring) — so probing it would spend a live API call
    # confirming quota for a model nothing actually uses yet. invariant_candidates IS
    # probed: `qaura init` calls it, and a user finding out mid-recon that the tier is
    # quota-blocked is exactly what doctor exists to prevent.
    configured = {
        "planner": cfg.model_tiers.planner,
        "planner_fallback": cfg.model_tiers.planner_fallback,
        "triage": cfg.model_tiers.triage,
        "fix": cfg.model_tiers.fix,
        "element_classify": cfg.model_tiers.element_classify,
        "invariant_candidates": cfg.model_tiers.invariant_candidates,
        "visual_confirm": cfg.model_tiers.visual_confirm,
    }
    quota_blocked: list[str] = []
    probed: dict[str, str] = {}  # model_id -> status, so distinct models shared by multiple roles are probed once
    for role, model_id in configured.items():
        if model_id not in probed:
            probed[model_id] = _probe_model_quota(client, model_id)
        status = probed[model_id]
        if status == "quota_blocked":
            mark = "[red]QUOTA BLOCKED (429, limit 0)[/red]"
            quota_blocked.append(role)
        elif status == "ok":
            mark = "[green]OK (reachable + live call succeeded)[/green]"
        else:
            mark = f"[yellow]error: {status}[/yellow]"
        console.print(f"  {role:24s} {model_id:28s} {mark}")

    if quota_blocked:
        console.print(
            f"\n[yellow]{len(quota_blocked)} tier(s) are quota-blocked on this account: "
            f"{', '.join(quota_blocked)}.[/yellow]"
        )
        console.print(
            "[dim]GeminiProvider will automatically fall back to the element_classify "
            "model at runtime when this happens (see llm/gemini.py) — a run will still "
            "work, just at lower quality on the affected tiers, until billing is "
            "enabled or qaura.yaml's model_tiers are pointed at working models.[/dim]"
        )

    console.print("\n[bold]Making one live call to confirm the Interactions API shape...[/bold]")
    try:
        interaction = client.interactions.create(
            model=cfg.model_tiers.element_classify,  # cheapest tier for this smoke test
            input="Reply with exactly the word: ok",
        )
    except Exception as e:
        console.print(f"[red]interactions.create() failed:[/red] {e}")
        console.print(
            "[yellow]This means the Interactions API call shape assumed by "
            "qaura/llm/gemini.py does not match the installed google-genai version. "
            "Check the installed SDK's actual surface (client.interactions vs "
            "client.models.generate_content) and update gemini.py + docs/PROGRESS.md.[/yellow]"
        )
        raise SystemExit(1)

    text = getattr(interaction, "output_text", None)
    console.print(f"  output_text: {text!r}")

    usage = _extract_usage(interaction)
    console.print(f"  parsed usage: input={usage.input_tokens} output={usage.output_tokens} total={usage.total_tokens}")
    console.print(f"  raw usage object dump: {json.dumps(usage.raw, default=str)[:500]}")
    console.print(f"  interaction.id (session handle): {getattr(interaction, 'id', None)!r}")

    console.print("\n[bold green]qaura doctor: Gemini call path confirmed working.[/bold green]")
    console.print(
        "If the parsed usage numbers above look wrong (all zero when a real call was made), "
        "the field names in llm/gemini.py:_extract_usage need correcting — see the raw dump."
    )
