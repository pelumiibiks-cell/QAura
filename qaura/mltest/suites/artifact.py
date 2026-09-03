"""Artifact test suite: point at a trained model plus eval data, get metrics,
per-slice/subgroup performance, perturbation robustness, calibration, fairness, and
regression against a baseline — plan's four-suite ML testing breakdown, suite #1.

Findings reuse the same `Finding` model as the web-crawl detectors (so
reporting/html.py's shell works unmodified) — `url` is repurposed as the model path
since there's no URL, and `repro_steps`/`evidence` are left empty since neither
concept applies to a static evaluation.

Fairness is checked on an ABSOLUTE threshold (any subgroup's recall more than
`fairness_gap_threshold` below the best-performing subgroup), not a baseline-vs-model
comparison — real-world fairness gaps exist independent of whether a "better" model
also has them (confirmed live against tests/fixtures/ml: both the degraded and
baseline fixture models show a ~25-29 point group recall gap; an absolute threshold
correctly flags both, which is the right behavior — a fairness bug in an "improved"
model is still a fairness bug). Regression-vs-baseline is checked separately, on
accuracy/calibration/robustness, where a baseline comparison genuinely makes sense.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from qaura.mltest.registry import LoadedModel
from qaura.reporting.models import Evidence, Finding, Severity

DEFAULT_FAIRNESS_GAP_THRESHOLD = 0.15   # 15 percentage points of recall
DEFAULT_CALIBRATION_BRIER_THRESHOLD = 0.10
DEFAULT_ROBUSTNESS_FLIP_RATE_THRESHOLD = 0.05
DEFAULT_PERTURBATION_STD = 0.05
DEFAULT_REGRESSION_ACCURACY_THRESHOLD = 0.02   # 2 points worse than baseline = regression


@dataclass
class ArtifactMetrics:
    accuracy: float
    per_slice_recall: dict[str, float] = field(default_factory=dict)
    brier_score: float | None = None
    flip_rate: float | None = None


def _predict_proba_positive(model: LoadedModel, X: pd.DataFrame) -> np.ndarray | None:
    """Best-effort: not every model exposes predict_proba (e.g. a bare callable
    wrapped by registry.py for a non-sklearn format) — calibration/some robustness
    checks are skipped gracefully rather than erroring when it's unavailable."""
    raw = model.raw
    if hasattr(raw, "predict_proba"):
        proba = raw.predict_proba(X)
        return proba[:, 1] if proba.ndim == 2 and proba.shape[1] == 2 else None
    return None


def _brier_score(y_true: np.ndarray, y_proba: np.ndarray) -> float:
    return float(np.mean((y_proba - y_true) ** 2))


