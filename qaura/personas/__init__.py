"""Persona registry — lets cli.py and core/planner_loop.py look personas up by the
string names used in `qaura run --personas curious,impatient` and config.py's
PersonaConfig.enabled list, without every call site importing five modules by hand.
"""
from qaura.personas.accessibility import PERSONA as ACCESSIBILITY
from qaura.personas.base import Persona
from qaura.personas.curious import PERSONA as CURIOUS
from qaura.personas.impatient import PERSONA as IMPATIENT
from qaura.personas.malicious import PERSONA as MALICIOUS
from qaura.personas.power_user import PERSONA as POWER_USER

REGISTRY: dict[str, Persona] = {
    p.name: p for p in (CURIOUS, IMPATIENT, MALICIOUS, POWER_USER, ACCESSIBILITY)
}


def get(name: str) -> Persona:
    try:
        return REGISTRY[name]
    except KeyError:
        raise ValueError(f"Unknown persona {name!r}. Known personas: {sorted(REGISTRY)}") from None


def resolve_all(names: list[str]) -> list[Persona]:
    return [get(n) for n in names]
