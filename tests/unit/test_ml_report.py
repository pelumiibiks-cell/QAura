from qaura.mltest.report import build_run_report, overall_gate
from qaura.reporting.html import render_html
from qaura.reporting.models import Finding, Severity


def test_overall_gate_pass_for_no_findings():
    assert overall_gate([]) == "PASS"


def test_overall_gate_pass_for_info_only():
    assert overall_gate([Finding(severity=Severity.INFO)]) == "PASS"


def test_overall_gate_warn_for_medium():
    assert overall_gate([Finding(severity=Severity.MEDIUM)]) == "WARN"


def test_overall_gate_warn_for_low():
    assert overall_gate([Finding(severity=Severity.LOW)]) == "WARN"


def test_overall_gate_fail_for_high():
    assert overall_gate([Finding(severity=Severity.HIGH)]) == "FAIL"


def test_overall_gate_fail_for_critical():
    assert overall_gate([Finding(severity=Severity.CRITICAL)]) == "FAIL"


def test_overall_gate_fail_wins_over_warn():
    findings = [Finding(severity=Severity.LOW), Finding(severity=Severity.CRITICAL)]
    assert overall_gate(findings) == "FAIL"


def test_build_run_report_produces_valid_report():
    findings = [Finding(title="x", detector="ml_calibration", severity=Severity.MEDIUM, url="model.joblib")]
    report = build_run_report("model.joblib", findings, mode="ml_artifact")
    assert report.summary.target_url == "model.joblib"
    assert report.summary.mode == "ml_artifact"
    assert report.findings == findings


def test_build_run_report_renders_through_existing_html_shell():
    # The whole point of report.py: reuse reporting/html.py unmodified.
    findings = [Finding(title="Poor calibration", detector="ml_calibration", severity=Severity.MEDIUM, url="model.joblib")]
    report = build_run_report("model.joblib", findings)
    html = render_html(report)
    assert "Poor calibration" in html
    assert "model.joblib" in html


def test_ml_metrics_do_not_render_as_misleading_crawl_coverage():
    # Regression: build_run_report stuffs arbitrary ML metrics (accuracy, etc.) into
    # RunSummary.coverage for JSON persistence — that dict is truthy but has no
    # "total_elements" key, and html.py used to treat any truthy coverage dict as
    # crawl coverage, rendering every ML report as "0 states explored / 0 of 0
    # elements exercised" regardless of what the metrics actually said.
    findings = [Finding(title="x", detector="ml_calibration", severity=Severity.MEDIUM, url="model.joblib")]
    report = build_run_report("model.joblib", findings, metrics={"accuracy": 0.91, "f1": 0.88})
    html = render_html(report)
    assert "states explored" not in html
    assert "elements exercised" not in html
