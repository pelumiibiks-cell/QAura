"""Security detector: two checks from the plan's "malicious" persona description.

`detect_reflected_injection` is fully automatic — wired into both crawl loops, runs
after any fill that used one of core/inputs.py's INJECTION_MARKER_VALUES, no
configuration needed. It checks whether the marker actually got parsed as real DOM
(an unescaped-HTML injection) or actually executed (a real XSS), not just whether the
raw string appears somewhere in a text node — string-matching innerHTML would false-
positive on a page that correctly escaped and displayed the marker as literal text.

`check_cross_role_access` isn't part of the per-action crawl loop, since whether a URL
should require a more privileged role can't be inferred from the page alone. It runs
once at the end of `qaura run --role <name>`, against each entry in the config's
`admin_paths` list (see cli.py's `_check_cross_role`).
"""
from __future__ import annotations

import logging

from playwright.async_api import Page

from qaura.browser.observe import build_page_model
from qaura.reporting.models import Evidence, Finding, ReproStep, Severity

_log = logging.getLogger(__name__)

# Matches core/inputs.py's INJECTION_MARKER_VALUES html_tag_marker / attr_break_marker
_MARKER_TAG = "qaura-marker"
# Matches core/inputs.py's INJECTION_MARKER_VALUES script_tag_marker
_MARKER_GLOBAL = "__qaura_marker"

_CHECK_JS = f"""
() => {{
  return {{
    tagReflected: !!document.querySelector('{_MARKER_TAG}'),
    scriptExecuted: window['{_MARKER_GLOBAL}'] === 1,
  }};
}}
"""

# Heuristics for "this page looks like it's blocking access", used by
# check_cross_role_access. Deliberately conservative (a real login page always has a
# password field; a generic 403 page usually says so) — false negatives here just
# mean a real access-control gap goes unreported, which is why this is a persona-
# invoked check with a human/LLM in the loop, not a silent auto-pass/fail gate.
_LOGIN_INDICATOR_ROLES = {"textbox"}
_LOGIN_INDICATOR_NAME_SUBSTRINGS = ("password", "log in", "login", "sign in")
_BLOCKED_TEXT_MARKERS = ("403", "forbidden", "access denied", "not authorized", "unauthorized")


async def detect_reflected_injection(
    page: Page, url: str, repro_steps: list[ReproStep], persona: str = "heuristic"
) -> list[Finding]:
    # Called after every single action in both crawl loops, so a page mid-navigation
    # when this fires ("Execution context was destroyed") used to propagate all the
    # way out of the crawl loop and end the whole run — unlike detectors/performance.py,
    # which already guards its own page.evaluate() calls the same way this now does.
    try:
        result = await page.evaluate(_CHECK_JS)
    except Exception:
        _log.debug("reflected-injection check failed to evaluate on %s", url, exc_info=True)
        return []
    findings: list[Finding] = []

    if result.get("scriptExecuted"):
        findings.append(Finding(
            title="Injected script executed (reflected XSS)",
            detector="security",
            severity=Severity.CRITICAL,
            persona=persona,
            url=url,
            description=(
                "A script-tag-shaped input value was reflected into the page and actually "
                "EXECUTED — this is a real cross-site-scripting vulnerability, not just "
                "unescaped output. Any user input reaching this field is at risk."
            ),
            repro_steps=list(repro_steps),
            evidence=Evidence(),
        ))
    elif result.get("tagReflected"):
        findings.append(Finding(
            title="Unescaped HTML injection reflected in DOM",
            detector="security",
            severity=Severity.HIGH,
            persona=persona,
            url=url,
            description=(
                "An HTML-tag-shaped input value was parsed as a real DOM element instead of "
                "being escaped and shown as literal text — the page is not escaping user "
                "input before rendering it, which is an injection risk even if this "
                "particular marker was inert."
            ),
            repro_steps=list(repro_steps),
            evidence=Evidence(),
        ))

    return findings


async def check_cross_role_access(
    page: Page, url: str, role_name: str, repro_steps: list[ReproStep], persona: str = "malicious"
) -> Finding | None:
    """Call this after navigating to a URL under a session for `role_name` that is
    NOT expected to have access to it. Returns a Finding if the page shows no sign of
    blocking access — heuristic, see module docstring on why this isn't automatic."""
    model = await build_page_model(page)

    looks_like_login = any(
        el.role in _LOGIN_INDICATOR_ROLES
        and any(s in (el.name or "").lower() for s in _LOGIN_INDICATOR_NAME_SUBSTRINGS)
        for el in model.elements
    )
    try:
        body_text = (await page.locator("body").text_content() or "").lower()
    except Exception:
        _log.debug("cross-role body text_content() failed on %s", url, exc_info=True)
        body_text = ""
    looks_blocked = any(marker in body_text for marker in _BLOCKED_TEXT_MARKERS)

    if looks_like_login or looks_blocked:
        return None  # access appears to have been denied — not a finding

    return Finding(
        title=f"Possible cross-role access: '{role_name}' session reached {url}",
        detector="security",
        severity=Severity.HIGH,
        persona=persona,
        url=url,
        description=(
            f"A session authenticated as role '{role_name}' loaded {url} without any "
            f"visible sign of being blocked (no login prompt, no 403/forbidden text). "
            f"Verify manually whether this page should require a different role — this "
            f"check is heuristic and can false-positive on pages that legitimately allow "
            f"this role, or false-negative on access controls that don't show an obvious "
            f"block state."
        ),
        repro_steps=list(repro_steps),
        evidence=Evidence(),
    )
