from qaura.analysis.dedupe import dedupe, fingerprint, normalize_title
from qaura.reporting.models import Finding, Severity


def _finding(title: str, detector: str = "invariant", url: str = "http://shop.test/cart") -> Finding:
    return Finding(title=title, detector=detector, url=url, severity=Severity.HIGH)


def test_normalize_title_collapses_digit_runs():
    assert normalize_title("Long task(s) blocking the main thread (3 >= 200ms)") == \
        normalize_title("Long task(s) blocking the main thread (7 >= 200ms)")


def test_normalize_title_keeps_quoted_names_distinct():
    a = normalize_title('button ("Claim offer"): contrast ratio 1.09 below 4.5')
    b = normalize_title('button ("Submit"): contrast ratio 1.09 below 4.5')
    assert a != b


def test_fingerprint_same_for_identical_repeated_finding():
    a = _finding("Invariant violated: total_reflects_discount")
    b = _finding("Invariant violated: total_reflects_discount")
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_differs_across_detectors():
    a = _finding("something broke", detector="crash")
    b = _finding("something broke", detector="console")
    assert fingerprint(a) != fingerprint(b)


def test_fingerprint_collapses_id_bearing_urls():
    a = _finding("Invariant violated: x", url="http://shop.test/product/1")
    b = _finding("Invariant violated: x", url="http://shop.test/product/2")
    assert fingerprint(a) == fingerprint(b)


def test_dedupe_collapses_fifteen_identical_invariant_findings():
    # The exact real scenario from Phase 4's live verification run.
    findings = [_finding("Invariant violated: total_reflects_discount") for _ in range(15)]
    result = dedupe(findings)
    assert result.total_before == 15
    assert result.total_after == 1
    assert result.merged_count == 14
    assert result.findings[0].occurrence_count == 15


def test_dedupe_keeps_distinct_findings_separate():
    findings = [
        _finding("Invariant violated: total_reflects_discount"),
        _finding("Uncaught page error: TypeError", detector="crash"),
        _finding("Console error: 404 not found", detector="console"),
    ]
    result = dedupe(findings)
    assert result.total_after == 3
    assert all(f.occurrence_count == 1 for f in result.findings)


def test_dedupe_preserves_first_appearance_order():
    a = _finding("first bug", detector="crash")
    b = _finding("second bug", detector="console")
    a2 = _finding("first bug", detector="crash")
    result = dedupe([a, b, a2])
    assert [f.title for f in result.findings] == ["first bug", "second bug"]


def test_dedupe_empty_list():
    result = dedupe([])
    assert result.findings == []
    assert result.total_before == 0
    assert result.total_after == 0


def test_dedupe_keeps_most_severe_representative():
    low = Finding(title="Uncaught page error", detector="crash", url="http://shop.test/", severity=Severity.LOW)
    high = Finding(title="Uncaught page error", detector="crash", url="http://shop.test/", severity=Severity.HIGH)
    result = dedupe([low, high])
    assert result.total_after == 1
    assert result.findings[0].severity == Severity.HIGH
    assert result.findings[0].occurrence_count == 2


def test_dedupe_does_not_mutate_its_input():
    a = _finding("Invariant violated: x")
    b = _finding("Invariant violated: x")
    dedupe([a, b])
    assert a.occurrence_count == 1
    assert b.occurrence_count == 1


def test_dedupe_counts_survive_a_second_pass():
    first = dedupe([_finding("Invariant violated: x") for _ in range(3)]).findings
    again = dedupe(first + [_finding("Invariant violated: x")])
    assert again.findings[0].occurrence_count == 4


def test_normalize_title_keeps_digits_inside_quotes():
    a = normalize_title('button ("Item 2") has no accessible name')
    b = normalize_title('button ("Item 3") has no accessible name')
    assert a != b
