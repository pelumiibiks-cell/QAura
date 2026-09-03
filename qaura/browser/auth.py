"""Storage-state auth capture and the multi-role session store. Plan decision: rather
than scripted credential login (breaks on MFA/SSO) or having the agent guess a login
flow, the human logs in once in a visible browser and we save cookies/localStorage —
every subsequent run replays that state. Multiple named roles (user/admin/anon) let
personas probe cross-role boundaries.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from playwright.async_api import async_playwright

DEFAULT_AUTH_DIR = Path(".qaura/auth")


def storage_state_path(role: str, auth_dir: str | Path = DEFAULT_AUTH_DIR) -> Path:
    return Path(auth_dir) / f"{role}.json"


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
    cli.py's `auth capture` command is what supplies the interactive version.
    """
    out_path = storage_state_path(role, auth_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(no_viewport=True)
        page = await context.new_page()
        await page.goto(url)

        on_ready(url, role)

        await context.storage_state(path=str(out_path))
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
    is the expected case — there's nothing to log in as)."""
    path = storage_state_path(role, auth_dir)
    return str(path) if path.exists() else None
