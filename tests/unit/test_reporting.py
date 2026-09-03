from qaura.reporting.html import render_html
from qaura.reporting.models import Evidence, Finding, ReproStep, RunReport, RunSummary, Severity


def _report(findings=None) -> RunReport:
    summary = RunSummary(
        target_url="http://shop.test", started_at="2026-08-30T00:00:00Z",
        finished_at="2026-08-30T00:05:00Z", mode="heuristic", personas=[],
        coverage={"states": 3, "unexplored_states": 1},
    )
    return RunReport(summary=summary, findings=findings or [])


def test_finding_to_dict_serializes_severity_as_string():
    f = Finding(title="x", detector="crash", severity=Severity.HIGH)
    d = f.to_dict()
    assert d["severity"] == "high"


def test_run_report_by_severity_buckets():
    findings = [
        Finding(title="a", severity=Severity.HIGH),
        Finding(title="b", severity=Severity.HIGH),
        Finding(title="c", severity=Severity.LOW),
    ]
    report = _report(findings)
    buckets = report.by_severity()
    assert len(buckets["high"]) == 2
    assert len(buckets["low"]) == 1
    assert len(buckets["critical"]) == 0


def test_run_report_to_json_roundtrip_shape():
    report = _report([Finding(title="a", severity=Severity.MEDIUM)])
    text = report.to_json()
    assert '"title": "a"' in text
    assert '"severity": "medium"' in text


def test_render_html_empty_findings_says_no_findings():
    report = _report([])
    html = render_html(report)
    assert "No findings" in html
    assert "shop.test" in html


def test_render_html_includes_finding_details():
    finding = Finding(
        title="Uncaught page error: boom",
        detector="crash",
        severity=Severity.HIGH,
        url="http://shop.test/cart",
        description="Something broke",
        repro_steps=[ReproStep(description="click e3 'Checkout'")],
        evidence=Evidence(page_error="TypeError: boom"),
    )
    html = render_html(_report([finding]))
    assert "Uncaught page error" in html
    assert "click e3 &#39;Checkout&#39;" in html or "click e3 'Checkout'" in html
    assert "TypeError: boom" in html
    assert "sev-high" in html


def test_render_html_shows_unreached_elements_when_present():
    summary = RunSummary(
        target_url="http://shop.test", started_at="t1", finished_at="t2", mode="heuristic",
        coverage={"states": 2, "unexplored_states": 1, "unreached": ["button: 'Wishlist' (http://shop.test/product)"]},
    )
    report = RunReport(summary=summary, findings=[])
    html = render_html(report)
    assert "never interacted with" in html
    assert "Wishlist" in html


def test_render_html_omits_unreached_section_when_absent():
    html = render_html(_report([]))
    assert "never interacted with" not in html


def test_render_html_escapes_untrusted_content():
    # a finding description containing markup should not inject raw HTML —
    # autoescape must be doing its job
    finding = Finding(title="x", description="<script>alert(1)</script>")
    html = render_html(_report([finding]))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
