"""Data and drift suite: schema validation, feature drift (PSI/KS), null-rate
shifts, and a leakage correlation scan — plan's four-suite breakdown, suite #2.
No trained model needed here — everything operates on two DataFrames (a reference,
e.g. training data, and a current, e.g. serving/eval data) or on one DataFrame plus
a label column for the leakage check.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

from qaura.reporting.models import Evidence, Finding, Severity

DEFAULT_PSI_WARN_THRESHOLD = 0.10
DEFAULT_PSI_FAIL_THRESHOLD = 0.25
DEFAULT_NULL_RATE_SHIFT_THRESHOLD = 0.05
DEFAULT_LEAKAGE_CORRELATION_THRESHOLD = 0.95
DEFAULT_KS_PVALUE_THRESHOLD = 0.01


def check_schema(reference: pd.DataFrame, current: pd.DataFrame, source: str = "") -> list[Finding]:
    findings: list[Finding] = []
    missing = set(reference.columns) - set(current.columns)
    if missing:
        findings.append(Finding(
            title=f"Schema: {len(missing)} column(s) missing from current data",
            detector="ml_schema", severity=Severity.HIGH, url=source,
            description=f"Present in reference but missing from current: {sorted(missing)}",
            evidence=Evidence(),
        ))

    common = set(reference.columns) & set(current.columns)
    dtype_mismatches = {
        col: (str(reference[col].dtype), str(current[col].dtype))
        for col in common if reference[col].dtype != current[col].dtype
    }
    if dtype_mismatches:
        findings.append(Finding(
            title=f"Schema: {len(dtype_mismatches)} column(s) changed dtype",
            detector="ml_schema", severity=Severity.MEDIUM, url=source,
            description=f"reference dtype -> current dtype: {dtype_mismatches}",
            evidence=Evidence(),
        ))
    return findings


def population_stability_index(expected: pd.Series, actual: pd.Series, buckets: int = 10) -> float:
    """Standard PSI: bucket `expected` into equal-frequency quantile bins, compare
    the proportion of `actual` falling in each bin. >0.25 is the commonly-cited
    "significant shift" threshold; 0.1-0.25 is "moderate"; below 0.1 is "no
    significant shift" — see DEFAULT_PSI_*_THRESHOLD."""
    expected = expected.dropna()
    actual = actual.dropna()
    quantiles = np.unique(np.quantile(expected, np.linspace(0, 1, buckets + 1)))
    if len(quantiles) < 3:
        return 0.0  # not enough distinct values to bucket meaningfully

    expected_counts, _ = np.histogram(expected, bins=quantiles)
    actual_counts, _ = np.histogram(actual, bins=quantiles)

    # Laplace smoothing (add 1) avoids divide-by-zero / log(0) for empty bins —
    # standard practice for PSI on small samples.
    expected_pct = (expected_counts + 1) / (expected_counts.sum() + len(expected_counts))
    actual_pct = (actual_counts + 1) / (actual_counts.sum() + len(actual_counts))

    return float(np.sum((actual_pct - expected_pct) * np.log(actual_pct / expected_pct)))


def check_drift(
    reference: pd.DataFrame, current: pd.DataFrame, columns: list[str], source: str = "",
    psi_warn_threshold: float = DEFAULT_PSI_WARN_THRESHOLD,
    psi_fail_threshold: float = DEFAULT_PSI_FAIL_THRESHOLD,
) -> list[Finding]:
    findings: list[Finding] = []
    for col in columns:
        if col not in reference.columns or col not in current.columns:
            continue
        if not pd.api.types.is_numeric_dtype(reference[col]):
            continue  # PSI as implemented here is for numeric/quantile-binnable columns
        psi = population_stability_index(reference[col], current[col])
        if psi >= psi_fail_threshold:
            severity = Severity.HIGH
        elif psi >= psi_warn_threshold:
            severity = Severity.MEDIUM
        else:
            continue

        ks_stat, ks_pvalue = stats.ks_2samp(reference[col].dropna(), current[col].dropna())
        findings.append(Finding(
            title=f"Feature drift: {col} PSI={psi:.3f}",
            detector="ml_drift", severity=severity, url=source,
            description=(
                f"Population Stability Index for {col!r} is {psi:.3f} "
                f"(warn >= {psi_warn_threshold}, fail >= {psi_fail_threshold}). "
                f"Kolmogorov-Smirnov test: statistic={ks_stat:.3f}, p={ks_pvalue:.4f}."
            ),
            evidence=Evidence(),
        ))
    return findings


def check_null_rate_shift(
    reference: pd.DataFrame, current: pd.DataFrame, columns: list[str], source: str = "",
    threshold: float = DEFAULT_NULL_RATE_SHIFT_THRESHOLD,
) -> list[Finding]:
    findings: list[Finding] = []
    for col in columns:
        if col not in reference.columns or col not in current.columns:
            continue
        ref_rate = reference[col].isna().mean()
        cur_rate = current[col].isna().mean()
        shift = abs(cur_rate - ref_rate)
        if shift > threshold:
            findings.append(Finding(
                title=f"Null-rate shift: {col} {ref_rate:.1%} -> {cur_rate:.1%}",
                detector="ml_data_quality", severity=Severity.MEDIUM, url=source,
                description=(
                    f"{col!r}'s null rate shifted from {ref_rate:.1%} (reference) to "
                    f"{cur_rate:.1%} (current), a {shift:.1%} change — above the "
                    f"{threshold:.1%} threshold."
                ),
                evidence=Evidence(),
            ))
    return findings


def check_leakage(
    data: pd.DataFrame, feature_cols: list[str], label_col: str, source: str = "",
    correlation_threshold: float = DEFAULT_LEAKAGE_CORRELATION_THRESHOLD,
) -> list[Finding]:
    """Flags a feature suspiciously (near-perfectly) correlated with the label —
    often a sign the feature was derived FROM the label (or a close proxy computed
    after the fact), which inflates offline metrics but can't hold up in production
    where that information isn't actually available at prediction time."""
    findings: list[Finding] = []
    if not pd.api.types.is_numeric_dtype(data[label_col]):
        return findings
    for col in feature_cols:
        if col == label_col or col not in data.columns or not pd.api.types.is_numeric_dtype(data[col]):
            continue
        corr = data[col].corr(data[label_col])
        if pd.notna(corr) and abs(corr) >= correlation_threshold:
            findings.append(Finding(
                title=f"Possible label leakage: {col} correlates {corr:.3f} with {label_col}",
                detector="ml_leakage", severity=Severity.HIGH, url=source,
                description=(
                    f"{col!r} has a {corr:.3f} correlation with the label {label_col!r} — "
                    f"suspiciously close to perfect. Verify this feature is genuinely "
                    f"available at prediction time and wasn't derived from the label."
                ),
                evidence=Evidence(),
            ))
    return findings
