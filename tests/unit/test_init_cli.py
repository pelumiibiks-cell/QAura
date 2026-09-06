"""`qaura init` at the CLI boundary.

The contract worth testing here is mostly about what the command must NOT do: touch
qaura.yaml, reach the network without consent, or overwrite an existing proposal.
"""
import hashlib

import httpx
import pytest
from typer.testing import CliRunner

from qaura.cli import app

runner = CliRunner()
FIXTURE_URL = "http://127.0.0.1:8099/"


def _fixture_is_up() -> bool:
    try:
        httpx.get(FIXTURE_URL, timeout=1.0)
        return True
    except Exception:
        return False


def test_init_is_registered():
    result = runner.invoke(app, ["init", "--help"])
    assert result.exit_code == 0
    assert "--url" in result.stdout
    assert "--login-form" in result.stdout
    assert "--check" in result.stdout


def test_remote_url_without_consent_exits_without_touching_the_network(monkeypatch, tmp_path):
    """The consent gate has to run before anything else. If a network call happens first,
    the gate is decorative."""
    def _explode(*args, **kwargs):
        raise AssertionError("network was touched before the consent gate")

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(httpx.AsyncClient, "get", _explode)

    result = runner.invoke(app, [
        "init", "--url", "https://example.com/", "--no-llm",
        "--out", str(tmp_path / "gen.yaml"),
    ])
    assert result.exit_code == 2
    assert not (tmp_path / "gen.yaml").exists()


def test_missing_role_session_exits_with_the_capture_command(tmp_path):
    """Deliberately harsher than observe/run, which warn and continue anonymously.
    Silently emitting an anonymous-only config under --role admin would look
    authoritative while describing the logged-out shell."""
    result = runner.invoke(app, [
        "init", "--url", FIXTURE_URL, "--role", "definitely-not-captured",
        "--no-llm", "--out", str(tmp_path / "gen.yaml"),
    ])
    assert result.exit_code == 1
    assert "auth capture" in result.stdout
    assert not (tmp_path / "gen.yaml").exists()


def test_existing_output_is_not_overwritten_without_force(tmp_path):
    target = tmp_path / "gen.yaml"
    target.write_text("# do not clobber me\n", encoding="utf-8")

    result = runner.invoke(app, [
        "init", "--url", FIXTURE_URL, "--no-llm", "--out", str(target),
    ])
    assert result.exit_code == 1
    assert target.read_text(encoding="utf-8") == "# do not clobber me\n"


pytestmark_live = pytest.mark.skipif(
    not _fixture_is_up(), reason="buggy_app fixture not running on :8099"
)


@pytestmark_live
def test_generated_config_is_loadable(tmp_path):
    from qaura.config import load_config

    target = tmp_path / "gen.yaml"
    result = runner.invoke(app, [
        "init", "--url", FIXTURE_URL, "--no-llm", "--max-pages", "2",
        "--out", str(target),
    ])
    assert result.exit_code == 0, result.stdout
    assert target.exists()

    config = load_config(target)
    assert config.target_url
    assert config.guardrails.allowed_domains == ["127.0.0.1"]
    assert config.guardrails.allow_destructive is False


@pytestmark_live
def test_qaura_yaml_is_never_touched(tmp_path, monkeypatch):
    """The core promise of the command, asserted rather than merely commented."""
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / "qaura.yaml"
    existing.write_text("target_url: http://untouched.example/\n", encoding="utf-8")
    before = hashlib.sha256(existing.read_bytes()).hexdigest()

    result = runner.invoke(app, [
        "init", "--url", FIXTURE_URL, "--no-llm", "--max-pages", "1",
        "--out", str(tmp_path / "gen.yaml"),
    ])
    assert result.exit_code == 0, result.stdout
    assert hashlib.sha256(existing.read_bytes()).hexdigest() == before


@pytestmark_live
def test_force_overwrites(tmp_path):
    target = tmp_path / "gen.yaml"
    target.write_text("# stale\n", encoding="utf-8")

    result = runner.invoke(app, [
        "init", "--url", FIXTURE_URL, "--no-llm", "--max-pages", "1",
        "--out", str(target), "--force",
    ])
    assert result.exit_code == 0, result.stdout
    assert "# stale" not in target.read_text(encoding="utf-8")


@pytestmark_live
def test_check_reports_a_rotted_selector_and_names_it(tmp_path):
    """A CSS selector is mostly square brackets, which rich parses as console markup and
    swallows. Unescaped, this table would report "selector '' matched nothing" and hide
    the one detail the reader needs."""
    config = tmp_path / "rotted.yaml"
    config.write_text(
        "target_url: http://127.0.0.1:8099/\n"
        "invariants:\n"
        "  - name: rotted_rule\n"
        "    description: references a selector that no longer exists\n"
        "    values:\n"
        '      subtotal: \'[data-testid="cart-subtotal-RENAMED"]\'\n'
        '      total: \'[data-testid="cart-total"]\'\n'
        "    expression: subtotal <= total\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, [
        "init", "--url", FIXTURE_URL, "--check", "--config", str(config),
    ])
    assert result.exit_code == 1, result.stdout
    assert "unmatched" in result.stdout
    assert "cart-subtotal-RENAMED" in result.stdout


@pytestmark_live
def test_check_passes_on_selectors_that_still_match(tmp_path):
    config = tmp_path / "healthy.yaml"
    config.write_text(
        "target_url: http://127.0.0.1:8099/\n"
        "invariants:\n"
        "  - name: healthy_rule\n"
        "    description: still applies\n"
        '    container_selector: \'[data-testid="cart"]\'\n'
        "    values:\n"
        '      subtotal: \'[data-testid="cart-subtotal"]\'\n'
        '      total: \'[data-testid="cart-total"]\'\n'
        "    expression: abs(total - subtotal) <= 0.01\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, [
        "init", "--url", FIXTURE_URL, "--check", "--config", str(config),
    ])
    assert result.exit_code == 0, result.stdout
    assert "holds" in result.stdout


@pytestmark_live
def test_no_llm_still_produces_a_useful_config(tmp_path):
    """Guardrails and personas need no credential, so the command has to be worth running
    without one."""
    target = tmp_path / "gen.yaml"
    result = runner.invoke(app, [
        "init", "--url", FIXTURE_URL, "--no-llm", "--max-pages", "1", "--out", str(target),
    ])
    assert result.exit_code == 0, result.stdout

    text = target.read_text(encoding="utf-8")
    assert "guardrails:" in text
    assert "personas:" in text
    assert "invariants: []" in text
    assert "confidence: high" in text
