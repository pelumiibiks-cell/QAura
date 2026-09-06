"""Live tests against tests/fixtures/buggy_app.

These need a real browser and a real server: selector generation reads computed styles
and match counts, and the read-only guarantee is a claim about actual network traffic
that only real traffic can verify. Skipped automatically if the fixture is not up.
"""
import httpx
import pytest

from qaura.browser.driver import ContextSpec, Driver
from qaura.browser.numerics import scan_numeric_elements
from qaura.browser.observe import build_page_model
from qaura.browser.readonly import (
    AllowOnce,
    ReadOnlyLedger,
    install_offline_routes,
    install_readonly_routes,
)
from qaura.config import InvariantConfig
from qaura.init.candidates import validate
from qaura.init.limits import ReconLimits, recon_guardrails
from qaura.init.login import detect_login_wall
from qaura.init.recon import Snapshot, run_recon

FIXTURE_URL = "http://127.0.0.1:8099/"


def _fixture_is_up() -> bool:
    try:
        httpx.get(FIXTURE_URL, timeout=1.0)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _fixture_is_up(), reason="buggy_app fixture not running on :8099")


@pytest.fixture
def cfg():
    return recon_guardrails(FIXTURE_URL)


# --- selector generation ------------------------------------------------------------


async def test_scan_finds_the_cart_numbers():
    async with Driver(headless=True) as driver:
        async with driver.context(ContextSpec(persona="test")) as (_, page):
            await page.goto(FIXTURE_URL)
            elements = await scan_numeric_elements(page)

    by_selector = {e.selector: e for e in elements}
    for name in ("line-total", "cart-subtotal", "cart-discount", "cart-total"):
        assert f'[data-testid="{name}"]' in by_selector, f"missing {name}"

    assert by_selector['[data-testid="cart-subtotal"]'].number == pytest.approx(10.0)
    assert by_selector['[data-testid="cart-discount"]'].number == pytest.approx(0.0)
    assert by_selector['[data-testid="cart-total"]'].number == pytest.approx(10.0)


async def test_testid_is_preferred_over_id():
    """The fixture puts both data-testid and id on the same elements, which is what makes
    this a real preference test rather than a tautology."""
    async with Driver(headless=True) as driver:
        async with driver.context(ContextSpec(persona="test")) as (_, page):
            await page.goto(FIXTURE_URL)
            elements = await scan_numeric_elements(page)

    cart = [e for e in elements if "cart-total" in e.selector]
    assert cart, "cart-total not found"
    assert cart[0].selector == '[data-testid="cart-total"]'
    assert cart[0].selector_kind == "testid"


async def test_labels_come_from_preceding_text():
    async with Driver(headless=True) as driver:
        async with driver.context(ContextSpec(persona="test")) as (_, page):
            await page.goto(FIXTURE_URL)
            elements = await scan_numeric_elements(page)

    labels = {e.selector: e.label for e in elements}
    assert labels['[data-testid="cart-subtotal"]'] == "Subtotal"
    assert labels['[data-testid="cart-discount"]'] == "Discount"
    assert labels['[data-testid="cart-total"]'] == "Total"


async def test_container_is_the_cart():
    async with Driver(headless=True) as driver:
        async with driver.context(ContextSpec(persona="test")) as (_, page):
            await page.goto(FIXTURE_URL)
            elements = await scan_numeric_elements(page)

    cart = [e for e in elements if "cart-total" in e.selector][0]
    assert cart.container_selector == '[data-testid="cart"]'


async def test_form_inputs_are_excluded_from_the_inventory():
    """extract_values() reads text_content(), which is empty for an <input>. The fixture's
    <input type="number" id="qty" value="1"> is exactly that trap: an invariant
    referencing it would raise InvariantError on every evaluation and be permanently
    inconclusive rather than obviously broken."""
    async with Driver(headless=True) as driver:
        async with driver.context(ContextSpec(persona="test")) as (_, page):
            await page.goto(FIXTURE_URL)
            elements = await scan_numeric_elements(page)

    assert not any("qty" in e.selector for e in elements)
    assert not any(e.tag == "input" for e in elements)


# --- read-only guarantee ------------------------------------------------------------


async def test_post_is_blocked_at_the_wire(cfg):
    """The load-bearing safety test. It must never be skipped or weakened: everything
    else in recon assumes the target's own JavaScript cannot mutate state."""
    ledger = ReadOnlyLedger()
    async with Driver(headless=True) as driver:
        async with driver.context(ContextSpec(persona="test")) as (context, page):
            await install_readonly_routes(context, cfg, ledger)
            await page.goto(FIXTURE_URL)
            await page.evaluate("""
                () => fetch('/api/validate-email', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({email: 'x@y.com'}),
                }).catch(() => null)
            """)
            await page.wait_for_timeout(500)

    aborted = [(m, u) for m, u, _ in ledger.aborted]
    assert any(m == "POST" and "validate-email" in u for m, u in aborted), (
        f"the POST was not blocked; ledger={ledger.aborted}"
    )
    assert ledger.non_get_attempts >= 1


