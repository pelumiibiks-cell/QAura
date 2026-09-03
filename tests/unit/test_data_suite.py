import numpy as np
import pandas as pd
import pytest

from qaura.mltest.suites.data import (
    check_drift,
    check_leakage,
    check_null_rate_shift,
    check_schema,
    population_stability_index,
)


@pytest.fixture
def reference_df():
    rng = np.random.default_rng(0)
    return pd.DataFrame({
        "age": rng.normal(35, 10, size=1000),
        "income": rng.normal(50000, 15000, size=1000),
        "category": rng.choice(["a", "b", "c"], size=1000),
    })


def test_check_schema_flags_missing_column(reference_df):
    current = reference_df.drop(columns=["income"])
    findings = check_schema(reference_df, current)
    assert any(f.detector == "ml_schema" and "missing" in f.title.lower() for f in findings)


def test_check_schema_flags_dtype_change(reference_df):
    current = reference_df.copy()
    current["age"] = current["age"].astype(str)
    findings = check_schema(reference_df, current)
    assert any("dtype" in f.title.lower() for f in findings)


def test_check_schema_no_findings_for_identical_schema(reference_df):
    assert check_schema(reference_df, reference_df.copy()) == []


def test_psi_near_zero_for_identical_distributions(reference_df):
    psi = population_stability_index(reference_df["age"], reference_df["age"])
    assert psi < 0.01


def test_psi_high_for_genuinely_shifted_distribution():
    rng = np.random.default_rng(1)
    expected = pd.Series(rng.normal(0, 1, size=1000))
    actual = pd.Series(rng.normal(3, 1, size=1000))  # shifted by 3 std devs
    psi = population_stability_index(expected, actual)
    assert psi > 0.25


def test_check_drift_flags_real_shift(reference_df):
    current = reference_df.copy()
    current["age"] = current["age"] + 25  # dramatic shift
    findings = check_drift(reference_df, current, ["age", "income"])
    assert any(f.detector == "ml_drift" and "age" in f.title for f in findings)
    assert not any("income" in f.title for f in findings)  # income unchanged, no finding


def test_check_drift_no_finding_for_unshifted_data(reference_df):
    rng = np.random.default_rng(2)
    current = reference_df.copy()
    current["age"] = current["age"] + rng.normal(0, 0.01, size=len(current))  # negligible noise
    findings = check_drift(reference_df, current, ["age"])
    assert findings == []


def test_check_drift_skips_non_numeric_columns(reference_df):
    current = reference_df.copy()
    findings = check_drift(reference_df, current, ["category"])
    assert findings == []  # non-numeric column silently skipped, not an error


def test_check_null_rate_shift_flags_new_nulls(reference_df):
    current = reference_df.copy()
    current.loc[: len(current) // 2, "income"] = np.nan  # ~50% now null, was 0%
    findings = check_null_rate_shift(reference_df, current, ["income"], threshold=0.05)
    assert len(findings) == 1
    assert findings[0].detector == "ml_data_quality"


def test_check_null_rate_shift_no_finding_below_threshold(reference_df):
    current = reference_df.copy()
    current.loc[0:5, "income"] = np.nan  # tiny null rate, well under 5%
    findings = check_null_rate_shift(reference_df, current, ["income"], threshold=0.05)
    assert findings == []


def test_check_leakage_flags_near_perfect_correlation():
    rng = np.random.default_rng(3)
    label = rng.integers(0, 2, size=500).astype(float)
    data = pd.DataFrame({
        "leaky_feature": label + rng.normal(0, 0.001, size=500),  # essentially == label
        "real_feature": rng.normal(0, 1, size=500),
        "label": label,
    })
    findings = check_leakage(data, ["leaky_feature", "real_feature"], "label")
    assert len(findings) == 1
    assert "leaky_feature" in findings[0].title
    assert findings[0].detector == "ml_leakage"


def test_check_leakage_no_finding_for_uncorrelated_features():
    rng = np.random.default_rng(4)
    data = pd.DataFrame({
        "feature_a": rng.normal(0, 1, size=500),
        "feature_b": rng.normal(0, 1, size=500),
        "label": rng.integers(0, 2, size=500),
    })
    findings = check_leakage(data, ["feature_a", "feature_b"], "label")
    assert findings == []
