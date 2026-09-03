from pathlib import Path

from PIL import Image

from qaura.analysis.visual_baseline import (
    BaselineCompareError,
    compare_runs,
    compare_screenshots,
)
from qaura.reporting.models import Evidence, Finding, RunReport, RunSummary, Severity


def _make_png(path: Path, color: tuple[int, int, int], size=(100, 100)) -> None:
    Image.new("RGB", size, color).save(path)


def test_compare_screenshots_identical_images_zero_change(tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    _make_png(a, (255, 0, 0))
    _make_png(b, (255, 0, 0))
    changed_fraction, dims_match = compare_screenshots(str(a), str(b))
    assert changed_fraction == 0.0
    assert dims_match is True


def test_compare_screenshots_completely_different_images_full_change(tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    _make_png(a, (255, 0, 0))
    _make_png(b, (0, 0, 255))
    changed_fraction, dims_match = compare_screenshots(str(a), str(b))
    assert changed_fraction == 1.0
    assert dims_match is True


def _make_complex_png(path: Path, seed: int, size=(300, 400)) -> None:
    """A page-like image (varied colored blocks, not a flat fill) — closer to a real
    full-page screenshot's pixel distribution than a single solid color, without
    depending on gitignored output from an actual qaura run existing on disk."""
    from PIL import ImageDraw

    img = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(img)
    rng = seed
    for i in range(20):
        rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
        x0, y0 = rng % size[0], (rng // size[0]) % size[1]
        x1, y1 = min(x0 + 30, size[0]), min(y0 + 20, size[1])
        color = (rng % 256, (rng // 256) % 256, (rng // 65536) % 256)
        draw.rectangle([x0, y0, x1, y1], fill=color)
    img.save(path)


def test_compare_screenshots_on_complex_page_like_images(tmp_path):
    # Regression: the old version of this test globbed `runs/screenshot_check/`,
    # which is gitignored — it silently skipped forever on every fresh clone rather
    # than exercising the diff math on anything beyond flat solid-color fixtures.
    # Full-page screenshots taken at different points in a crawl can legitimately
    # have different heights (dynamic content growing/shrinking the scrollable
    # page), so this also covers a same-width-different-height pair.
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    _make_complex_png(a, seed=1, size=(300, 400))
    _make_complex_png(b, seed=2, size=(300, 450))

    changed_fraction, dims_match = compare_screenshots(str(a), str(b))
    assert isinstance(dims_match, bool)
    assert 0.0 <= changed_fraction <= 1.0
    if not dims_match:
        assert changed_fraction == 1.0  # dimension mismatch always reports as fully changed

    # Compare an image against itself: same file, must always match exactly.
    same_fraction, same_dims = compare_screenshots(str(a), str(a))
    assert same_dims is True
    assert same_fraction == 0.0


def test_compare_screenshots_dimension_mismatch(tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    _make_png(a, (255, 0, 0), size=(100, 100))
    _make_png(b, (255, 0, 0), size=(200, 100))
    changed_fraction, dims_match = compare_screenshots(str(a), str(b))
    assert dims_match is False
    assert changed_fraction == 1.0


def test_compare_screenshots_tolerates_tiny_noise(tmp_path):
    # A 5-value RGB difference is below PIXEL_DIFF_TOLERANCE (20) -- shouldn't
    # register as a changed pixel (anti-aliasing / compression noise tolerance).
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    _make_png(a, (100, 100, 100))
    _make_png(b, (105, 100, 100))
    changed_fraction, dims_match = compare_screenshots(str(a), str(b))
    assert changed_fraction == 0.0


def test_compare_screenshots_raises_for_missing_file(tmp_path):
    a = tmp_path / "a.png"
    _make_png(a, (255, 0, 0))
    try:
        compare_screenshots(str(a), str(tmp_path / "nonexistent.png"))
        assert False, "expected BaselineCompareError"
    except BaselineCompareError:
        pass


def _finding(title: str, detector: str, screenshot_path: str | None, url: str = "http://x/") -> Finding:
    return Finding(
        title=title, detector=detector, severity=Severity.MEDIUM, url=url,
        evidence=Evidence(screenshot_path=screenshot_path),
    )


def _report(findings) -> RunReport:
    return RunReport(
        summary=RunSummary(target_url="http://x/", started_at="t1", finished_at="t2", mode="heuristic"),
        findings=findings,
    )


def test_compare_runs_flags_recurring_finding_with_changed_screenshot(tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    _make_png(a, (255, 0, 0))
    _make_png(b, (0, 0, 255))

    baseline = _report([_finding("Visual issue: contrast too low", "visual", str(a))])
    current = _report([_finding("Visual issue: contrast too low", "visual", str(b))])

    results = compare_runs(baseline, current)
    assert len(results) == 1
    assert results[0].detector == "visual_baseline"


def test_compare_runs_no_finding_when_screenshot_unchanged(tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    _make_png(a, (255, 0, 0))
    _make_png(b, (255, 0, 0))

    baseline = _report([_finding("Visual issue: contrast too low", "visual", str(a))])
    current = _report([_finding("Visual issue: contrast too low", "visual", str(b))])

    assert compare_runs(baseline, current) == []


def test_compare_runs_skips_findings_with_no_screenshot():
    baseline = _report([_finding("x", "crash", None)])
    current = _report([_finding("x", "crash", None)])
    assert compare_runs(baseline, current) == []


def test_compare_runs_skips_findings_with_no_baseline_match(tmp_path):
    b = tmp_path / "b.png"
    _make_png(b, (0, 0, 255))
    baseline = _report([])  # nothing to match against
    current = _report([_finding("brand new bug", "crash", str(b))])
    assert compare_runs(baseline, current) == []
