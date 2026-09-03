"""Live tests against tests/fixtures/buggy_app — replay.py fundamentally needs a real
browser re-executing real actions, so these are integration-shaped rather than pure
unit tests. Requires the fixture server running on :8099 (same pattern used
throughout Phase 2-4's manual verification); skipped automatically if it's not up, so
the full suite still passes in an environment where nobody started it.
"""
import httpx
import pytest

from qaura.analysis.replay import replay_finding
from qaura.browser.driver import ContextSpec, Driver
from qaura.browser.observe import build_page_model
from qaura.config import InvariantConfig, QAuraConfig
from qaura.reporting.models import Finding, ReproStep

FIXTURE_URL = "http://127.0.0.1:8099/"


def _fixture_is_up() -> bool:
    try:
        httpx.get(FIXTURE_URL, timeout=1.0)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _fixture_is_up(), reason="buggy_app fixture not running on :8099")


@pytest.fixture
async def driver():
    async with Driver(headless=True) as d:
        yield d


async def test_replay_reproduces_broken_widget_crash(driver):
    # Discover the real ref live -- a hardcoded "e4" broke once already when the
    # fixture gained new sections (Cart, Settings, Search) ahead of this button.
    async with driver.context(ContextSpec(persona="ref-discovery", role=None)) as (_, page):
        await page.goto(FIXTURE_URL)
        model = await build_page_model(page)
        widget_ref = next(e.ref for e in model.elements if "broken widget" in e.name.lower())

    finding = Finding(
        title="Uncaught page error: Cannot read properties of undefined (reading 'activate')",
        detector="crash",
        url=FIXTURE_URL,
        repro_steps=[ReproStep(
            description=f"click {widget_ref} button 'Trigger broken widget'",
            action_kind="click", ref=widget_ref, url_before=FIXTURE_URL,
        )],
    )
    result = await replay_finding(driver, QAuraConfig(), finding, attempts=3)
    assert result.attempts == 3
    assert result.successes == 3  # this bug is fully deterministic
    assert finding.reproducibility == "3/3"
    assert finding.confirmed is True


async def test_replay_reports_zero_for_nonexistent_bug(driver):
    finding = Finding(
        title="A bug that does not actually exist",
        detector="crash",
        url=FIXTURE_URL,
        repro_steps=[ReproStep(
            description="click e1 button 'Check email'",  # exercises a real, harmless flow
            action_kind="click", ref="e2", url_before=FIXTURE_URL,
        )],
    )
    result = await replay_finding(driver, QAuraConfig(), finding, attempts=2)
    assert result.successes == 0
    assert finding.confirmed is False


async def test_replay_reproduces_invariant_violation(driver):
    cfg = QAuraConfig(invariants=[
        InvariantConfig(
            name="total_reflects_discount", description="d",
            container_selector="[data-testid=cart]",
            values={
                "total": "[data-testid=cart-total]",
                "subtotal": "[data-testid=cart-subtotal]",
                "discount": "[data-testid=cart-discount]",
            },
            expression="total == subtotal - discount",
        )
    ])
    # Discover the real refs live rather than guessing them -- fragile hardcoded refs
    # would silently break if the fixture's markup ever gets reordered.
    async with driver.context(ContextSpec(persona="ref-discovery", role=None)) as (_, page):
        await page.goto(FIXTURE_URL)
        model = await build_page_model(page)
        qty_ref = next(e.ref for e in model.elements if e.role == "spinbutton")
        coupon_ref = next(e.ref for e in model.elements if e.role == "textbox" and "coupon" in e.name.lower())
        apply_ref = next(e.ref for e in model.elements if e.role == "button" and "coupon" in e.name.lower())

    finding = Finding(
        title="Invariant violated: total_reflects_discount",
        detector="invariant",
        url=FIXTURE_URL,
        # Includes the Tab keypress after the qty fill -- confirmed live (debug
        # script) that .fill() alone changes the input's value but does NOT dispatch
        # the browser 'change' event the fixture's onchange="updateQuantity()"
        # listens for; only a real blur (Tab) triggers it. This matches what
        # heuristic.py actually records for every text/spinbutton fill (fill + Tab as
        # two separate ReproSteps) — a repro missing the Tab step is incomplete,
        # exactly the shape of bug this test would have hidden if left unfixed.
        repro_steps=[
            ReproStep(description="fill coupon", action_kind="fill", ref=coupon_ref, value="SAVE10", url_before=FIXTURE_URL),
            ReproStep(description="apply coupon", action_kind="click", ref=apply_ref, url_before=FIXTURE_URL),
            ReproStep(description="change qty", action_kind="fill", ref=qty_ref, value="3", url_before=FIXTURE_URL),
            ReproStep(description="blur qty field", action_kind="key", ref=qty_ref, value="Tab", url_before=FIXTURE_URL),
        ],
    )
    result = await replay_finding(driver, cfg, finding, attempts=2)
    assert result.attempts == 2
    assert result.successes == 2  # this bug is fully deterministic given this exact sequence
    assert finding.confirmed is True
