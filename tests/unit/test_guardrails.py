import pytest

from qaura.browser.actions import Action, ActionKind
from qaura.browser.observe import ElementInfo, PageModel
from qaura.config import GuardrailConfig
from qaura.core.guardrails import (
    BudgetStop,
    GuardrailViolation,
    RunLimiter,
    check_action,
    enforce_scope_after_action,
    guard_goto,
    is_destructive,
    is_in_scope,
    sanitize_fill_value,
)


class _FakePage:
    """Minimal stand-in for a Playwright Page — just enough surface (`.url`, async
    `goto`/`go_back`) for guard_goto/enforce_scope_after_action, without needing a
    real browser for what's really just URL-comparison logic."""

    def __init__(self, url: str, back_url: str | None = None, back_raises: bool = False) -> None:
        self.url = url
        self.went_back = False
        self._back_url = back_url
        self._back_raises = back_raises

    async def goto(self, url: str) -> None:
        self.url = url

    async def go_back(self) -> None:
        self.went_back = True
        if self._back_raises:
            raise RuntimeError("no history to go back to")
        if self._back_url is not None:
            self.url = self._back_url


def _cfg(**overrides) -> GuardrailConfig:
    return GuardrailConfig(**overrides)


def test_in_scope_allows_matching_domain():
    cfg = _cfg(allowed_domains=["shop.test"])
    assert is_in_scope("http://shop.test/cart", cfg) is True


def test_in_scope_allows_subdomain():
    cfg = _cfg(allowed_domains=["shop.test"])
    assert is_in_scope("http://checkout.shop.test/pay", cfg) is True


def test_in_scope_blocks_other_domain():
    cfg = _cfg(allowed_domains=["shop.test"])
    assert is_in_scope("http://evil.test/phish", cfg) is False


def test_in_scope_no_allowed_domains_means_no_domain_restriction():
    cfg = _cfg(allowed_domains=[])
    assert is_in_scope("http://anywhere.test/x", cfg) is True


def test_in_scope_strips_port_before_matching():
    # Regression: urlsplit(...).netloc includes the port ("localhost:8099"), which
    # never matched a bare allowed_domains entry like "localhost" — silently
    # defeating the domain guardrail for every port-qualified target, which is how
    # both shipped example configs point at their local dev targets.
    cfg = _cfg(allowed_domains=["localhost"])
    assert is_in_scope("http://localhost:8099/dashboard", cfg) is True
    assert is_in_scope("http://evil.test:8099/x", cfg) is False


def test_blocked_path_overrides_allowed():
    cfg = _cfg(allowed_domains=["shop.test"], allowed_paths=["/**"], blocked_paths=["/admin/**"])
    assert is_in_scope("http://shop.test/admin/danger", cfg) is False
    assert is_in_scope("http://shop.test/cart", cfg) is True


def test_is_destructive_matches_configured_pattern():
    cfg = _cfg()
    el = ElementInfo(ref="e1", role="button", name="Delete account")
    assert is_destructive(el, cfg) is True


def test_is_destructive_false_for_safe_button():
    cfg = _cfg()
    el = ElementInfo(ref="e1", role="button", name="Add to cart")
    assert is_destructive(el, cfg) is False


def test_check_action_blocks_destructive_when_allowed_but_persona_does_not_attempt():
    # Regression: Persona.attempts_destructive was set on every persona and asserted
    # by test_personas.py, but check_action() never consulted it — so with
    # allow_destructive=true, every persona performed destructive actions equally,
    # including ones with no business doing so (e.g. "accessibility" clicking a real
    # delete button).
    from qaura.personas.base import Persona

    cfg = _cfg(allow_destructive=True)
    persona = Persona(name="accessibility", system_prompt="x", attempts_destructive=False)
    model = PageModel(url="http://shop.test", title="t", elements=[
        ElementInfo(ref="e1", role="button", name="Delete account"),
    ])
    action = Action(kind=ActionKind.CLICK, ref="e1")
    with pytest.raises(GuardrailViolation):
        check_action(action, model, cfg, persona=persona)


