"""Tests against the REAL tests/fixtures/ml models (built by
tests/fixtures/ml/build_fixture.py) — same discipline as every other phase's live
fixture verification, not synthetic mocks."""
from pathlib import Path

import pandas as pd
import pytest

from qaura.mltest.registry import load_model
from qaura.mltest.suites.artifact import (
    check_calibration,
    check_regression,
    check_robustness,
    check_slices,
    run_artifact_suite,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "ml"
FEATURE_COLS = ["x1", "x2", "x3"]


@pytest.fixture(scope="module")
def degraded():
    return load_model(FIXTURE_DIR / "model_degraded.joblib")


@pytest.fixture(scope="module")
def baseline():
    return load_model(FIXTURE_DIR / "model_baseline.joblib")


@pytest.fixture(scope="module")
def eval_data():
    path = FIXTURE_DIR / "eval_data.csv"
    if not path.exists():
        pytest.skip("tests/fixtures/ml eval data not built — run build_fixture.py")
    return pd.read_csv(path)


def test_check_slices_flags_real_fairness_gap(degraded, eval_data):
    X = eval_data[FEATURE_COLS + ["group"]]
    y = eval_data["label"]
    per_slice, findings = check_slices(degraded, X, y, "group", degraded.path, gap_threshold=0.15)
    assert "A" in per_slice and "B" in per_slice
    assert per_slice["A"] > per_slice["B"]  # matches the fixture's design: A is the easier group
    assert len(findings) == 1
    assert findings[0].detector == "ml_fairness"


def test_check_slices_no_finding_with_very_high_threshold(degraded, eval_data):
    X = eval_data[FEATURE_COLS + ["group"]]
    y = eval_data["label"]
    _, findings = check_slices(degraded, X, y, "group", degraded.path, gap_threshold=0.99)
    assert findings == []


def test_check_calibration_flags_degraded_model(degraded, eval_data):
    X = eval_data[FEATURE_COLS]
    y = eval_data["label"]
    brier, finding = check_calibration(degraded, X, y, degraded.path, brier_threshold=0.10)
    assert brier is not None
    assert finding is not None
    assert finding.detector == "ml_calibration"


def test_check_calibration_no_finding_for_baseline(baseline, eval_data):
    X = eval_data[FEATURE_COLS]
    y = eval_data["label"]
    brier, finding = check_calibration(baseline, X, y, baseline.path, brier_threshold=0.10)
    assert brier is not None
    assert finding is None  # baseline's real Brier score (~0.077) is under the 0.10 threshold


def test_check_robustness_flags_degraded_model(degraded, eval_data):
    X = eval_data[FEATURE_COLS]
    flip_rate, finding = check_robustness(degraded, X, degraded.path, FEATURE_COLS, flip_rate_threshold=0.05)
    assert flip_rate > 0.05
    assert finding is not None
    assert finding.detector == "ml_robustness"


def test_check_robustness_no_finding_for_baseline(baseline, eval_data):
    X = eval_data[FEATURE_COLS]
    flip_rate, finding = check_robustness(baseline, X, baseline.path, FEATURE_COLS, flip_rate_threshold=0.05)
    assert flip_rate < 0.05  # baseline's real flip rate (~1.2%) is well under threshold
    assert finding is None


def test_check_regression_detects_degraded_vs_baseline(degraded, baseline, eval_data):
    X = eval_data[FEATURE_COLS]
    y = eval_data["label"]
    finding = check_regression(degraded, baseline, X, y, degraded.path, accuracy_threshold=0.02)
    assert finding is not None
    assert finding.detector == "ml_regression"


def test_check_regression_no_finding_when_comparing_model_to_itself(baseline, eval_data):
    X = eval_data[FEATURE_COLS]
    y = eval_data["label"]
    finding = check_regression(baseline, baseline, X, y, baseline.path)
    assert finding is None


def test_run_artifact_suite_end_to_end_on_degraded_model(degraded, baseline, eval_data):
    result = run_artifact_suite(
        degraded, eval_data, FEATURE_COLS, "label", slice_col="group", baseline=baseline,
    )
    detectors_fired = {f.detector for f in result.findings}
    assert "ml_fairness" in detectors_fired
    assert "ml_calibration" in detectors_fired
    assert "ml_robustness" in detectors_fired
    assert "ml_regression" in detectors_fired
    assert result.metrics["accuracy"] > 0.5
    assert "per_slice_recall" in result.metrics


def test_run_artifact_suite_baseline_has_fewer_findings_than_degraded(degraded, baseline, eval_data):
    degraded_result = run_artifact_suite(degraded, eval_data, FEATURE_COLS, "label", slice_col="group", baseline=baseline)
    baseline_result = run_artifact_suite(baseline, eval_data, FEATURE_COLS, "label", slice_col="group", baseline=baseline)
    # baseline vs itself: no calibration/robustness/regression findings, only the
    # (real, expected) fairness gap that both models share
    baseline_detectors = {f.detector for f in baseline_result.findings}
    assert "ml_calibration" not in baseline_detectors
    assert "ml_robustness" not in baseline_detectors
    assert "ml_regression" not in baseline_detectors
    assert len(baseline_result.findings) < len(degraded_result.findings)