def _recall(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    positives = y_true == 1
    if positives.sum() == 0:
        return float("nan")
    return float((y_pred[positives] == 1).mean())


def _accuracy(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float((y_true == y_pred).mean())


def check_slices(
    model: LoadedModel, X: pd.DataFrame, y: pd.Series, slice_col: str,
    model_path: str, feature_cols: list[str] | None = None,
    gap_threshold: float = DEFAULT_FAIRNESS_GAP_THRESHOLD,
) -> tuple[dict[str, float], list[Finding]]:
    """`feature_cols` defaults to every column except `slice_col` — pass it
    explicitly when `X` also carries other non-feature columns (an id column, other
    metadata) that shouldn't reach `model.predict()`. Found live: predicting on the
    full X (including the string-valued slice_col itself) crashed inside sklearn —
    the model was only ever trained on its actual feature columns."""
    feature_cols = feature_cols or [c for c in X.columns if c != slice_col]
    y_pred = np.asarray(model.predict(X[feature_cols]))
    y_true = y.to_numpy()

    per_slice: dict[str, float] = {}
    for slice_value in X[slice_col].unique():
        mask = (X[slice_col] == slice_value).to_numpy()
        per_slice[str(slice_value)] = _recall(y_true[mask], y_pred[mask])

    findings: list[Finding] = []
    valid = {k: v for k, v in per_slice.items() if not np.isnan(v)}
    if len(valid) >= 2:
        best = max(valid.values())
        for slice_value, recall in valid.items():
            gap = best - recall
            if gap > gap_threshold:
                findings.append(Finding(
                    title=f"Fairness gap: {slice_col}={slice_value} recall {gap:.1%} below best subgroup",
                    detector="ml_fairness", severity=Severity.HIGH, url=model_path,
                    description=(
                        f"Subgroup {slice_col}={slice_value} has recall {recall:.1%}, "
                        f"{gap:.1%} below the best-performing subgroup ({best:.1%}). "
                        f"Per-subgroup recall: {valid}"
                    ),
                    evidence=Evidence(),
                ))
    return per_slice, findings


def check_calibration(
    model: LoadedModel, X: pd.DataFrame, y: pd.Series, model_path: str,
    brier_threshold: float = DEFAULT_CALIBRATION_BRIER_THRESHOLD,
) -> tuple[float | None, Finding | None]:
    proba = _predict_proba_positive(model, X)
    if proba is None:
        return None, None
    brier = _brier_score(y.to_numpy(), proba)
    if brier > brier_threshold:
        return brier, Finding(
            title=f"Poor calibration: Brier score {brier:.3f} exceeds {brier_threshold}",
            detector="ml_calibration", severity=Severity.MEDIUM, url=model_path,
            description=(
                f"Brier score {brier:.3f} (lower is better; 0 is perfect, 0.25 is a coin flip's "
                f"worth of confident-but-wrong predictions). Predicted probabilities don't "
                f"reliably reflect actual outcome likelihood — downstream code that trusts "
                f"the model's confidence (e.g. thresholding, ranking by score) will misbehave."
            ),
            evidence=Evidence(),
        )
    return brier, None


def check_robustness(
    model: LoadedModel, X: pd.DataFrame, model_path: str,
    numeric_cols: list[str], perturbation_std: float = DEFAULT_PERTURBATION_STD,
    flip_rate_threshold: float = DEFAULT_ROBUSTNESS_FLIP_RATE_THRESHOLD,
    seed: int = 0,
) -> tuple[float, Finding | None]:
    y_pred = np.asarray(model.predict(X))
    rng = np.random.default_rng(seed)
    X_perturbed = X.copy()
    X_perturbed[numeric_cols] = X[numeric_cols] + rng.normal(0, perturbation_std, size=X[numeric_cols].shape)
    y_pred_perturbed = np.asarray(model.predict(X_perturbed))

    flip_rate = float((y_pred != y_pred_perturbed).mean())
    if flip_rate > flip_rate_threshold:
        return flip_rate, Finding(
            title=f"Brittle to small input perturbation: {flip_rate:.1%} of predictions flip",
            detector="ml_robustness", severity=Severity.MEDIUM, url=model_path,
            description=(
                f"Adding tiny Gaussian noise (std={perturbation_std}) to {numeric_cols} flips "
                f"{flip_rate:.1%} of predictions — above the {flip_rate_threshold:.1%} threshold. "
                f"A model this sensitive to measurement noise or minor input variation is likely "
                f"overfit rather than capturing a robust decision boundary."
            ),
            evidence=Evidence(),
        )
    return flip_rate, None


def check_regression(
    model: LoadedModel, baseline: LoadedModel, X: pd.DataFrame, y: pd.Series, model_path: str,
    accuracy_threshold: float = DEFAULT_REGRESSION_ACCURACY_THRESHOLD,
) -> Finding | None:
    y_true = y.to_numpy()
    model_acc = _accuracy(y_true, np.asarray(model.predict(X)))
    baseline_acc = _accuracy(y_true, np.asarray(baseline.predict(X)))
    gap = baseline_acc - model_acc
    if gap > accuracy_threshold:
        return Finding(
            title=f"Accuracy regression vs baseline: {model_acc:.1%} vs {baseline_acc:.1%}",
            detector="ml_regression", severity=Severity.HIGH, url=model_path,
            description=(
                f"This model scores {model_acc:.1%} accuracy on the eval set; the baseline "
                f"({baseline.path}) scores {baseline_acc:.1%} — a {gap:.1%} regression, above "
                f"the {accuracy_threshold:.1%} threshold."
            ),
            evidence=Evidence(),
        )
    return None


@dataclass
class ArtifactSuiteResult:
    findings: list[Finding]
    metrics: dict


def run_artifact_suite(
    model: LoadedModel,
    data: pd.DataFrame,
    feature_cols: list[str],
    label_col: str,
    slice_col: str | None = None,
    baseline: LoadedModel | None = None,
    **thresholds,
) -> ArtifactSuiteResult:
    X = data[feature_cols]
    y = data[label_col]
    model_path = model.path

    findings: list[Finding] = []
    y_pred = np.asarray(model.predict(X))
    accuracy = _accuracy(y.to_numpy(), y_pred)
    metrics: dict = {"accuracy": accuracy}

    if slice_col:
        X_with_slice = X.assign(**{slice_col: data[slice_col]})
        per_slice, slice_findings = check_slices(
            model, X_with_slice, y, slice_col, model_path, feature_cols=feature_cols,
            gap_threshold=thresholds.get("fairness_gap_threshold", DEFAULT_FAIRNESS_GAP_THRESHOLD),
        )
        metrics["per_slice_recall"] = per_slice
        findings.extend(slice_findings)

    brier, calib_finding = check_calibration(
        model, X, y, model_path,
        brier_threshold=thresholds.get("calibration_brier_threshold", DEFAULT_CALIBRATION_BRIER_THRESHOLD),
    )
    metrics["brier_score"] = brier
    if calib_finding:
        findings.append(calib_finding)

    numeric_cols = [c for c in feature_cols if pd.api.types.is_numeric_dtype(X[c])]
    if numeric_cols:
        flip_rate, robustness_finding = check_robustness(
            model, X, model_path, numeric_cols,
            perturbation_std=thresholds.get("perturbation_std", DEFAULT_PERTURBATION_STD),
            flip_rate_threshold=thresholds.get("robustness_flip_rate_threshold", DEFAULT_ROBUSTNESS_FLIP_RATE_THRESHOLD),
        )
        metrics["flip_rate"] = flip_rate
        if robustness_finding:
            findings.append(robustness_finding)

    if baseline is not None:
        regression_finding = check_regression(
            model, baseline, X, y, model_path,
            accuracy_threshold=thresholds.get("regression_accuracy_threshold", DEFAULT_REGRESSION_ACCURACY_THRESHOLD),
        )
        if regression_finding:
            findings.append(regression_finding)

    return ArtifactSuiteResult(findings=findings, metrics=metrics)
