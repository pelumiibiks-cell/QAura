"""The authorization gate. Pure, no browser, no network."""
import pytest
from rich.console import Console

from qaura.init.consent import ConsentDeclined, is_remote_target, require_authorization


@pytest.mark.parametrize("url", [
    "http://localhost:8099/",
    "http://127.0.0.1:8099/",
    "http://[::1]:3000/",
    "http://0.0.0.0:8080/",
    "http://10.0.0.5/",
    "http://192.168.1.1/",
    "http://172.16.0.1/",          # bottom of the private /12
    "http://172.31.255.254/",      # top of the private /12
    "http://169.254.1.1/",         # link-local
    "http://myapp.local/",
    "http://myapp.test/",
    "http://app.localhost/",
    "http://svc.internal/",
])
def test_local_targets(url):
    assert not is_remote_target(url)


@pytest.mark.parametrize("url", [
    "https://example.com/",
    "https://staging.example.com/app",
    "http://8.8.8.8/",
    # The prefix-string trap: 172.16/12 is private, but 172.32 is ordinary public
    # internet. A "starts with 172." check would wave this straight through.
    "http://172.32.0.1/",
    "http://172.15.0.1/",
    "http://11.0.0.1/",
])
def test_remote_targets(url):
    assert is_remote_target(url)


def test_local_target_needs_no_prompt():
    statement = require_authorization(
        "http://localhost:8099/", Console(), max_pages=25, rps=2.0
    )
    assert "no authorization prompt" in statement


def test_assume_yes_records_the_assertion():
    statement = require_authorization(
        "https://example.com/", Console(), max_pages=25, rps=1.0, assume_yes=True
    )
    assert "example.com" in statement
    assert "GET and HEAD" in statement


def test_remote_without_a_tty_is_refused(monkeypatch):
    """Refusing here rather than defaulting to yes is the point: a non-interactive
    context cannot consent on the operator's behalf."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(ConsentDeclined, match="no terminal"):
        require_authorization("https://example.com/", Console(), max_pages=25, rps=1.0)


def test_typing_the_hostname_confirms(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "example.com")
    statement = require_authorization("https://example.com/app", Console(), max_pages=5, rps=1.0)
    assert "example.com" in statement


def test_hostname_comparison_is_case_insensitive(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "EXAMPLE.COM")
    assert require_authorization("https://example.com/", Console(), max_pages=5, rps=1.0)


def test_typing_yes_does_not_confirm(monkeypatch):
    """'yes' is exactly the answer muscle memory produces, which is why it is not the
    accepted one."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "yes")
    with pytest.raises(ConsentDeclined):
        require_authorization("https://example.com/", Console(), max_pages=5, rps=1.0)


def test_wrong_hostname_is_refused(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "evil.example.net")
    with pytest.raises(ConsentDeclined):
        require_authorization("https://example.com/", Console(), max_pages=5, rps=1.0)


def test_interrupt_is_treated_as_decline(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def _raise(*a):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _raise)
    with pytest.raises(ConsentDeclined):
        require_authorization("https://example.com/", Console(), max_pages=5, rps=1.0)
