"""Authorization gate for remote targets.

`qaura init --url <anything>` makes it a single command to point an automated crawler at
a website. Against localhost that is unremarkable. Against a host someone else operates
it is unauthorized testing, whatever the intent, so a remote target requires the operator
to state that they are allowed to test it.

The gate asks the user to type the target's hostname rather than "yes". That is
deliberate: "yes" becomes muscle memory after the third prompt, whereas typing the
hostname cannot be answered without reading which host is about to be touched. It also
catches the specific mistake this gate exists for — a copy-pasted URL pointing somewhere
the operator did not intend.
"""
from __future__ import annotations

import ipaddress
import sys
from urllib.parse import urlsplit

from rich.console import Console

# Suffixes that are local by convention rather than by address. `.local` is mDNS,
# `.test`/`.localhost` are reserved by RFC 2606/6761 for exactly this purpose.
_LOCAL_SUFFIXES = (".localhost", ".local", ".test", ".internal", ".localdomain", ".home.arpa")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]", ""})


class ConsentDeclined(RuntimeError):
    """The operator did not confirm authorization, or could not be asked."""


def is_remote_target(url: str) -> bool:
    """True if `url` points somewhere outside the operator's own machine or network.

    Address checks go through the ipaddress module, never string prefixes. The tempting
    shortcut — "starts with 172." — is wrong: 172.16.0.0/12 is private but 172.32.0.0 is
    ordinary public internet, and a prefix check silently waves the second one through.
    """
    host = (urlsplit(url).hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return False
    if any(host.endswith(suffix) for suffix in _LOCAL_SUFFIXES):
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return True  # a real DNS name
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved)


def authorization_statement(url: str, *, max_pages: int, rps: float) -> str:
    host = urlsplit(url).hostname or url
    return (
        f"I am authorized to run automated testing against {host}. "
        f"qaura init will issue at most {max_pages} page loads at up to {rps} req/s, "
        f"using only GET and HEAD requests."
    )


def require_authorization(
    url: str,
    console: Console,
    *,
    max_pages: int,
    rps: float,
    assume_yes: bool = False,
    login_form: bool = False,
) -> str:
    """Returns the assertion the operator agreed to, for the record. Raises
    ConsentDeclined if they did not, or if there is no way to ask them."""
    statement = authorization_statement(url, max_pages=max_pages, rps=rps)
    if not is_remote_target(url):
        return f"Local target ({urlsplit(url).hostname}); no authorization prompt required."

    if assume_yes:
        return f"Asserted via --yes: {statement}"

    if not sys.stdin.isatty():
        raise ConsentDeclined(
            f"{url} is a remote target and there is no terminal to confirm authorization on.\n"
            f"Re-run interactively, or pass --yes to assert: {statement}"
        )

    host = urlsplit(url).hostname or ""
    console.print()
    console.print(f"[bold yellow]{url} is a remote target.[/bold yellow]")
    console.print(f"  Requests:     GET and HEAD only, at most {max_pages} pages, up to {rps}/s")
    if login_form:
        console.print("  Exception:    --login-form permits exactly one POST (the login submit)")
    console.print("  Not sent:     no form submissions, no destructive clicks")
    console.print()
    console.print("Only continue if you own this site or have permission to test it.")
    console.print(f"Type the hostname [bold]{host}[/bold] to confirm, or anything else to cancel:")

    try:
        answer = input("> ").strip()
    except (EOFError, KeyboardInterrupt) as e:
        raise ConsentDeclined("Cancelled.") from e

    if answer.lower() != host.lower():
        raise ConsentDeclined(f"Expected {host!r}, got {answer!r} — cancelled.")
    return statement
