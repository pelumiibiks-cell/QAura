import pytest

from qaura import personas


def test_registry_has_all_five_plan_personas():
    assert set(personas.REGISTRY) == {"curious", "impatient", "malicious", "power_user", "accessibility"}


def test_every_persona_has_a_nonempty_system_prompt():
    for name, persona in personas.REGISTRY.items():
        assert persona.system_prompt.strip(), f"{name} has an empty system prompt"
        assert len(persona.system_prompt) > 50, f"{name}'s system prompt looks too thin"


def test_get_returns_named_persona():
    p = personas.get("curious")
    assert p.name == "curious"


def test_get_raises_on_unknown_name():
    with pytest.raises(ValueError, match="Unknown persona"):
        personas.get("nonexistent")


def test_resolve_all_preserves_order():
    result = personas.resolve_all(["impatient", "curious"])
    assert [p.name for p in result] == ["impatient", "curious"]


def test_no_persona_attempts_destructive_by_default():
    # Safety-relevant: none of the five should default to attempts_destructive=True —
    # that's a guardrail-level decision (config.guardrails.allow_destructive), not
    # something a persona prompt should quietly enable on its own.
    for name, persona in personas.REGISTRY.items():
        assert persona.attempts_destructive is False, f"{name} defaults to attempting destructive actions"