def test_check_action_allows_destructive_when_persona_attempts_it():
    from qaura.personas.base import Persona

    cfg = _cfg(allow_destructive=True)
    persona = Persona(name="malicious", system_prompt="x", attempts_destructive=True)
    model = PageModel(url="http://shop.test", title="t", elements=[
        ElementInfo(ref="e1", role="button", name="Delete account"),
    ])
    action = Action(kind=ActionKind.CLICK, ref="e1")
    check_action(action, model, cfg, persona=persona)  # should not raise


def test_check_action_persona_none_preserves_cfg_only_behavior():
    # heuristic.py has no Persona objects at all — persona=None must behave exactly
    # like the pre-persona-gating code did.
    cfg = _cfg(allow_destructive=True)
    model = PageModel(url="http://shop.test", title="t", elements=[
        ElementInfo(ref="e1", role="button", name="Delete account"),
    ])
    action = Action(kind=ActionKind.CLICK, ref="e1")
    check_action(action, model, cfg, persona=None)  # should not raise


def test_check_action_blocks_destructive_click_by_default():
    cfg = _cfg()
    model = PageModel(url="http://shop.test", title="t", elements=[
        ElementInfo(ref="e1", role="button", name="Cancel order"),
    ])
    action = Action(kind=ActionKind.CLICK, ref="e1")
    with pytest.raises(GuardrailViolation):
        check_action(action, model, cfg)


def test_check_action_allows_destructive_when_configured():
    cfg = _cfg(allow_destructive=True)
    model = PageModel(url="http://shop.test", title="t", elements=[
        ElementInfo(ref="e1", role="button", name="Cancel order"),
    ])
    action = Action(kind=ActionKind.CLICK, ref="e1")
    check_action(action, model, cfg)  # should not raise


def test_check_action_blocks_navigate_out_of_scope():
    cfg = _cfg(allowed_domains=["shop.test"])
    model = PageModel(url="http://shop.test", title="t", elements=[])
    action = Action(kind=ActionKind.NAVIGATE, value="http://evil.test/")
    with pytest.raises(GuardrailViolation):
        check_action(action, model, cfg)


def test_check_action_raises_on_stale_ref():
    cfg = _cfg()
    model = PageModel(url="http://shop.test", title="t", elements=[])
    action = Action(kind=ActionKind.CLICK, ref="e404")
    with pytest.raises(GuardrailViolation):
        check_action(action, model, cfg)


def test_sanitize_fill_value_replaces_card_number():
    cfg = _cfg()
    el = ElementInfo(ref="e1", role="textbox", name="Card number")
    result = sanitize_fill_value("4111111111111111", el, cfg)
    assert result == cfg.test_card_number


def test_sanitize_fill_value_replaces_email_domain():
    cfg = _cfg(fake_email_domain="qaura.invalid")
    el = ElementInfo(ref="e1", role="textbox", name="Email address")
    result = sanitize_fill_value("realuser@gmail.com", el, cfg)
    assert result == "realuser@qaura.invalid"


def test_sanitize_fill_value_leaves_unrelated_fields_alone():
    cfg = _cfg()
    el = ElementInfo(ref="e1", role="textbox", name="Search")
    assert sanitize_fill_value("hello world", el, cfg) == "hello world"


def test_run_limiter_stops_on_action_cap():
    cfg = _cfg(max_actions_per_run=2, max_wall_clock_seconds=999)
    limiter = RunLimiter(cfg=cfg)
    limiter.check_before_action()
    limiter.record_action()
    limiter.check_before_action()
    limiter.record_action()
    with pytest.raises(BudgetStop):
        limiter.check_before_action()


def test_run_limiter_stops_on_wall_clock_cap():
    cfg = _cfg(max_wall_clock_seconds=0, max_actions_per_run=999)
    limiter = RunLimiter(cfg=cfg)
    with pytest.raises(BudgetStop):
        limiter.check_before_action()


@pytest.mark.asyncio
async def test_guard_goto_allows_in_scope_target():
    cfg = _cfg(allowed_domains=["shop.test"])
    page = _FakePage(url="about:blank")
    await guard_goto(page, "http://shop.test/cart", cfg)
    assert page.url == "http://shop.test/cart"


