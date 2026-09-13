"""cli.py had zero direct tests despite being the largest module in the project —
including the --ci/--fail-on severity gate that users wire into their own CI
pipelines. Covers the pure-logic pieces (the CI gate, config/report-loading error
handling) via direct import, plus a few commands cheap enough to run for real
through Typer's CliRunner without needing a live target or an LLM credential.
"""
import pytest
from typer.testing import CliRunner

from qaura import __version__
from qaura.cli import _CI_SEVERITY_RANK, _evaluate_ci_gate, app
from qaura.reporting.models import Finding, Severity

runner = CliRunner()


# --- _evaluate_ci_gate -------------------------------------------------------

def _finding(severity: Severity) -> Finding:
    return Finding(title="x", detector="crash", severity=severity, url="http://x/")


def test_ci_gate_passes_when_nothing_meets_threshold():
    findings = [_finding(Severity.LOW), _finding(Severity.MEDIUM)]
    passed, offenders = _evaluate_ci_gate(findings, "high")
    assert passed is True
    assert offenders == []


def test_ci_gate_fails_when_a_finding_meets_threshold():
    findings = [_finding(Severity.LOW), _finding(Severity.CRITICAL)]
    passed, offenders = _evaluate_ci_gate(findings, "high")
    assert passed is False
    assert len(offenders) == 1
    assert offenders[0].severity == Severity.CRITICAL


def test_ci_gate_threshold_is_inclusive():
    findings = [_finding(Severity.HIGH)]
    passed, offenders = _evaluate_ci_gate(findings, "high")
    assert passed is False
    assert len(offenders) == 1


def test_ci_gate_is_case_insensitive():
    findings = [_finding(Severity.CRITICAL)]
    passed, _ = _evaluate_ci_gate(findings, "CRITICAL")
    assert passed is False


def test_ci_gate_raises_on_unknown_severity_name():
    with pytest.raises(ValueError, match="catastrophic"):
        _evaluate_ci_gate([], "catastrophic")


def test_ci_severity_rank_is_monotonically_ordered():
    ordered = ["info", "low", "medium", "high", "critical"]
    ranks = [_CI_SEVERITY_RANK[s] for s in ordered]
    assert ranks == sorted(ranks)


# --- CLI commands, via CliRunner --------------------------------------------

def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_all_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("doctor", "observe", "run", "replay", "report", "auth", "ml"):
        assert command in result.stdout


def test_auth_list_with_nothing_captured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["auth", "list"])
    assert result.exit_code == 0
    assert "No captured sessions" in result.stdout


def test_replay_on_nonexistent_path_exits_cleanly():
    result = runner.invoke(app, ["replay", "does/not/exist.json"])
    assert result.exit_code == 1


def test_report_on_nonexistent_path_exits_cleanly():
    result = runner.invoke(app, ["report", "does/not/exist.json"])
    assert result.exit_code == 1


def test_report_on_corrupt_json_exits_cleanly(tmp_path):
    bad = tmp_path / "report.json"
    bad.write_text("{not valid json", encoding="utf-8")
    result = runner.invoke(app, ["report", str(bad)])
    assert result.exit_code == 1
    assert "Could not load" in result.stdout


def test_run_with_malformed_config_exits_cleanly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bad.yaml").write_text("seed: not_a_number\n", encoding="utf-8")
    result = runner.invoke(app, ["run", "--url", "http://x/", "--config", "bad.yaml", "--no-llm"])
    assert result.exit_code == 1
    assert "Config error" in result.stdout


def test_run_with_no_target_url_exits_cleanly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["run", "--no-llm"])
    assert result.exit_code == 1
    assert "No target URL" in result.stdout


def test_run_rejects_unknown_fail_on_before_crawling(tmp_path, monkeypatch):
    import qaura.browser.driver as driver_module

    async def _never(self):
        raise AssertionError("a browser was launched before --fail-on was validated")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(driver_module.Driver, "__aenter__", _never)
    result = runner.invoke(app, ["run", "--url", "http://x.test/", "--no-llm", "--ci", "--fail-on", "hgih"])
    assert result.exit_code == 2
    assert "--fail-on" in result.stdout


# --- helpers -----------------------------------------------------------------

def test_default_scope_uses_target_host_when_unset(tmp_path, monkeypatch):
    from qaura.cli import _default_scope
    from qaura.config import QAuraConfig

    monkeypatch.chdir(tmp_path)
    cfg = QAuraConfig()
    assert _default_scope(cfg, "http://Shop.test:8080/cart") == "shop.test"
    assert cfg.guardrails.allowed_domains == ["shop.test"]


def test_default_scope_keeps_configured_domains(tmp_path, monkeypatch):
    from qaura.cli import _default_scope
    from qaura.config import GuardrailConfig, QAuraConfig

    monkeypatch.chdir(tmp_path)
    cfg = QAuraConfig(guardrails=GuardrailConfig(allowed_domains=["a.test"]))
    assert _default_scope(cfg, "http://b.test/") is None
    assert cfg.guardrails.allowed_domains == ["a.test"]


def test_unique_dir_appends_a_suffix_when_taken(tmp_path):
    from qaura.cli import _unique_dir

    first = _unique_dir(tmp_path / "20260913_101010")
    first.mkdir()
    second = _unique_dir(tmp_path / "20260913_101010")
    assert first.name == "20260913_101010"
    assert second.name == "20260913_101010_2"
