"""Storage-state auth capture and the multi-role session store. Plan decision: rather
than scripted credential login (breaks on MFA/SSO) or having the agent guess a login
flow, the human logs in once in a visible browser and we save cookies/localStorage —
every subsequent run replays that state. Multiple named roles (user/admin/anon) let
personas probe cross-role boundaries.
"""
from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable
from pathlib import Path

from playwright.async_api import async_playwright

DEFAULT_AUTH_DIR = Path(".qaura/auth")

_ROLE_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def validate_role_name(role: str) -> str:
    """Role names become file names, so `--role ../x` must not write outside the auth dir."""
    if not _ROLE_NAME_RE.fullmatch(role or ""):
        raise ValueError(f"invalid role name {role!r}: use 1-64 letters, digits, '_' or '-'")
    return role


def storage_state_path(role: str, auth_dir: str | Path = DEFAULT_AUTH_DIR) -> Path:
    return Path(auth_dir) / f"{validate_role_name(role)}.json"


def restrict_permissions(path: Path) -> None:
    """Session files hold live cookies, so make them owner-only where the OS supports it."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


async def capture(
    url: str, role: str = "user", auth_dir: str | Path = DEFAULT_AUTH_DIR,
    *, on_ready: Callable[[str, str], None],
) -> Path:
    """Opens a real, visible browser at `url`, waits for `on_ready(url, role)` to
    return, then saves storage state. Deliberately not headless — the whole point is
    a human completing whatever auth flow the app requires, MFA included.

    `on_ready` is required rather than defaulted, and does the actual waiting (e.g.
    print a prompt and block on input()) — previously that terminal I/O lived HERE,
    in a library function, which made it untestable without monkeypatching stdin and
    meant it hung forever with no TTY attached (a service context, piped stdin, CI).
    cli.py's `auth capture` command is what supplies the interactive version. It runs
    in a worker thread so a blocking input() doesn't stall the event loop.
    """
    out_path = storage_state_path(role, auth_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(no_viewport=True)
        page = await context.new_page()
        await page.goto(url)

        await asyncio.to_thread(on_ready, url, role)

        await context.storage_state(path=str(out_path))
        restrict_permissions(out_path)
        await browser.close()

    return out_path


def list_captured(auth_dir: str | Path = DEFAULT_AUTH_DIR) -> list[str]:
    auth_dir = Path(auth_dir)
    if not auth_dir.exists():
        return []
    return sorted(p.stem for p in auth_dir.glob("*.json"))


def resolve_role(role: str, auth_dir: str | Path = DEFAULT_AUTH_DIR) -> str | None:
    """Returns a storage_state path for use with Driver's ContextSpec, or None for an
    anonymous context (e.g. role == "anon" and nothing was ever captured for it, which
    is the expected case — there's nothing to log in as). Raises ValueError for a role
    name that isn't safe to use as a file name."""
    path = storage_state_path(role, auth_dir)
    return str(path) if path.exists() else None