async def test_ordinary_get_crawl_blocks_nothing(cfg):
    ledger = ReadOnlyLedger()
    async with Driver(headless=True) as driver:
        async with driver.context(ContextSpec(persona="test")) as (context, page):
            await install_readonly_routes(context, cfg, ledger)
            await page.goto(FIXTURE_URL)
            await page.wait_for_timeout(300)

    assert ledger.non_get_attempts == 0
    assert ledger.allowed > 0


async def test_allow_once_permits_exactly_one_post(cfg):
    """The assisted-login carve-out, verified against real traffic rather than the
    classifier alone."""
    ledger = ReadOnlyLedger()
    allow = AllowOnce(origin="http://127.0.0.1:8099")
    async with Driver(headless=True) as driver:
        async with driver.context(ContextSpec(persona="test")) as (context, page):
            await install_readonly_routes(context, cfg, ledger, allow)
            await page.goto(FIXTURE_URL)

            allow.armed = True
            await page.evaluate(
                "() => fetch('/api/validate-email', {method:'POST', "
                "headers:{'Content-Type':'application/json'}, body:'{}'}).catch(() => null)"
            )
            await page.wait_for_timeout(400)
            allow.armed = False

            await page.evaluate(
                "() => fetch('/api/validate-email', {method:'POST', "
                "headers:{'Content-Type':'application/json'}, body:'{}'}).catch(() => null)"
            )
            await page.wait_for_timeout(400)

    assert len(ledger.allowed_exceptions) == 1
    assert allow.used
    assert any(m == "POST" for m, _, _ in ledger.aborted), "the second POST should be blocked"


# --- login wall ---------------------------------------------------------------------


async def test_login_page_is_detected(cfg):
    async with Driver(headless=True) as driver:
        async with driver.context(ContextSpec(persona="test")) as (_, page):
            response = await page.goto("http://127.0.0.1:8099/login")
            model = await build_page_model(page)
            signal = await detect_login_wall(page, model, response.status)

    assert signal.kind == "login_page"
    assert signal.confidence >= 0.6
    assert any("password" in e for e in signal.evidence)


async def test_401_page_is_detected_as_a_wall(cfg):
    async with Driver(headless=True) as driver:
        async with driver.context(ContextSpec(persona="test")) as (_, page):
            response = await page.goto("http://127.0.0.1:8099/private")
            model = await build_page_model(page)
            signal = await detect_login_wall(page, model, response.status)

    assert signal.kind == "login_page"
    assert any("401" in e for e in signal.evidence)


async def test_ordinary_page_is_not_a_wall(cfg):
    async with Driver(headless=True) as driver:
        async with driver.context(ContextSpec(persona="test")) as (_, page):
            response = await page.goto(FIXTURE_URL)
            model = await build_page_model(page)
            signal = await detect_login_wall(page, model, response.status)

    assert signal.kind == "none"


# --- assisted login -------------------------------------------------------------------


async def _try_login(cfg, auth_dir, role="testrole"):
    from qaura.init.login import attempt_login

    ledger = ReadOnlyLedger()
    allow = AllowOnce(origin="http://127.0.0.1:8099")
    async with Driver(headless=True) as driver:
        async with driver.context(ContextSpec(persona="test")) as (context, page):
            await install_readonly_routes(context, cfg, ledger, allow)
            await page.goto("http://127.0.0.1:8099/login")
            outcome = await attempt_login(page, context, allow, role=role, auth_dir=auth_dir)
            return outcome, ledger, allow, page.url