@pytest.mark.asyncio
async def test_guard_goto_blocks_out_of_scope_target():
    # Regression: every real navigation in the codebase used to call page.goto()
    # directly, so is_in_scope() was only reachable via ActionKind.NAVIGATE — an
    # action kind nothing ever emits. guard_goto is the actual choke point now.
    cfg = _cfg(allowed_domains=["shop.test"])
    page = _FakePage(url="http://shop.test/")
    with pytest.raises(GuardrailViolation):
        await guard_goto(page, "http://evil.test/phish", cfg)
    assert page.url == "http://shop.test/"  # never navigated


@pytest.mark.asyncio
async def test_enforce_scope_after_action_steps_back_on_drift():
    # Regression: a CLICK on an <a href="https://external"> was checked only against
    # destructive_patterns — never against allowed_domains — so the crawler would
    # follow it and keep fuzzing forms on the external site under this run's auth
    # cookies. This is the post-hoc catch for exactly that.
    cfg = _cfg(allowed_domains=["shop.test"])
    page = _FakePage(url="http://evil.test/phish", back_url="http://shop.test/")
    stepped_back = await enforce_scope_after_action(page, cfg)
    assert stepped_back is True
    assert page.went_back is True


@pytest.mark.asyncio
async def test_enforce_scope_after_action_noop_when_in_scope():
    cfg = _cfg(allowed_domains=["shop.test"])
    page = _FakePage(url="http://shop.test/cart")
    stepped_back = await enforce_scope_after_action(page, cfg)
    assert stepped_back is False
    assert page.went_back is False


@pytest.mark.asyncio
async def test_enforce_scope_after_action_raises_when_back_fails():
    cfg = _cfg(allowed_domains=["shop.test"])
    page = _FakePage(url="http://evil.test/phish", back_raises=True)
    with pytest.raises(GuardrailViolation, match="could not step back"):
        await enforce_scope_after_action(page, cfg)


@pytest.mark.asyncio
async def test_enforce_scope_after_action_raises_when_back_is_still_out_of_scope():
    cfg = _cfg(allowed_domains=["shop.test"])
    page = _FakePage(url="http://evil.test/b", back_url="http://evil.test/a")
    with pytest.raises(GuardrailViolation):
        await enforce_scope_after_action(page, cfg)


def _form_model(button_name: str) -> PageModel:
    return PageModel(url="http://shop.test/account", title="t", elements=[
        ElementInfo(ref="e1", role="textbox", name="Confirmation", form_key="f1"),
        ElementInfo(ref="e2", role="button", name=button_name, form_key="f1"),
    ])


def test_enter_in_a_destructive_form_is_blocked():
    # Regression: only the focused field's name was checked, so Enter submitted "Delete account" forms
    with pytest.raises(GuardrailViolation):
        check_action(Action(kind=ActionKind.KEY, ref="e1", value="Enter"), _form_model("Delete account"), _cfg())


def test_enter_in_a_harmless_form_is_allowed():
    check_action(Action(kind=ActionKind.KEY, ref="e1", value="Enter"), _form_model("Search"), _cfg())


def test_tab_in_a_destructive_form_is_allowed():
    check_action(Action(kind=ActionKind.KEY, ref="e1", value="Tab"), _form_model("Delete account"), _cfg())


@pytest.mark.parametrize("name", [
    "Archive project", "Revoke token", "Refund payment", "Withdraw funds",
    "Transfer ownership", "Disable two-factor", "Reset password",
])
def test_new_destructive_patterns_are_blocked_by_default(name):
    assert is_destructive(ElementInfo(ref="e1", role="button", name=name), _cfg())


def test_in_scope_domain_match_ignores_case():
    cfg = _cfg(allowed_domains=["Shop.Test"])
    assert is_in_scope("http://SHOP.test/cart", cfg) is True


def test_blocked_path_matches_percent_encoded_and_uppercase_paths():
    cfg = _cfg(allowed_domains=["shop.test"], blocked_paths=["/logout*"])
    assert is_in_scope("http://shop.test/log%6Fut", cfg) is False
    assert is_in_scope("http://shop.test/LOGOUT", cfg) is False
    assert is_in_scope("http://shop.test/login", cfg) is True


@pytest.mark.asyncio
async def test_throttle_awaits_between_actions():
    limiter = RunLimiter(cfg=_cfg(max_requests_per_second=20.0))
    assert await limiter.throttle() == 0.0
    slept = await limiter.throttle()
    assert 0 < slept <= 0.05
