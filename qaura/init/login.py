"""Login-wall detection, and the opt-in assisted login that can get past a simple one.

Two separate jobs. Detection always runs and is the reason a config generated against a
gated app says so instead of quietly describing the logged-out shell. Assisted login runs
only under --login-form and only for plain credential forms; MFA, SSO and CAPTCHA still
need `qaura auth capture`, which is the whole reason browser/auth.py works the way it
does.

The verdict that matters is the whole-crawl one, not the per-page one. A site with a
"Sign in" link in its header trips several per-page signals on every single page while
being entirely public; a site that redirects everything to /login trips the same signals
and is completely gated. Only the ratio across the crawl separates them.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from playwright.async_api import Page

from qaura.browser.observe import PageModel
from qaura.browser.readonly import AllowOnce
from qaura.core.forms import SUBMIT_NAME_HINTS
from qaura.core.state import url_template
from qaura.detectors.security import _BLOCKED_TEXT_MARKERS, _LOGIN_INDICATOR_NAME_SUBSTRINGS

if TYPE_CHECKING:
    from qaura.init.recon import ObservedState

_log = logging.getLogger(__name__)

WallKind = Literal["none", "login_page", "partial_wall", "full_wall"]

_LOGIN_PATH_HINTS = (
    "login", "log-in", "log_in", "signin", "sign-in", "sign_in",
    "auth", "sso", "session/new", "users/sign_in", "account/login",
)

# Role names that are conventionally privileged. Used to decide whether the anonymous
# differential probe is worth running.
ADMIN_ROLE_NAMES = frozenset({"admin", "administrator", "superuser", "staff", "owner", "root", "manager"})

_USERNAME_ATTR_HINTS = ("user", "email", "login", "account", "identifier", "handle", "phone")

LOGIN_PAGE_THRESHOLD = 0.6
FULL_WALL_RATIO = 0.6


@dataclass
class LoginWallSignal:
    kind: WallKind = "none"
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)
    login_url: str | None = None
    suggested_role: str = "user"

    @property
    def walled(self) -> bool:
        return self.kind in ("partial_wall", "full_wall")


def _path_looks_like_login(url: str) -> bool:
    path = (urlsplit(url).path or "/").lower()
    return any(hint in path for hint in _LOGIN_PATH_HINTS)


async def detect_login_wall(page: Page, model: PageModel, status: int | None) -> LoginWallSignal:
    """Per-page verdict. Weighted rather than any-of, because each individual signal has
    a common innocent explanation and only their combination is meaningful."""
    score = 0.0
    evidence: list[str] = []

    # Queried against the DOM directly, not via PageModel. A password input's ARIA role
    # mapping is inconsistent across browsers and it frequently does not surface as a
    # "textbox" in the accessibility tree at all, so the aria-derived model is the wrong
    # place to look for the single strongest signal available.
    try:
        if await page.locator("input[type=password]").count() > 0:
            score += 0.6
            evidence.append("password field present")
    except Exception:
        _log.debug("password probe failed on %s", page.url, exc_info=True)

    if status in (401, 403):
        score += 0.6
        evidence.append(f"HTTP {status}")

    if _path_looks_like_login(page.url):
        score += 0.3
        evidence.append(f"URL path looks like a login route ({urlsplit(page.url).path})")

    try:
        body_text = (await page.locator("body").text_content() or "").lower()
    except Exception:
        body_text = ""
    marker = next((m for m in _BLOCKED_TEXT_MARKERS if m in body_text), None)
    if marker:
        score += 0.25
        evidence.append(f"body text contains {marker!r}")

    names = [(e.name or "").lower() for e in model.elements]
    has_login_field = any(
        any(sub in name for sub in _LOGIN_INDICATOR_NAME_SUBSTRINGS) for name in names
    )
    has_submit = any(any(hint in name for hint in SUBMIT_NAME_HINTS) for name in names)
    if has_login_field and has_submit:
        score += 0.25
        evidence.append("login-shaped field and submit button")

    kind: WallKind = "login_page" if score >= LOGIN_PAGE_THRESHOLD else "none"
    return LoginWallSignal(
        kind=kind,
        confidence=min(score, 1.0),
        evidence=evidence,
        login_url=page.url if kind == "login_page" else None,
    )


def summarize_wall(states: list["ObservedState"], attempted: int) -> LoginWallSignal:
    """Whole-crawl verdict. `attempted` is how many navigations were tried, which is not
    len(states) — states are deduped by fingerprint, and a site that redirects everything
    to one login page collapses to a single state precisely because it is walled."""
    if not states:
        return LoginWallSignal(kind="none")

    walled = [s for s in states if s.login_signal.kind == "login_page"]
    if not walled:
        return LoginWallSignal(kind="none", evidence=[f"{len(states)} states, none login-shaped"])

    by_template: dict[str, int] = {}
    for state in walled:
        by_template[url_template(state.url)] = by_template.get(url_template(state.url), 0) + 1
    dominant_template, dominant_count = max(by_template.items(), key=lambda kv: kv[1])

    denominator = max(attempted, len(states), 1)
    ratio = dominant_count / denominator
    best = max(walled, key=lambda s: s.login_signal.confidence)

    evidence = list(best.login_signal.evidence)
    evidence.append(
        f"{dominant_count} of {denominator} crawled URL(s) resolved to {dominant_template}"
    )

    seed_is_login = states[0].login_signal.kind == "login_page"
    if ratio >= FULL_WALL_RATIO or (len(states) < 3 and seed_is_login):
        kind: WallKind = "full_wall"
    else:
        kind = "partial_wall"
        evidence.append(f"{len(states) - len(walled)} state(s) were reachable anonymously")

    return LoginWallSignal(
        kind=kind,
        confidence=best.login_signal.confidence,
        evidence=evidence,
        login_url=best.url,
    )


def guidance(signal: LoginWallSignal, seed_url: str, role: str | None, states: int) -> list[str]:
    """The message a user actually needs when recon hits a wall: what was seen, what it
    cost them, and the exact two commands that fix it."""
    target = signal.login_url or seed_url
    suggested = role or signal.suggested_role
    lines = [
        f"Login wall detected ({signal.kind.replace('_', ' ')}, confidence {signal.confidence:.2f}).",
        f"  Evidence: {'; '.join(signal.evidence)}.",
        f"  Only {states} state(s) were reachable, so this config describes the public surface only.",
        "",
        "To analyze authenticated routes, capture a session and re-run:",
        f"  qaura auth capture --url {target} --role {suggested}",
        f"  qaura init --url {seed_url} --role {suggested}",
    ]
    return lines


# --- assisted login ----------------------------------------------------------------


@dataclass
class AssistedLoginResult:
    ok: bool
    reason: str
    storage_state_path: Path | None = None
    posted: bool = False


def credentials_from_env() -> tuple[str | None, str | None]:
    """Credentials come from the environment only. Never a CLI argument (they land in
    shell history and process listings), never a config field (that file gets committed)."""
    return os.environ.get("QAURA_LOGIN_USER"), os.environ.get("QAURA_LOGIN_PASS")


async def _find_username_locator(page: Page, password_locator):
    """The username field is whatever text-ish input precedes the password one. Tried in
    order of how explicitly the page says so."""
    for selector in (
        "input[autocomplete='username']",
        "input[type=email]",
        "input[name*='user' i]", "input[name*='email' i]", "input[name*='login' i]",
        "input[id*='user' i]", "input[id*='email' i]", "input[id*='login' i]",
    ):
        try:
            loc = page.locator(selector).first
            if await loc.count() > 0 and await loc.is_visible():
                return loc
        except Exception:
            continue

    # Fall back to the last visible text input appearing before the password field.
    try:
        form = password_locator.locator("xpath=ancestor::form[1]")
        scope = form if await form.count() > 0 else page
        inputs = scope.locator("input[type=text], input:not([type])")
        count = await inputs.count()
        for i in range(count - 1, -1, -1):
            candidate = inputs.nth(i)
            if await candidate.is_visible():
                name = (await candidate.get_attribute("name") or "").lower()
                idv = (await candidate.get_attribute("id") or "").lower()
                if not name and not idv:
                    return candidate
                if any(h in name or h in idv for h in _USERNAME_ATTR_HINTS):
                    return candidate
        if count:
            return inputs.nth(0)
    except Exception:
        _log.debug("username field search failed", exc_info=True)
    return None


async def _find_submit_locator(page: Page, password_locator):
    try:
        form = password_locator.locator("xpath=ancestor::form[1]")
        scope = form if await form.count() > 0 else page
    except Exception:
        scope = page

    for selector in ("button[type=submit]", "input[type=submit]", "button"):
        try:
            loc = scope.locator(selector)
            count = await loc.count()
            for i in range(count):
                item = loc.nth(i)
                if not await item.is_visible():
                    continue
                text = ((await item.text_content()) or (await item.get_attribute("value")) or "").lower()
                if not text or any(hint in text for hint in SUBMIT_NAME_HINTS):
                    return item
            if count:
                return loc.first
        except Exception:
            continue
    return None


async def attempt_login(
    page: Page,
    context,
    allow_once: AllowOnce,
    *,
    role: str,
    auth_dir: Path,
) -> AssistedLoginResult:
    """One attempt at a plain credential login. Deliberately no retry: a wrong password
    retried is how an account gets locked, and recon has no way to tell a typo from a
    rate limit.

    The POST exemption is armed immediately before the submit and disarmed immediately
    after, in a finally block, so a failed or hanging submit cannot leave the read-only
    guarantee switched off for the rest of the crawl.
    """
    user, password = credentials_from_env()
    if not user or not password:
        return AssistedLoginResult(
            False, "QAURA_LOGIN_USER and QAURA_LOGIN_PASS must both be set for --login-form"
        )

    try:
        password_locator = page.locator("input[type=password]").first
        if await password_locator.count() == 0:
            return AssistedLoginResult(False, "no password field found on the login page")
    except Exception as e:
        return AssistedLoginResult(False, f"could not locate a password field: {e}")

    username_locator = await _find_username_locator(page, password_locator)
    if username_locator is None:
        return AssistedLoginResult(False, "found a password field but no username field")

    submit_locator = await _find_submit_locator(page, password_locator)
    if submit_locator is None:
        return AssistedLoginResult(False, "found a login form but no submit button")

    try:
        await username_locator.fill(user, timeout=5000)
        await password_locator.fill(password, timeout=5000)
    except Exception as e:
        return AssistedLoginResult(False, f"could not fill the login form: {e}")

    # Success has to be verified positively. Judging by "is the password field gone?"
    # alone treats any error page as a successful login — a 500 has no password field
    # either — and would save a useless session that recon then trusts.
    submit_statuses: list[int] = []

    def _on_response(response) -> None:
        try:
            if response.request.method.upper() != "GET":
                submit_statuses.append(response.status)
        except Exception:
            pass

    page.on("response", _on_response)
    nav_status: int | None = None

    allow_once.armed = True
    try:
        try:
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=15000) as nav:
                await submit_locator.click(timeout=5000)
            response = await nav.value
            nav_status = response.status if response is not None else None
        except Exception:
            # A fetch-based login never navigates. Give the request a beat to land and
            # judge it by the POST's own status instead of by a URL change.
            _log.debug("login submit did not navigate; checking for an in-place login", exc_info=True)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
    finally:
        allow_once.armed = False
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass

    posted = allow_once.used

    failed_status = next((s for s in submit_statuses if s >= 400), None)
    if failed_status is not None:
        return AssistedLoginResult(
            False,
            f"the login request was rejected (HTTP {failed_status}) — check "
            f"QAURA_LOGIN_USER/QAURA_LOGIN_PASS",
            posted=posted,
        )
    if nav_status is not None and nav_status >= 400:
        return AssistedLoginResult(
            False, f"the page after login returned HTTP {nav_status}", posted=posted
        )
    if not posted:
        return AssistedLoginResult(
            False, "submitting the form sent no request — this may not be a real login form",
            posted=posted,
        )

    try:
        still_walled = await page.locator("input[type=password]").count() > 0
    except Exception:
        still_walled = True

    if still_walled and _path_looks_like_login(page.url):
        return AssistedLoginResult(
            False, f"still on a login page after submitting ({page.url})", posted=posted
        )

    auth_dir.mkdir(parents=True, exist_ok=True)
    out_path = auth_dir / f"{role}.json"
    try:
        await context.storage_state(path=str(out_path))
    except Exception as e:
        return AssistedLoginResult(False, f"login appeared to succeed but session save failed: {e}",
                                   posted=posted)

    return AssistedLoginResult(True, f"logged in and saved session for role {role!r}",
                               storage_state_path=out_path, posted=posted)
