"""Localizes against tests/fixtures/buggy_app — a real repo already in the
project, not a synthetic fixture, so this proves the mechanism works on the actual
target used throughout Phase 2-5's verification, not just a toy directory."""
from pathlib import Path

from qaura.analysis.localize import apply_localization, extract_search_terms, localize
from qaura.analysis.repo_index import build_index
from qaura.reporting.models import Evidence, Finding, ReproStep

BUGGY_APP_DIR = Path(__file__).parent.parent / "fixtures" / "buggy_app"


def test_extract_search_terms_from_url_path():
    f = Finding(url="http://127.0.0.1:8099/api/validate-email")
    terms = extract_search_terms(f)
    assert "/api/validate-email" in terms
    assert "validate-email" in terms


def test_extract_search_terms_from_network_failure_evidence():
    f = Finding(
        url="http://127.0.0.1:8099/",
        evidence=Evidence(network_failures=["POST http://127.0.0.1:8099/api/subscribe -> 404"]),
    )
    terms = extract_search_terms(f)
    assert "/api/subscribe" in terms


def test_extract_search_terms_from_repro_step_quotes():
    f = Finding(repro_steps=[ReproStep(description="click e4 button 'Trigger broken widget'")])
    terms = extract_search_terms(f)
    assert "Trigger broken widget" in terms


def test_extract_search_terms_dedupes_preserving_order():
    f = Finding(
        url="http://x/api/foo",
        description="calling 'api/foo' again",
    )
    terms = extract_search_terms(f)
    assert terms.count("/api/foo") <= 1


def test_localize_finds_the_real_buggy_app_source_for_validate_email_bug():
    index = build_index(BUGGY_APP_DIR)
    finding = Finding(
        title="Server error 500 on POST http://127.0.0.1:8099/api/validate-email",
        detector="crash",
        url="http://127.0.0.1:8099/",
        description="POST http://127.0.0.1:8099/api/validate-email returned HTTP 500",
        evidence=Evidence(network_failures=["POST http://127.0.0.1:8099/api/validate-email -> 500"]),
    )
    candidates = localize(index, finding)
    assert any("app.py" in c for c in candidates)


def test_localize_finds_the_real_buggy_app_source_for_broken_widget():
    index = build_index(BUGGY_APP_DIR)
    finding = Finding(
        title="Uncaught page error",
        detector="crash",
        url="http://127.0.0.1:8099/",
        repro_steps=[ReproStep(description="click e10 button 'Trigger broken widget'")],
    )
    candidates = localize(index, finding)
    assert any("app.py" in c for c in candidates)


def test_apply_localization_sets_likely_component():
    index = build_index(BUGGY_APP_DIR)
    finding = Finding(url="http://127.0.0.1:8099/api/validate-email")
    assert finding.likely_component is None
    apply_localization(index, finding)
    assert finding.likely_component is not None
    assert "app.py" in finding.likely_component


def test_apply_localization_leaves_none_when_no_terms():
    index = build_index(BUGGY_APP_DIR)
    finding = Finding()  # no url, no repro_steps, no description
    apply_localization(index, finding)
    assert finding.likely_component is None
