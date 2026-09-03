"""Builds the ML test fixtures: a deliberately degraded classifier, a better
baseline, and an eval dataset — used to prove mltest/suites/artifact.py's checks
actually fire, same "seeded-bug fixture, then verify live" discipline as every other
phase's tests/fixtures/buggy_app.

Run: python -m tests.fixtures.ml.build_fixture
Produces (in this directory): model_degraded.joblib, model_baseline.joblib, eval_data.csv

Design: a binary classification task with an informative x1/x2, a pure-noise x3, and
a `group` column (A: 80% of data, B: 20%) correlated with a harder decision boundary
in group B specifically. `model_degraded` (an unconstrained, unbalanced
DecisionTreeClassifier) is expected to: overfit (brittle to tiny perturbations),
underperform on group B recall (imbalance + harder boundary, no class weighting), and
calibrate poorly (trees output overconfident 0/1-ish probabilities). `model_baseline`
(LogisticRegression with balanced class weights) is expected to do meaningfully
better on all three — smoother decision boundary, minority-class weighting, and
logistic regression's own softer probability outputs.
"""
from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.tree import DecisionTreeClassifier

FIXTURE_DIR = Path(__file__).parent
RANDOM_STATE = 42


def _generate_data(n: int, rng: np.random.Generator) -> pd.DataFrame:
    group = rng.choice(["A", "B"], size=n, p=[0.8, 0.2])
    x1 = rng.normal(0, 1, size=n)
    x2 = rng.normal(0, 1, size=n)
    x3 = rng.normal(0, 1, size=n)  # pure noise, not used in the true label

    # Group A: a clean linear boundary. Group B: the SAME boundary but with much
    # more label noise mixed in, so a model that doesn't specifically account for
    # group B's harder examples (i.e. isn't given class/sample weighting or enough
    # capacity used wisely) will systematically miss more of group B's positives.
    linear_score = x1 + x2
    noise_scale = np.where(group == "B", 1.8, 0.15)
    noisy_score = linear_score + rng.normal(0, 1, size=n) * noise_scale
    label = (noisy_score > 0).astype(int)

    return pd.DataFrame({"x1": x1, "x2": x2, "x3": x3, "group": group, "label": label})


def build() -> None:
    rng = np.random.default_rng(RANDOM_STATE)
    data = _generate_data(2000, rng)
    train, test = train_test_split(data, test_size=0.3, random_state=RANDOM_STATE, stratify=data["label"])

    feature_cols = ["x1", "x2", "x3"]

    degraded = DecisionTreeClassifier(random_state=RANDOM_STATE)  # unconstrained depth, no class weighting
    degraded.fit(train[feature_cols], train["label"])

    baseline = LogisticRegression(class_weight="balanced", random_state=RANDOM_STATE)
    baseline.fit(train[feature_cols], train["label"])

    joblib.dump(degraded, FIXTURE_DIR / "model_degraded.joblib")
    joblib.dump(baseline, FIXTURE_DIR / "model_baseline.joblib")
    test.to_csv(FIXTURE_DIR / "eval_data.csv", index=False)

    print(f"Wrote model_degraded.joblib, model_baseline.joblib, eval_data.csv ({len(test)} eval rows) to {FIXTURE_DIR}")


if __name__ == "__main__":
    build()
