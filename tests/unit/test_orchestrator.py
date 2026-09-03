"""Unit-level coverage for PersonaOrchestrator's constructor wiring — orchestrator.run()
itself is a full async browser-driven loop (like heuristic.py's), out of scope for a
no-browser unit test; what's covered here is the actual regression this phase fixed:
cli.py building one RunLimiter/Budget and sharing it across every persona in a run,
rather than each PersonaOrchestrator building its own.
"""
from qaura import personas
from qaura.config import QAuraConfig
from qaura.core.guardrails import RunLimiter
from qaura.core.orchestrator import PersonaOrchestrator
from qaura.llm.budget import Budget


class _FakeProvider:
    available = False

    def complete(self, **kwargs):
        raise RuntimeError("not used by these tests")


def test_defaults_to_a_fresh_limiter_and_budget_when_not_given():
    cfg = QAuraConfig()
    orch = PersonaOrchestrator("http://x/", cfg, _FakeProvider(), personas.get("curious"))
    assert isinstance(orch.limiter, RunLimiter)
    assert isinstance(orch.budget, Budget)


def test_two_orchestrators_default_to_independent_instances():
    # The pre-fix behavior — each orchestrator building its own limiter/budget is
    # exactly what let a 5-persona run spend 5x the configured action/call caps.
    # Confirming the DEFAULT still gives independent instances matters just as much
    # as confirming sharing works below: cli.py's shared-instance fix has to be
    # something the caller opts into by passing limiter/budget, not something that
    # accidentally becomes global shared state for every construction.
    cfg = QAuraConfig()
    orch1 = PersonaOrchestrator("http://x/", cfg, _FakeProvider(), personas.get("curious"))
    orch2 = PersonaOrchestrator("http://x/", cfg, _FakeProvider(), personas.get("impatient"))
    assert orch1.limiter is not orch2.limiter
    assert orch1.budget is not orch2.budget


def test_shares_caller_supplied_limiter_and_budget_across_personas():
    # Regression: cli.py's _run_personas() now builds ONE RunLimiter and ONE Budget
    # for the whole `qaura run --personas ...` invocation and passes the same
    # instances into every persona's orchestrator — guardrails.py's own docstring
    # says a run with 5 personas shouldn't get 5x the action/call budget. Before
    # this fix, cli.py built 5 orchestrators, each with its own fresh limiter and
    # budget (plus a 6th independent Budget for the analysis step), so the shipped
    # defaults (300 calls / 500 actions / 1800s) actually permitted up to 1,800
    # calls, 3,000 actions, and 6x the wall-clock ceiling in one run.
    cfg = QAuraConfig()
    shared_limiter = RunLimiter(cfg=cfg.guardrails)
    shared_budget = Budget(max_calls=10)

    resolved = personas.resolve_all(["curious", "impatient", "malicious"])
    orchestrators = [
        PersonaOrchestrator("http://x/", cfg, _FakeProvider(), p, limiter=shared_limiter, budget=shared_budget)
        for p in resolved
    ]

    assert all(o.limiter is shared_limiter for o in orchestrators)
    assert all(o.budget is shared_budget for o in orchestrators)

    # Actions taken by one persona's orchestrator count against every other's view
    # of the same run-wide budget.
    orchestrators[0].limiter.record_action()
    orchestrators[1].limiter.record_action()
    assert orchestrators[2].limiter.actions_taken == 2
