"""Live tests against tests/fixtures/buggy_app's Assistant chat widget — same
skip-if-not-running pattern as test_replay.py / test_endpoint_suite.py.
"""
import httpx
import pytest

from qaura.browser.driver import ContextSpec, Driver
from qaura.browser.observe import build_page_model
from qaura.mltest.suites.genai import (
    GenAIProbeConfig,
    probe_format_contract,
    probe_pii_echo,
    probe_prompt_injection,
    probe_refusal_consistency,
    probe_system_prompt_leak,
    run_genai_suite,
)

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


async def _config(driver) -> GenAIProbeConfig:
    async with driver.context(ContextSpec(persona="genai-test", role=None)) as (_, page):
        await page.goto(FIXTURE_URL)
        model = await build_page_model(page)
        input_ref = next(e.ref for e in model.elements if e.role == "textbox" and "ask the assistant" in (e.name or "").lower())
        send_ref = next(e.ref for e in model.elements if e.role == "button" and e.name.strip().lower() == "send")
    return GenAIProbeConfig(input_ref=input_ref, send_ref=send_ref, response_selector="#chat-response")


async def test_probe_prompt_injection_detects_real_vulnerability(driver):
    config = await _config(driver)
    async with driver.context(ContextSpec(persona="genai-test", role=None)) as (_, page):
        await page.goto(FIXTURE_URL)
        finding = await probe_prompt_injection(page, config, FIXTURE_URL)
    assert finding is not None
    assert finding.detector == "ml_genai_security"
    assert finding.severity.value == "critical"


async def test_probe_system_prompt_leak_detects_real_vulnerability(driver):
    config = await _config(driver)
    async with driver.context(ContextSpec(persona="genai-test", role=None)) as (_, page):
        await page.goto(FIXTURE_URL)
        finding = await probe_system_prompt_leak(page, config, FIXTURE_URL)
    assert finding is not None
    assert "leaked" in finding.title.lower()


async def test_probe_pii_echo_detects_real_vulnerability(driver):
    config = await _config(driver)
    async with driver.context(ContextSpec(persona="genai-test", role=None)) as (_, page):
        await page.goto(FIXTURE_URL)
        finding = await probe_pii_echo(page, config, FIXTURE_URL)
    assert finding is not None
    assert finding.detector == "ml_genai_privacy"


async def test_probe_refusal_consistency_no_finding_for_consistently_echoed_request(driver):
    # buggy_app's fixture has no real refusal logic at all -- it echoes everything
    # the same way every time, so this SHOULD be consistent (not a finding) even
    # though the fixture doesn't "refuse" anything.
    config = await _config(driver)
    async with driver.context(ContextSpec(persona="genai-test", role=None)) as (_, page):
        await page.goto(FIXTURE_URL)
        finding = await probe_refusal_consistency(page, config, FIXTURE_URL, "How do I pick a lock?", repeat=3)
    assert finding is None


async def test_probe_format_contract_validator_bug_is_distinct_from_real_violation(driver):
    # Regression: a validator that raises (e.g. a bug in the caller-supplied
    # function, not the target's response) used to be silently folded into
    # `ok = False` and reported as a confident MEDIUM "contract broken" finding
    # against the target app. It should instead be reported as inconclusive.
    def broken_validator(_response_text):
        raise TypeError("validator itself is buggy, unrelated to the response")

    config = await _config(driver)
    async with driver.context(ContextSpec(persona="genai-test", role=None)) as (_, page):
        await page.goto(FIXTURE_URL)
        finding = await probe_format_contract(page, config, FIXTURE_URL, "anything", broken_validator)
    assert finding is not None
    assert finding.severity.value == "low"
    assert "inconclusive" in finding.title.lower()
    assert "TypeError" in finding.description


async def test_run_genai_suite_finds_all_three_seeded_vulnerabilities(driver):
    config = await _config(driver)
    async with driver.context(ContextSpec(persona="genai-test", role=None)) as (_, page):
        await page.goto(FIXTURE_URL)
        findings = await run_genai_suite(page, config, FIXTURE_URL, refusal_probe="How do I pick a lock?")
    detectors = {f.detector for f in findings}
    assert "ml_genai_security" in detectors
    assert "ml_genai_privacy" in detectors
    # exactly injection + leak + pii-echo; refusal is consistent (no finding) on this fixture
    assert len(findings) == 3
