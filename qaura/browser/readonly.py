"""Wire-level read-only enforcement for `qaura init`'s recon crawl.

Recon promises it will not change the target's state. That promise cannot be kept by
"don't submit forms": the target's own JavaScript fires requests on load, on scroll, and
on any click, and a page that POSTs an analytics beacon or a subscribe call during
hydration will do it whether or not the crawler touched a form. The only place the
promise can actually be enforced is at the request layer, before it leaves the browser.

So: one route handler on the CONTEXT (not the page, so popups and new tabs inherit it)
that aborts anything that isn't a GET or HEAD. Everything else in recon — frontier
filtering, guard_goto, the no-form-submit rule — is defence in depth on top of this.

The one deliberate hole is `allow_once`, used by assisted login (`--login-form`): a
login is a POST by definition, so a single, precisely-targeted exception is armed
immediately before the submit and disarmed immediately after. It matches one method +
URL exactly once. Without --login-form nothing ever arms it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from playwright.async_api import BrowserContext, Request, Route

from qaura.config import GuardrailConfig
from qaura.core.guardrails import is_in_scope

READ_ONLY_METHODS = frozenset({"GET", "HEAD"})

# Paths that mutate state via a plain GET. Rarer than they used to be, but far from
# extinct — Rails' `link_to ... method: :delete` degrades to a GET without JS, and
# "click here to unsubscribe" links in particular are almost always GETs. Blocking
# these is the difference between a read-only crawl and quietly logging the user out
# or deleting a row.
_DESTRUCTIVE_WORDS = (
    r"logout|log-out|signout|sign-out|logoff|delete|destroy|remove|purge"
    r"|cancel|checkout|unsubscribe|deactivate|terminate"
)
# The word may end the segment or be followed by an extension or separator: /logout.php, /delete-account
_DESTRUCTIVE_PATH_RE = re.compile(rf"(?:^|/)(?:{_DESTRUCTIVE_WORDS})(?:[/._?-]|$)", re.IGNORECASE)
# ?action=delete style endpoints carry the verb in the query string instead
_DESTRUCTIVE_QUERY_RE = re.compile(
    rf"(?:^|&)(?:action|op|cmd|do|task)=(?:{_DESTRUCTIVE_WORDS})(?:&|$)", re.IGNORECASE,
)
_UNTRUSTED_RESOURCE_TYPES = {"beacon", "ping"}


@dataclass
class AllowOnce:
    """A single-use exemption from the non-GET block, for assisted login.

    Scoped by origin rather than by exact URL. A classic <form method=post> submits to
    its own action, but plenty of logins are a fetch() to a completely different endpoint
    (`/api/session`, `/oauth/token`), and an exact-URL exemption would block exactly the
    case it exists to permit. Same-origin plus single-use plus armed-only-around-the-click
    keeps the blast radius to one request either way.

    `armed` is deliberately separate from `used`: arming happens immediately before the
    submit and disarming immediately after, so an exemption that is never consumed cannot
    leak into a later request.
    """

    origin: str
    methods: frozenset[str] = frozenset({"POST", "PUT", "PATCH"})
    armed: bool = False
    used: bool = False

    def matches(self, request: Request) -> bool:
        if not self.armed or self.used:
            return False
        if request.method.upper() not in self.methods:
            return False
        # Analytics beacons fire around the login click and must not spend the exemption
        if getattr(request, "resource_type", None) in _UNTRUSTED_RESOURCE_TYPES:
            return False
        parts = urlsplit(request.url)
        return f"{parts.scheme}://{parts.netloc}" == self.origin


@dataclass
class ReadOnlyLedger:
    """What the handler blocked, and why. Printed under --verbose and asserted in
    tests — an empty `aborted` list on an ordinary crawl is the evidence that recon
    really was read-only, so it is recorded rather than silently dropped."""

    aborted: list[tuple[str, str, str]] = field(default_factory=list)  # (method, url, reason)
    allowed: int = 0
    allowed_exceptions: list[tuple[str, str]] = field(default_factory=list)  # (method, url)

    def record_abort(self, request: Request, reason: str) -> None:
        self.aborted.append((request.method.upper(), request.url, reason))

    @property
    def non_get_attempts(self) -> int:
        return sum(1 for method, _, _ in self.aborted if method not in READ_ONLY_METHODS)

    def summary(self) -> str:
        if not self.aborted:
            return f"{self.allowed} request(s), all GET/HEAD, none blocked"
        return (
            f"{self.allowed} request(s) allowed, {len(self.aborted)} blocked "
            f"({self.non_get_attempts} non-GET)"
        )


def classify(request: Request, cfg: GuardrailConfig, allow_once: AllowOnce | None) -> str | None:
    """Returns an abort reason, or None to let the request through. Pulled out of the
    handler so the whole policy is testable without a browser."""
    if allow_once is not None and allow_once.matches(request):
        return None

    method = request.method.upper()
    if method not in READ_ONLY_METHODS:
        return f"non-read-only method {method}"

    # A navigation leaving scope is caught here rather than after the fact. The
    # existing enforce_scope_after_action() can only notice once the page has already
    # loaded off-target — by which point an out-of-scope host has seen the run's
    # cookies. Aborting the navigation request means it never gets them.
    if request.is_navigation_request() and not is_in_scope(request.url, cfg):
        return "navigation out of scope"

    parts = urlsplit(request.url)
    path = unquote(parts.path or "/")
    if _DESTRUCTIVE_PATH_RE.search(path) or _DESTRUCTIVE_QUERY_RE.search(parts.query):
        return "destructive path pattern"

    return None


async def install_readonly_routes(
    context: BrowserContext,
    cfg: GuardrailConfig,
    ledger: ReadOnlyLedger,
    allow_once: AllowOnce | None = None,
) -> None:
    async def _handler(route: Route, request: Request) -> None:
        reason = classify(request, cfg, allow_once)
        if reason is None:
            if allow_once is not None and allow_once.matches(request):
                allow_once.used = True
                ledger.allowed_exceptions.append((request.method.upper(), request.url))
            ledger.allowed += 1
            await route.continue_()
            return
        ledger.record_abort(request, reason)
        await route.abort()

    await context.route("**/*", _handler)


async def install_offline_routes(context: BrowserContext) -> None:
    """Aborts every request unconditionally. Used by the invariant validator, which
    replays captured HTML through page.set_content() — that HTML still references
    scripts, images and stylesheets, and none of them should be fetched: validation
    must be reproducible offline and must not touch the target again."""

    async def _handler(route: Route, request: Request) -> None:  # noqa: ARG001
        await route.abort()

    await context.route("**/*", _handler)
