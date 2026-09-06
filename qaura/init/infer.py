"""Turns a ReconResult into a proposed config, one rule per field.

Each rule carries a confidence, and confidence is not decoration: anything LOW is emitted
commented out. That matters most for admin_paths, where a wrong entry makes
check_cross_role_access() report a HIGH-severity access-control finding on every future
run against a route that was never privileged in the first place. A guess that has to be
uncommented before it can do that is a suggestion; a guess written live is a bug.

Where a rule had to choose between over- and under-inferring, it under-infers. A config
that is too narrow produces a run that explores less than it could, which is visible in
the coverage report. A config that is too broad produces false findings, which costs
someone an afternoon.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from qaura.config import QAuraConfig
from qaura.core.forms import SUBMIT_NAME_HINTS, group_forms
from qaura.init.candidates import CandidateVerdict
from qaura.init.consent import is_remote_target
from qaura.init.emit import Annotated, ConfidenceLevel, ConfigProposal, HeaderInfo
from qaura.init.login import ADMIN_ROLE_NAMES
from qaura.init.recon import ReconResult

# Always blocked, whether or not recon saw them. Costs nothing when the route doesn't
# exist, and matches the reasoning already in GuardrailConfig.destructive_patterns:
# clicking sign-out discards the authenticated session and strands the rest of the run
# on the login wall.
ALWAYS_BLOCKED = ["/logout*", "/log-out*", "/signout*", "/sign-out*"]

_DESTRUCTIVE_PATH_RE = re.compile(
    r"(delete|destroy|remove|cancel|billing|payment|checkout|unsubscribe|export|purge)",
    re.IGNORECASE,
)
_ADMIN_PREFIX_RE = re.compile(
    r"^/(admin|administrator|manage|management|staff|internal|backoffice|console"
    r"|settings/users|users/admin)(/|$)",
    re.IGNORECASE,
)


def _clamp(value: float, low: int, high: int) -> int:
    return int(max(low, min(high, round(value))))


def _registrable(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else host


@dataclass
class PersonaDecision:
    name: str
    enabled: bool
    reason: str


@dataclass
class Inference:
    """Everything build_proposal() derived, kept separately from the rendered output so
    the rules can be tested without going through the emitter."""

    target_url: str
    allowed_domains: list[str]
    blocked_paths: list[str]
    blocked_paths_low: list[str]
    admin_paths: list[str]
    admin_paths_low: list[str]
    admin_measured: bool
    max_rps: float
    max_actions: int
    max_wall_clock: int
    max_llm_calls: int
    personas: list[PersonaDecision]
    auth_roles: list[dict]
    repo_path: str | None
    notes: list[str] = field(default_factory=list)


def infer_allowed_domains(result: ReconResult) -> tuple[list[str], str]:
    host = urlsplit(result.final_url).hostname or urlsplit(result.seed_url).hostname or "localhost"
    extra = {h for h in result.subdomains_visited if h != host}
    if extra and all(_registrable(h) == _registrable(host) for h in extra):
        domain = _registrable(host)
        return [domain], (
            f"Recon started on {host} and also visited {', '.join(sorted(extra))}, so this "
            f"widens to the registrable domain {domain}."
        )
    return [host], f"Hostname of the final URL after redirects ({host})."


def infer_blocked_paths(result: ReconResult) -> tuple[list[str], list[str]]:
    blocked = list(ALWAYS_BLOCKED)
    for path in sorted(set(result.discovered_paths)):
        if _DESTRUCTIVE_PATH_RE.search(path) and path not in blocked:
            blocked.append(f"{path.rstrip('/')}*")
    low = [
        f"{path}*" for path, status in sorted(result.statuses.items())
        if isinstance(status, int) and status >= 500
    ]
    return blocked, low


def infer_admin_paths(result: ReconResult) -> tuple[list[str], list[str], bool]:
    if result.admin_measured:
        return sorted(result.admin_measured), [], True
    guessed = sorted({
        path for path in result.discovered_paths if _ADMIN_PREFIX_RE.match(path)
    })
    return [], guessed, False


def infer_rate(result: ReconResult) -> tuple[float, str]:
    if not is_remote_target(result.final_url):
        return 5.0, "Local target, so the default rate applies."
    if result.saw_429 or result.rate_limit_headers:
        return 1.0, "Remote target that returned rate-limit signals during recon."
    if result.median_latency_ms and result.median_latency_ms < 800:
        return 2.0, (
            f"Remote target, no rate-limit signals, median navigation "
            f"{result.median_latency_ms:.0f}ms."
        )
    return 1.0, "Remote target with slow or unmeasured responses; erring conservative."


def infer_budgets(result: ReconResult, rps: float) -> tuple[int, int, int]:
    states = max(len(result.states), 1)
    max_actions = _clamp(25 * states, 100, 500)
    max_wall_clock = _clamp(3 * max_actions / max(rps, 0.1), 600, 3600)
    max_llm_calls = _clamp(max_actions + 50, 100, 500)
    return max_actions, max_wall_clock, max_llm_calls


def infer_personas(result: ReconResult) -> list[PersonaDecision]:
    """Every persona is decided explicitly and the disabled ones keep their reason, so a
    generated config never silently drops one. A user who disagrees can see what the
    evidence was and uncomment it."""
    all_elements = [e for state in result.states for e in state.model.elements]
    text_inputs = [e for e in all_elements if e.role in ("textbox", "searchbox")]
    form_groups = [g for state in result.states for g in group_forms(state.model)]
    has_submit = any(
        any(hint in (e.name or "").lower() for hint in SUBMIT_NAME_HINTS) for e in all_elements
    )
    has_query = any("?" in url for url in result.statuses)
    has_auth = result.login.walled or bool(result.role)
    pagination = any(
        (e.name or "").strip().lower() in {"next", "previous", "prev", "2", "3"}
        for e in all_elements
    )
    # A listing page is the thing power_user is for, and the honest proxy available here
    # is link density: PageModel only carries interactive elements, so there is no row
    # count to read. Total element count would be the tempting stand-in and it is wrong —
    # a dozen buttons on a settings page is not a data table.
    max_links = max(
        (sum(1 for e in state.model.elements if e.role == "link") for state in result.states),
        default=0,
    )

    decisions = [
        PersonaDecision("curious", True, "Always useful; explores whatever is reachable."),
        PersonaDecision("accessibility", True, "Always useful; the a11y detector needs no site features."),
        PersonaDecision(
            "malicious", bool(text_inputs) or has_auth or has_query,
            f"{len(text_inputs)} text input(s), auth={has_auth}, query params={has_query}.",
        ),
        PersonaDecision(
            "impatient", has_submit,
            "A submit-shaped button was found." if has_submit
            else "No submit-shaped button found; nothing to double-submit.",
        ),
        PersonaDecision(
            "power_user", len(form_groups) >= 2 or max_links >= 15 or pagination,
            f"{len(form_groups)} form group(s), {max_links} link(s) on the densest page, "
            f"pagination={pagination}.",
        ),
    ]
    return decisions


def infer_repo_path(result: ReconResult, cwd: Path) -> str | None:
    if is_remote_target(result.final_url):
        return None
    return str(cwd) if (cwd / ".git").exists() else None


def recon_digest(result: ReconResult) -> str:
    """A hash over what recon actually saw. Two runs with the same digest observed the
    same site, so any difference between their configs came from the LLM rather than from
    the crawl drifting."""
    material = "|".join(sorted(
        f"{state.template}#{state.key}" for state in result.states
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def build_inference(result: ReconResult, cwd: Path) -> Inference:
    domains, _ = infer_allowed_domains(result)
    blocked, blocked_low = infer_blocked_paths(result)
    admin, admin_low, measured = infer_admin_paths(result)
    rps, _ = infer_rate(result)
    max_actions, max_wall_clock, max_llm_calls = infer_budgets(result, rps)

    roles: list[dict] = []
    if result.role:
        roles.append({"name": result.role, "storage_state_path": f".qaura/auth/{result.role}.json",
                      "is_admin": result.role.lower() in ADMIN_ROLE_NAMES})
    elif result.login.walled:
        suggested = result.login.suggested_role
        roles.append({"name": suggested, "storage_state_path": f".qaura/auth/{suggested}.json"})

    return Inference(
        target_url=result.final_url,
        allowed_domains=domains,
        blocked_paths=blocked,
        blocked_paths_low=blocked_low,
        admin_paths=admin,
        admin_paths_low=admin_low,
        admin_measured=measured,
        max_rps=rps,
        max_actions=max_actions,
        max_wall_clock=max_wall_clock,
        max_llm_calls=max_llm_calls,
        personas=infer_personas(result),
        auth_roles=roles,
        repo_path=infer_repo_path(result, cwd),
    )


def _existing_config_note(cwd: Path) -> str | None:
    path = cwd / "qaura.yaml"
    if not path.exists():
        return None
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return "You already have a qaura.yaml. This file is a proposal, not a merge."
    invariants = len(data.get("invariants") or [])
    blocked = len((data.get("guardrails") or {}).get("blocked_paths") or [])
    return (
        f"You already have a qaura.yaml ({invariants} invariant(s), {blocked} blocked path(s)). "
        f"This file is a proposal, not a merge — nothing here has been applied to it."
    )


def build_proposal(
    result: ReconResult,
    verdicts: list[CandidateVerdict],
    *,
    command: str,
    version: str,
    authorization: str,
    cwd: Path | None = None,
    llm_used: bool = True,
) -> ConfigProposal:
    cwd = cwd or Path.cwd()
    inf = build_inference(result, cwd)
    _, domain_reason = infer_allowed_domains(result)
    _, rate_reason = infer_rate(result)

    fields: list[Annotated] = [
        Annotated(
            key="target_url", value=inf.target_url, confidence=ConfidenceLevel.HIGH,
            comment="The seed URL after following redirects.",
        ),
    ]

    if inf.repo_path:
        fields.append(Annotated(
            key="repo_path", value=inf.repo_path, confidence=ConfidenceLevel.LOW,
            comment=(
                "The current directory is a git repo and the target is local, so this MIGHT be "
                "the app's source. A website cannot actually tell us where its code lives — "
                "check before uncommenting. Used only for bug localization."
            ),
        ))
    else:
        fields.append(Annotated(
            key="repo_path", value=None, confidence=ConfidenceLevel.HIGH,
            comment="Cannot be derived from a website. Set it by hand to enable bug localization.",
        ))

    guardrails: dict = {
        "allowed_domains": inf.allowed_domains,
        "allowed_paths": ["/**"],
        "blocked_paths": inf.blocked_paths,
        "allow_destructive": False,
        "max_actions_per_run": inf.max_actions,
        "max_requests_per_second": inf.max_rps,
        "max_wall_clock_seconds": inf.max_wall_clock,
        "max_llm_calls_per_run": inf.max_llm_calls,
    }

    guardrail_comments = {
        "allowed_domains": (
            f"{domain_reason} Only navigation targets count; third-party origins seen as "
            f"subresources are excluded. NOTE: is_in_scope() also permits any SUBDOMAIN of "
            f"anything listed here, which is broader than it looks."
        ),
        "allowed_paths": "Left open. Recon cannot prove a path is out of scope, only that it "
                         "never saw it.",
        "blocked_paths": (
            "Logout routes are always blocked, observed or not — clicking sign-out during a run "
            "discards the session and strands everything after it on the login wall. The rest "
            "were discovered during recon and matched a destructive-looking name."
        ),
        "allow_destructive": "Never inferred. Turn this on deliberately or not at all.",
        "max_actions_per_run": (
            f"25 actions per distinct state, clamped to [100, 500]. Recon reached "
            f"{len(result.states)} state(s) at depth {result.max_depth}, so treat this as a "
            f"floor rather than a measurement."
        ),
        "max_requests_per_second": rate_reason,
        "max_wall_clock_seconds": "Roughly 3s per action at the configured rate, clamped to "
                                  "[600, 3600].",
        "max_llm_calls_per_run": "One planner call per action plus headroom for triage and fix, "
                                 "clamped to [100, 500].",
    }

    fields.append(Annotated(
        key="guardrails", value=guardrails, confidence=ConfidenceLevel.HIGH,
        comment="Scope and budget. Enforced in code by core/guardrails.py, not by prompt text.",
        subcomments=guardrail_comments,
    ))

    if inf.blocked_paths_low:
        fields.append(Annotated(
            key="_blocked_paths_5xx", value={"blocked_paths_5xx": inf.blocked_paths_low},
            confidence=ConfidenceLevel.LOW,
            comment=(
                "These paths returned 5xx during recon. Deliberately NOT added to blocked_paths: "
                "a 500 is a bug worth finding, not a boundary worth respecting. Merge them in "
                "only if they are known-broken and you want them skipped."
            ),
        ))

    enabled = [p.name for p in inf.personas if p.enabled]
    persona_comment_lines = [f"{p.name}: {p.reason}" for p in inf.personas]
    disabled = [p for p in inf.personas if not p.enabled]
    if disabled:
        persona_comment_lines.append(
            "Disabled above are commented out below rather than dropped — re-add any of them "
            "if the evidence was wrong."
        )
    fields.append(Annotated(
        key="personas", value={"enabled": enabled}, confidence=ConfidenceLevel.MEDIUM,
        comment="\n".join(persona_comment_lines),
        trailing_comment=(
            "  # disabled: " + ", ".join(p.name for p in disabled) if disabled else None
        ),
    ))

    if inf.admin_paths:
        fields.append(Annotated(
            key="admin_paths", value=inf.admin_paths, confidence=ConfidenceLevel.HIGH,
            comment=(
                "MEASURED: each of these rendered under the authenticated session but was "
                "refused anonymously. "
                + "; ".join(f"{p}: {why}" for p, why in sorted(result.admin_measured.items()))
            ),
        ))
    elif inf.admin_paths_low:
        fields.append(Annotated(
            key="admin_paths", value=inf.admin_paths_low, confidence=ConfidenceLevel.LOW,
            comment=(
                "GUESSED from path names only — nothing was measured. A wrong entry here makes "
                "check_cross_role_access() report a HIGH-severity access-control finding against "
                "a route that was never privileged. To measure instead of guess, capture an admin "
                "session and re-run with --role admin."
            ),
        ))
    else:
        fields.append(Annotated(
            key="admin_paths", value=[], confidence=ConfidenceLevel.HIGH,
            comment=(
                "Empty. Recon found no privileged-looking routes, and QAura cannot infer which "
                "routes are privileged from a page alone. Run with --role admin to measure."
            ),
        ))

    if inf.auth_roles:
        fields.append(Annotated(
            key="auth_roles", value=inf.auth_roles,
            confidence=ConfidenceLevel.HIGH if result.role else ConfidenceLevel.MEDIUM,
            comment=(
                f"Recon ran as role {result.role!r}." if result.role else
                "A login wall was detected, so a session will be needed. Capture one with "
                "`qaura auth capture --url <login-url> --role user` before running."
            ),
        ))
    else:
        fields.append(Annotated(
            key="auth_roles", value=[], confidence=ConfidenceLevel.HIGH,
            comment="No login wall detected; anonymous runs should reach the whole app.",
        ))

    notes: list[str] = []
    existing = _existing_config_note(cwd)
    if existing:
        notes.append(existing)
    if result.truncated_reason:
        notes.append(f"Recon stopped early: {result.truncated_reason}.")
    if result.assisted_login:
        notes.append(f"Assisted login: {result.assisted_login}. Credentials are not recorded here.")
    if result.robots_disallowed:
        notes.append(
            f"robots.txt disallowed {len(result.robots_disallowed)} path pattern(s); those were "
            f"not crawled."
        )
    if result.route_sources:
        notes.append("Routes found via " + ", ".join(
            f"{source} ({count})" for source, count in sorted(result.route_sources.items())
        ) + ".")
    if not llm_used:
        notes.append("Invariant synthesis was skipped (--no-llm or no credential), so `invariants` "
                     "is empty. Everything else is unaffected.")

    header = HeaderInfo(
        command=command,
        version=version,
        pages_visited=result.attempted,
        distinct_states=len(result.states),
        blocked_requests=len(result.ledger.aborted),
        allowed_requests=result.ledger.allowed,
        login_verdict=result.login.kind,
        authorization=authorization,
        recon_digest=recon_digest(result),
        llm_used=llm_used,
        notes=notes,
    )

    return ConfigProposal(header=header, fields=fields, verdicts=verdicts)


def validate_proposal_loads(rendered: str) -> QAuraConfig:
    """Re-parse and re-construct, so an emitter bug cannot ship a file load_config()
    would later reject."""
    import yaml

    data = yaml.safe_load(rendered) or {}
    if not isinstance(data, dict):
        raise ValueError("rendered config is not a YAML mapping")
    return QAuraConfig(**data)
