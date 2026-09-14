import pytest
from typer.testing import CliRunner

from qaura.browser.auth import list_captured, resolve_role, storage_state_path, validate_role_name
from qaura.cli import app

runner = CliRunner()


@pytest.mark.parametrize("role", ["user", "admin", "qa_lead", "role-2", "A" * 64])
def test_valid_role_names_are_accepted(role, tmp_path):
    assert storage_state_path(role, tmp_path) == tmp_path / f"{role}.json"


@pytest.mark.parametrize("role", ["", "../evil", "a/b", "a\\b", "..", "role.json", "has space", "A" * 65])
def test_invalid_role_names_are_rejected(role):
    with pytest.raises(ValueError, match="role name"):
        validate_role_name(role)


def test_storage_state_path_never_escapes_auth_dir(tmp_path):
    with pytest.raises(ValueError):
        storage_state_path("../../outside", tmp_path)


def test_resolve_role_returns_none_when_nothing_captured(tmp_path):
    assert resolve_role("user", tmp_path) is None


def test_resolve_role_finds_captured_session(tmp_path):
    (tmp_path / "admin.json").write_text("{}", encoding="utf-8")
    assert resolve_role("admin", tmp_path) == str(tmp_path / "admin.json")


def test_list_captured_lists_roles(tmp_path):
    (tmp_path / "user.json").write_text("{}", encoding="utf-8")
    (tmp_path / "admin.json").write_text("{}", encoding="utf-8")
    assert list_captured(tmp_path) == ["admin", "user"]


def test_auth_capture_rejects_bad_role_before_opening_a_browser(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["auth", "capture", "--url", "http://x.test/", "--role", "../evil"])
    assert result.exit_code == 2
    assert "role name" in result.stdout
    assert not (tmp_path.parent / "evil.json").exists()


def test_run_rejects_bad_role(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["run", "--url", "http://x.test/", "--no-llm", "--role", "a/b"])
    assert result.exit_code == 2