async def test_assisted_login_succeeds_and_saves_a_session(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("QAURA_LOGIN_USER", "demo")
    monkeypatch.setenv("QAURA_LOGIN_PASS", "demo-password")

    outcome, ledger, allow, final_url = await _try_login(cfg, tmp_path / "auth")

    assert outcome.ok, outcome.reason
    assert outcome.storage_state_path.exists()
    assert final_url.endswith("/private")
    assert len(ledger.allowed_exceptions) == 1
    assert allow.armed is False, "the POST exemption must be disarmed after the submit"


async def test_wrong_credentials_fail_and_save_nothing(cfg, tmp_path, monkeypatch):
    """Success is verified from the response status, not from the password field being
    gone. Judging by absence alone would treat any error page — a 401, a 500 — as a
    successful login and save a session recon would then wrongly trust."""
    monkeypatch.setenv("QAURA_LOGIN_USER", "demo")
    monkeypatch.setenv("QAURA_LOGIN_PASS", "wrong-password")

    outcome, ledger, _, _ = await _try_login(cfg, tmp_path / "auth")

    assert not outcome.ok
    assert "401" in outcome.reason
    assert not (tmp_path / "auth" / "testrole.json").exists()
    # Exactly one attempt. Retrying a wrong password is how an account gets locked.
    assert len(ledger.allowed_exceptions) == 1


async def test_missing_credentials_fail_before_touching_the_form(cfg, tmp_path, monkeypatch):
    monkeypatch.delenv("QAURA_LOGIN_USER", raising=False)
    monkeypatch.delenv("QAURA_LOGIN_PASS", raising=False)

    outcome, ledger, _, _ = await _try_login(cfg, tmp_path / "auth")

    assert not outcome.ok
    assert "QAURA_LOGIN_USER" in outcome.reason
    assert ledger.allowed_exceptions == []


async def test_credentials_never_reach_the_generated_config(cfg, tmp_path, monkeypatch):
    from qaura.init.emit import render_config
    from qaura.init.infer import build_proposal
    from qaura.init.limits import ReconLimits

    monkeypatch.setenv("QAURA_LOGIN_USER", "demo")
    monkeypatch.setenv("QAURA_LOGIN_PASS", "demo-password")

    limits = ReconLimits().with_overrides(max_pages=1, max_depth=0)
    result = await run_recon(
        "http://127.0.0.1:8099/login", cfg, limits, headless=True,
        login_form=True, auth_dir=tmp_path / "auth", role="testrole",
    )
    rendered = render_config(build_proposal(
        result, [], command="qaura init", version="test",
        authorization="local", cwd=tmp_path, llm_used=False,
    ))

    assert "demo-password" not in rendered
    assert "QAURA_LOGIN_PASS" not in rendered


# --- invariant validation -----------------------------------------------------------


@pytest.fixture
async def snapshot():
    async with Driver(headless=True) as driver:
        async with driver.context(ContextSpec(persona="test")) as (_, page):
            await page.goto(FIXTURE_URL)
            html = await page.content()
    return Snapshot(url=FIXTURE_URL, html=html, viewport=(1280, 800), load_index=0)


async def _validate(invariants, snapshots):
    async with Driver(headless=True) as driver:
        async with driver.context(ContextSpec(persona="validate")) as (context, page):
            await install_offline_routes(context)
            return await validate(page, invariants, snapshots)


async def test_true_invariant_is_accepted(snapshot):
    invariant = InvariantConfig(
        name="total_matches_subtotal_minus_discount",
        description="Total equals subtotal minus discount",
        container_selector='[data-testid="cart"]',
        values={
            "total": '[data-testid="cart-total"]',
            "subtotal": '[data-testid="cart-subtotal"]',
            "discount": '[data-testid="cart-discount"]',
        },
        expression="abs(total - (subtotal - discount)) <= 0.01",
    )
    verdicts = await _validate([invariant], [snapshot])
    assert verdicts[0].status == "accepted"
    assert verdicts[0].conclusive_count == 1


async def test_false_invariant_is_rejected_with_evidence(snapshot):
    invariant = InvariantConfig(
        name="deliberately_wrong", description="Cannot hold",
        container_selector='[data-testid="cart"]',
        values={"total": '[data-testid="cart-total"]',
                "subtotal": '[data-testid="cart-subtotal"]'},
        expression="total == subtotal * 3",
    )
    verdicts = await _validate([invariant], [snapshot])
    assert verdicts[0].status == "rejected_violated"
    assert verdicts[0].violating_state_url == FIXTURE_URL
    assert verdicts[0].sample_values


async def test_unmatched_selector_is_unverified_not_violated(snapshot):
    """Inconclusive is not the same as false. A rule that could not be evaluated must
    never be reported as a violation."""
    invariant = InvariantConfig(
        name="never_evaluable", description="Selector does not exist",
        values={"a": '[data-testid="does-not-exist"]', "b": '[data-testid="cart-total"]'},
        expression="a == b",
    )
    verdicts = await _validate([invariant], [snapshot])
    assert verdicts[0].status == "unverified"
    assert verdicts[0].conclusive_count == 0
    assert "matched nothing" in verdicts[0].reason


async def test_line_items_group_sums(snapshot):
    """The repeated-group path: a selector matching several elements becomes a list, and
    sum() over it is what a cart invariant needs."""
    invariant = InvariantConfig(
        name="subtotal_matches_line_items",
        description="Subtotal equals the sum of line totals",
        container_selector='[data-testid="cart"]',
        values={"subtotal": '[data-testid="cart-subtotal"]',
                "line_items": '[data-testid="line-total"]'},
        expression="abs(subtotal - sum(line_items)) <= 0.01",
    )
    verdicts = await _validate([invariant], [snapshot])
    assert verdicts[0].status == "accepted"


# --- recon ---------------------------------------------------------------------------


async def test_recon_respects_the_page_cap(cfg):
    limits = ReconLimits().with_overrides(max_pages=2, max_depth=1)
    result = await run_recon(FIXTURE_URL, cfg, limits, headless=True)

    assert len(result.states) <= 2
    assert result.states
    assert result.ledger.non_get_attempts == 0
    assert result.login.kind == "none"
    assert result.snapshots
    assert all(s.html for s in result.snapshots)


async def test_recon_captures_numerics_and_a_digest(cfg):
    from qaura.init.infer import recon_digest

    limits = ReconLimits().with_overrides(max_pages=1, max_depth=0)
    result = await run_recon(FIXTURE_URL, cfg, limits, headless=True)

    assert result.states[0].numerics
    assert len(recon_digest(result)) == 12
