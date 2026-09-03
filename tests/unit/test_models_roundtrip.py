from qaura.reporting.models import Evidence, Finding, ReproStep, RunReport, RunSummary, Severity


def test_finding_from_dict_roundtrip():
    original = Finding(
        title="x", detector="crash", severity=Severity.HIGH, url="http://a/",
        description="d", repro_steps=[ReproStep(description="click", action_kind="click", ref="e1")],
        evidence=Evidence(console_errors=["err"]), likely_component="app.py",
        occurrence_count=3, reproducibility="2/3", confirmed=True,
    )
    restored = Finding.from_dict(original.to_dict())
    assert restored.title == original.title
    assert restored.severity == Severity.HIGH
    assert restored.repro_steps[0].ref == "e1"
    assert restored.evidence.console_errors == ["err"]
    assert restored.occurrence_count == 3
    assert restored.reproducibility == "2/3"
    assert restored.confirmed is True


def test_finding_from_dict_handles_empty_evidence_and_steps():
    original = Finding(title="x")
    restored = Finding.from_dict(original.to_dict())
    assert restored.repro_steps == []
    assert restored.evidence.console_errors == []


def test_run_report_save_and_load_json_roundtrip(tmp_path):
    summary = RunSummary(
        target_url="http://a/", started_at="t1", finished_at="t2", mode="heuristic",
        coverage={"states": 3},
    )
    findings = [Finding(title="bug one", severity=Severity.HIGH), Finding(title="bug two", severity=Severity.LOW)]
    report = RunReport(summary=summary, findings=findings)

    path = report.save_json(tmp_path / "report.json")
    restored = RunReport.load_json(path)

    assert restored.summary.target_url == "http://a/"
    assert restored.summary.coverage == {"states": 3}
    assert len(restored.findings) == 2
    assert {f.title for f in restored.findings} == {"bug one", "bug two"}
    assert restored.findings[0].severity in (Severity.HIGH, Severity.LOW)
