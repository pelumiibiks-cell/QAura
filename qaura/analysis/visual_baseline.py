"""Visual baseline comparison across runs — plan's Phase 7 "compare a new run's
screenshots against a prior run's, flag unexpected changes." Lowest-priority item
per the plan's own ordering, scoped accordingly: compares screenshots for the SAME
recurring finding across two runs (matched via analysis/dedupe.py's fingerprint,
reused rather than reinvented — "is this the same bug" and "is this the same visual
context to diff" turn out to be the same matching problem), not a full state-by-state
sweep of every page regardless of whether a finding exists there. A complete sweep
would need unconditional per-state screenshot capture (currently screenshots are only
taken when a detection batch actually produces findings, deliberately, to avoid
redundant I/O — see core/heuristic.py's `_attach_screenshot`) — a real but larger
change than this lowest-priority item warrants without evidence it's needed.

What this DOES give you, and it's genuinely useful: "this known bug's page rendering
changed between two runs" — e.g. a low-contrast button finding that persists across
versions but whose surrounding layout shifted, or a finding whose screenshot looks
completely different from the last run for no findings-model reason (worth a human
glance even though the underlying detector still flags it the same way).
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from qaura.analysis.dedupe import fingerprint as dedupe_fingerprint
from qaura.reporting.models import Evidence, Finding, RunReport, Severity

DEFAULT_CHANGED_FRACTION_THRESHOLD = 0.05  # 5% of pixels differing beyond tolerance
PIXEL_DIFF_TOLERANCE = 20  # per-channel 0-255 difference below this counts as noise (anti-aliasing etc.)


class BaselineCompareError(RuntimeError):
    pass


def compare_screenshots(baseline_path: str, current_path: str) -> tuple[float, bool]:
    """Returns (changed_fraction, dimensions_match). changed_fraction is 1.0 (and
    dimensions_match False) if the two images aren't even the same size — a
    dimension change is itself a meaningful visual difference, not something to
    silently skip."""
    try:
        baseline = Image.open(baseline_path).convert("RGB")
        current = Image.open(current_path).convert("RGB")
    except (FileNotFoundError, OSError) as e:
        raise BaselineCompareError(f"could not open screenshot: {e}") from e

    if baseline.size != current.size:
        return 1.0, False

    baseline_arr = np.asarray(baseline, dtype=np.int16)
    current_arr = np.asarray(current, dtype=np.int16)
    diff = np.abs(baseline_arr - current_arr)
    changed_pixels = np.any(diff > PIXEL_DIFF_TOLERANCE, axis=-1)
    return float(changed_pixels.mean()), True


def compare_runs(
    baseline_report: RunReport, current_report: RunReport,
    threshold: float = DEFAULT_CHANGED_FRACTION_THRESHOLD,
) -> list[Finding]:
    """For every finding in `current_report` that recurs from `baseline_report`
    (same dedupe fingerprint) and both have a screenshot, flags a visual-regression
    Finding if the rendering changed by more than `threshold`. Findings with no
    screenshot, or with no match in the baseline, are silently skipped — not every
    finding needs to be new to matter, but there's nothing to DIFF without both
    a screenshot and a matching prior occurrence."""
    baseline_by_fp = {
        dedupe_fingerprint(f): f for f in baseline_report.findings if f.evidence.screenshot_path
    }

    results: list[Finding] = []
    for f in current_report.findings:
        if not f.evidence.screenshot_path:
            continue
        prior = baseline_by_fp.get(dedupe_fingerprint(f))
        if prior is None:
            continue
        try:
            changed_fraction, dimensions_match = compare_screenshots(
                prior.evidence.screenshot_path, f.evidence.screenshot_path,
            )
        except BaselineCompareError:
            continue

        if not dimensions_match or changed_fraction > threshold:
            reason = "screenshot dimensions changed" if not dimensions_match else f"{changed_fraction:.1%} of pixels changed"
            results.append(Finding(
                title=f"Visual regression on recurring finding: {reason}",
                detector="visual_baseline", severity=Severity.LOW, url=f.url,
                description=(
                    f"The finding {f.title!r} recurred from the baseline run, but its "
                    f"screenshot looks meaningfully different this time ({reason}, "
                    f"threshold {threshold:.1%}). Worth a human glance — either the "
                    f"surrounding page changed, or this is a different manifestation of "
                    f"a similarly-named issue."
                ),
                evidence=Evidence(screenshot_path=f.evidence.screenshot_path),
            ))
    return results
