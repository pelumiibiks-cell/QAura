"""QAura CLI — every command below is fully wired: exploratory web testing (observe/
auth/run/replay/report) and ML/GenAI testing (ml test/probe/genai). Run `qaura doctor`
first if you're using an LLM persona/triage/fix path — it validates the Gemini
credential and reachable models before anything spends real API calls."""
from __future__ import annotations

import logging

import typer
from rich.console import Console
from rich.logging import RichHandler

from qaura import __version__
from qaura.config import ConfigError, load_config

app = typer.Typer(
    name="qaura",
    help="Autonomous QA testing agent. See the README for usage.",
    no_args_is_help=True,
)
console = Console()
_log = logging.getLogger(__name__)


def _load_config_or_exit(config_path: str | None):
    """load_config() raises ConfigError on a malformed qaura.yaml (bad YAML, or a
    value that doesn't fit the schema) — every command that loads config wants the
    same clean "here's what's wrong" message and exit code, not a raw traceback."""
    try:
        return load_config(config_path)
    except ConfigError as e:
        console.print(f"[red]Config error:[/red] {e}")
        raise typer.Exit(code=1)


_CI_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def _evaluate_ci_gate(findings, fail_on: str) -> tuple[bool, list]:
    """Returns (passed, offending_findings). Raises ValueError if `fail_on` isn't a
    known severity name. Pulled out of `run()`'s body as a plain function so the
    severity-ranking logic that `--ci --fail-on` actually gates on — what users wire
    into their own CI pipelines — has a direct unit test rather than only being
    reachable through a full crawl."""
    threshold = _CI_SEVERITY_RANK.get(fail_on.lower())
    if threshold is None:
        raise ValueError(f"--fail-on must be one of {sorted(_CI_SEVERITY_RANK)}, got {fail_on!r}")
    offenders = [f for f in findings if _CI_SEVERITY_RANK.get(f.severity.value, 0) >= threshold]
    return not offenders, offenders


def _load_report_or_exit(path):
    """RunReport.load_json() has no schema-drift tolerance beyond
    reproducibility_status — a hand-edited or corrupted report.json raises a raw
    JSONDecodeError/TypeError/KeyError. `qaura replay`/`qaura report` both take a
    user-supplied path to one, so both want the same clean failure message."""
    from qaura.reporting.models import RunReport

    try:
        return RunReport.load_json(path)
    except Exception as e:
        console.print(f"[red]Could not load {path} as a QAura report:[/red] {e}")
        raise typer.Exit(code=1)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"qaura {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="Show debug-level diagnostics — including the guardrail/detector/network "
        "errors that are normally handled silently (a failed locator enrichment, a "
        "dropped network entry, a detector that couldn't run) — on stderr.",
    ),
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show the installed version and exit.",
    ),
) -> None:
    # A library module (browser/, core/, detectors/, analysis/, llm/) should never
    # print directly — only log. This is the one place that configures a handler, so
    # every module's logger.getLogger(__name__) actually reaches somewhere instead of
    # falling through to logging's silent lastResort handler.
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=verbose, rich_tracebacks=True, markup=False)],
    )


@app.command()
def doctor(
    config_path: str = typer.Option(None, "--config", "-c", help="Path to qaura.yaml"),
) -> None:
    """Validate the Gemini credential, list reachable models, and confirm the API call
    shape this build relies on. Run this first — everything else assumes it passed."""
    from qaura.llm.gemini import run_doctor

    cfg = _load_config_or_exit(config_path)
    run_doctor(cfg, console)


@app.command()
def observe(
    url: str = typer.Argument(..., help="URL to load and describe"),
    role: str = typer.Option(None, "--role", help="Replay a captured auth session by role"),
    headless: bool = typer.Option(True, "--headless/--headed"),
) -> None:
    """Print the distilled PageModel for a URL — the accessibility-tree-derived view
    the planner will act on, not raw HTML."""
    import asyncio

    from qaura.browser.auth import resolve_role
    from qaura.browser.driver import ContextSpec, Driver
    from qaura.browser.observe import build_page_model

    async def _run() -> None:
        storage_state = resolve_role(role) if role else None
        if role and not storage_state:
            console.print(f"[yellow]No captured session for role '{role}' — continuing unauthenticated.[/yellow]")

        async with Driver(headless=headless) as driver:
            spec = ContextSpec(persona="observe", role=role, storage_state_path=storage_state)
            async with driver.context(spec) as (context, page):
                await page.goto(url)
                model = await build_page_model(page)
                console.print(model.to_prompt())
                console.print(f"\n[dim]signature: {model.signature()}  elements: {len(model.elements)}[/dim]")

    asyncio.run(_run())


@app.command("init")
def init_config(
    url: str = typer.Option(..., "--url", help="Seed URL to analyze"),
    role: str = typer.Option(None, "--role", help="Analyze through a captured auth session"),
    out: str = typer.Option("qaura.generated.yaml", "--out", help="Where to write the proposal"),
    max_pages: int = typer.Option(25, "--max-pages", help="Hard cap on pages visited"),
    max_depth: int = typer.Option(2, "--max-depth", help="Link depth from the seed URL"),
    no_llm: bool = typer.Option(False, "--no-llm",
        help="Skip invariant synthesis. Guardrails, personas and admin paths are still inferred."),
    interact_safe: bool = typer.Option(False, "--interact-safe",
        help="Also click tabs and disclosure toggles to reveal hidden content. Never submits a form."),
    login_form: bool = typer.Option(False, "--login-form",
        help="On a login wall, try QAURA_LOGIN_USER/QAURA_LOGIN_PASS. Plain forms only — use "
             "`qaura auth capture` for MFA/SSO. Permits exactly one POST."),
    check: bool = typer.Option(False, "--check",
        help="Don't generate. Report whether --config's invariant selectors still match the site."),
    ignore_robots: bool = typer.Option(False, "--ignore-robots",
        help="Crawl paths robots.txt disallows. For infrastructure you own."),
    yes: bool = typer.Option(False, "--yes", help="Skip the interactive authorization prompt"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing --out file"),
    headless: bool = typer.Option(True, "--headless/--headed"),
    config_path: str = typer.Option(None, "--config", "-c",
        help="Existing config, read for the Gemini credential and model tiers. Never written to."),
) -> None:
    """Analyze a website and propose a qaura.yaml for it.

    Runs a bounded, read-only crawl (GET and HEAD only, no form submissions), infers
    guardrails, personas and admin paths from what it sees, synthesizes business-rule
    invariants and validates them against captured page snapshots, then writes an
    annotated proposal. Every value says how it was inferred and how much to trust it.

    Never writes qaura.yaml. Review the output and adopt it yourself.
    """
    import asyncio
    from pathlib import Path

    from rich.markup import escape
    from rich.table import Table

    from qaura.browser.auth import DEFAULT_AUTH_DIR, resolve_role
    from qaura.init import emit, infer
    from qaura.init.candidates import annotate_durability, build_inventory, generate, sanity_filter
    from qaura.init.consent import ConsentDeclined, is_remote_target, require_authorization
    from qaura.init.limits import ReconLimits, recon_guardrails
    from qaura.init.login import ADMIN_ROLE_NAMES, guidance
    from qaura.init.recon import ChallengeDetected, probe_anon_differential, run_recon

    cfg = _load_config_or_exit(config_path)
    remote = is_remote_target(url)
    recon_cfg = recon_guardrails(url, remote=remote)
    limits = ReconLimits().with_overrides(
        max_pages=max_pages, max_depth=max_depth,
        interact_safe=interact_safe, respect_robots=not ignore_robots,
    )

    try:
        authorization = require_authorization(
            url, console, max_pages=max_pages,
            rps=recon_cfg.max_requests_per_second, assume_yes=yes, login_form=login_form,
        )
    except ConsentDeclined as e:
        console.print(f"[yellow]{e}[/yellow]")
        raise typer.Exit(code=2)

    if check:
        from qaura.init.check import check_invariants

        if not cfg.invariants:
            console.print("No invariants in the loaded config — nothing to check.")
            raise typer.Exit(code=0)
        results = asyncio.run(check_invariants(
            cfg, url, storage_state=resolve_role(role) if role else None, headless=headless,
        ))
        table = Table(title=f"Invariant selector health — {url}")
        table.add_column("invariant")
        table.add_column("status")
        table.add_column("detail")
        for item in results:
            colour = {"holds": "green", "violated": "red"}.get(item.status, "yellow")
            # escape(): a CSS selector is mostly square brackets, which rich parses as
            # console markup and silently swallows — so an unescaped detail line drops
            # the exact selector the reader needs to see.
            table.add_row(escape(item.name), f"[{colour}]{item.status}[/{colour}]",
                          escape(item.detail))
        console.print(table)
        unhealthy = [r for r in results if not r.healthy]
        if unhealthy:
            console.print(f"[yellow]{len(unhealthy)} of {len(results)} invariant(s) did not hold "
                          f"cleanly on this page.[/yellow]")
            raise typer.Exit(code=1)
        console.print(f"[green]All {len(results)} invariant(s) still apply.[/green]")
        raise typer.Exit(code=0)

    out_path = Path(out)
    if out_path.exists() and not force:
        console.print(f"[red]{out_path} already exists.[/red] Pass --force to overwrite it.")
        raise typer.Exit(code=1)

    storage_state = None
    if role:
        storage_state = resolve_role(role)
        # With --login-form, `--role` names the session to CREATE, not one that must
        # already exist — requiring it up front would make the two flags contradict each
        # other and leave no way to say "log in and save it as this role".
        if not storage_state and not login_form:
            # Otherwise deliberately harsher than `observe`/`run`, which warn and
            # continue anonymously. Silently emitting an anonymous-only config under
            # --role admin would look authoritative while describing the logged-out shell.
            console.print(f"[red]No captured session for role '{role}'.[/red]")
            console.print(f"  qaura auth capture --url {url} --role {role}")
            console.print(f"  ...or, for a plain username/password form:")
            console.print(f"  QAURA_LOGIN_USER=... QAURA_LOGIN_PASS=... "
                          f"qaura init --url {url} --role {role} --login-form")
            raise typer.Exit(code=1)

    console.print(f"Analyzing [bold]{url}[/bold] — read-only, at most {max_pages} pages.")
    try:
        result = asyncio.run(run_recon(
            url, recon_cfg, limits, storage_state=storage_state, role=role,
            headless=headless, console=console, login_form=login_form,
            auth_dir=DEFAULT_AUTH_DIR,
        ))
    except ChallengeDetected as e:
        console.print(f"[red]Bot protection detected:[/red] {e.signal.describe()}")
        console.print("Recon stopped. A config derived from a challenge page would describe the "
                      "challenge, not the app. No file was written.")
        raise typer.Exit(code=3)

    console.print(
        f"  {result.attempted} navigation(s), {len(result.states)} distinct state(s), "
        f"{result.ledger.summary()}."
    )

    if result.login.walled and not role:
        for line in guidance(result.login, url, role, len(result.states)):
            console.print(f"[yellow]{escape(line)}[/yellow]" if line else "")

    if role and role.lower() in ADMIN_ROLE_NAMES and result.discovered_paths:
        console.print("  Measuring which paths are actually privileged...")
        result.admin_measured = asyncio.run(probe_anon_differential(
            sorted(set(result.discovered_paths)), url, recon_cfg, limits, headless=headless,
        ))

    verdicts = []
    llm_used = False
    if not no_llm and cfg.effective_llm_mode == "gemini":
        from qaura.llm.budget import Budget
        from qaura.llm.gemini import GeminiProvider

        entries = build_inventory(result.states)
        if entries:
            llm_used = True
            console.print(f"  {len(entries)} addressable number(s) found; proposing invariants...")
            provider = GeminiProvider(cfg.gemini_api_key, cfg.model_tiers)
            raw = generate(provider, entries, Budget(max_calls=limits.max_llm_calls))
            accepted, rejected = [], []
            for candidate in raw:
                outcome = sanity_filter(candidate, entries)
                (accepted if hasattr(outcome, "expression") else rejected).append(outcome)
            for bad in rejected:
                _log.debug("candidate %r rejected: %s", bad.candidate.name, bad.reason)
            if accepted:
                verdicts = asyncio.run(_validate_candidates(accepted, result.snapshots, headless))
                annotate_durability(verdicts, entries)
        else:
            console.print("  No addressable numbers found; skipping invariant synthesis.")
    elif not no_llm:
        console.print("  No Gemini credential; skipping invariant synthesis (--no-llm equivalent).")

    proposal = infer.build_proposal(
        result, verdicts,
        command=f"qaura init --url {url}" + (f" --role {role}" if role else ""),
        version=__version__, authorization=authorization, llm_used=llm_used,
    )
    rendered = emit.render_config(proposal)
    try:
        emit.assert_loadable(rendered)
    except Exception as e:
        console.print(f"[red]Generated config failed its own validation:[/red] {e}")
        console.print("This is a bug in qaura init. No file was written.")
        raise typer.Exit(code=4)

    emit.write_generated(rendered, out_path, force=force)

    table = Table(title="Proposed configuration")
    table.add_column("field")
    table.add_column("value", overflow="fold")
    table.add_column("confidence")
    for annotated in proposal.fields:
        value = annotated.value
        rendered_value = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
        if isinstance(value, dict):
            rendered_value = ", ".join(f"{k}={v}" for k, v in list(value.items())[:4])
        suffix = " (commented out)" if annotated.commented_out else ""
        table.add_row(annotated.key, escape(rendered_value[:100]) + suffix,
                      annotated.confidence.value)
    console.print(table)

    accepted_v = [v for v in verdicts if v.status == "accepted"]
    console.print(
        f"Invariants: {len(accepted_v)} accepted, "
        f"{sum(1 for v in verdicts if v.status == 'rejected_violated')} rejected, "
        f"{sum(1 for v in verdicts if v.status == 'unverified')} unverified."
    )
    console.print(f"[green]Wrote {out_path}[/green] — review it, then: "
                  f"qaura run --config {out_path}")


async def _validate_candidates(invariants, snapshots, headless: bool):
    """Replays candidates against captured HTML in a context where every request is
    aborted, so set_content() cannot re-fetch a single asset from the target."""
    from qaura.browser.driver import ContextSpec, Driver
    from qaura.browser.readonly import install_offline_routes
    from qaura.init.candidates import validate

    async with Driver(headless=headless) as driver:
        async with driver.context(ContextSpec(persona="init-validate")) as (context, page):
            await install_offline_routes(context)
            return await validate(page, invariants, snapshots)


auth_app = typer.Typer(help="Capture and manage storage-state auth sessions.")
app.add_typer(auth_app, name="auth")


@auth_app.command("capture")
def auth_capture(
    url: str = typer.Option(..., "--url"),
    role: str = typer.Option("user", "--role"),
) -> None:
    """Open a visible browser, let you log in by hand, save the session for reuse."""
    import asyncio

    from qaura.browser.auth import capture

    def _prompt(ready_url: str, ready_role: str) -> None:
        console.print(f"\nA browser window opened at {ready_url}.")
        console.print(f"Log in as '{ready_role}', then come back here and press Enter to save the session.")
        input()

    path = asyncio.run(capture(url, role, on_ready=_prompt))
    console.print(f"[green]Saved session for role '{role}' to {path}[/green]")


@auth_app.command("list")
def auth_list() -> None:
    """List captured auth sessions."""
    from qaura.browser.auth import list_captured

    roles = list_captured()
    if not roles:
        console.print("No captured sessions yet. Run: qaura auth capture --url <url> --role <role>")
        return
    for r in roles:
        console.print(f"  {r}")


@app.command()
def run(
    url: str = typer.Option(None, "--url"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Force heuristic mode"),
    personas: str = typer.Option(None, "--personas", help="Comma-separated persona list, e.g. curious,impatient"),
    role: str = typer.Option(None, "--role", help="Replay a captured auth session by role (see qaura auth capture)"),
    out: str = typer.Option(None, "--out"),
    config_path: str = typer.Option(None, "--config", "-c"),
    headless: bool = typer.Option(True, "--headless/--headed"),
    replay_attempts: int = typer.Option(2, "--replay-attempts", help="Times to re-run each finding's repro; 0 disables replay"),
    no_analysis: bool = typer.Option(False, "--no-analysis", help="Skip dedupe/triage/replay/localize/fix/repro-script generation"),
    ci: bool = typer.Option(False, "--ci", help="Exit non-zero if any finding meets --fail-on, for use as a CI gate"),
    fail_on: str = typer.Option("high", "--fail-on", help="Minimum severity that triggers CI failure with --ci: critical|high|medium|low"),
    baseline_run: str = typer.Option(None, "--baseline-run", help="Path to a prior run's report.json — flags recurring findings whose screenshot changed meaningfully"),
    trace: bool = typer.Option(False, "--trace", help="Capture a Playwright trace.zip per browser context — slower and larger output, but the single best debugging artifact for one specific run"),
) -> None:
    """Run an exploratory test session against a target app. With `--no-llm` (or no
    Gemini key configured), runs the deterministic heuristic crawler. With
    `--personas`, runs one or more LLM-driven personas, each in its own browser
    context, and merges their findings into one report."""
    import asyncio
    from datetime import datetime, timezone
    from pathlib import Path

    from qaura.browser.driver import ContextSpec, Driver
    from qaura.browser.recorder import Recorder
    from qaura.core.guardrails import RunLimiter
    from qaura.core.heuristic import HeuristicCrawler
    from qaura.core.orchestrator import PersonaOrchestrator
    from qaura.llm.budget import Budget
    from qaura.reporting.html import save_html
    from qaura.reporting.models import Finding, RunReport, RunSummary

    cfg = _load_config_or_exit(config_path)
    target = url or cfg.target_url
    if not target:
        console.print("[red]No target URL given.[/red] Pass --url or set target_url in qaura.yaml.")
        raise typer.Exit(code=1)

    from qaura.browser.auth import resolve_role

    storage_state = resolve_role(role) if role else None
    if role and not storage_state:
        console.print(f"[yellow]No captured session for role '{role}' — continuing unauthenticated.[/yellow]")

    persona_names = [p.strip() for p in personas.split(",")] if personas else []
    use_llm = bool(persona_names) and not no_llm and cfg.effective_llm_mode == "gemini"

    if persona_names and not use_llm:
        reason = "no_llm was set" if no_llm else "no Gemini credential is configured (run `qaura doctor`)"
        console.print(f"[yellow]--personas requested but {reason} — falling back to heuristic mode.[/yellow]")

    out_dir = Path(out or cfg.output_dir) / datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_dir = Path(out_dir) / "screenshots"
    trace_dir = Path(out_dir) / "traces"
    trace_paths: dict[str, str] = {}

    # One RunLimiter and one Budget for the entire run, per guardrails.py's own
    # documented contract ("a run with 5 personas shouldn't get 5x the action
    # budget"). Previously each PersonaOrchestrator built its own, and _analyze built
    # a further independent Budget for triage/fix — with 5 default personas and the
    # shipped 300/500/1800 caps, that meant up to 1,800 LLM calls, 3,000 actions, and
    # 6x the wall-clock ceiling per run. Reused across _run_personas and _analyze
    # below; _run_heuristic doesn't need it since it only ever builds one crawler.
    run_limiter = RunLimiter(cfg=cfg.guardrails)
    run_budget = Budget(max_calls=cfg.guardrails.max_llm_calls_per_run)

    async def _run_heuristic() -> tuple[list[Finding], dict, None]:
        crawler = HeuristicCrawler(target, cfg, screenshot_dir=screenshot_dir)
        async with Driver(headless=headless) as driver:
            spec = ContextSpec(persona="heuristic", role=role, storage_state_path=storage_state)
            async with driver.context(spec) as (context, page):
                recorder = Recorder(page, context)
                await recorder.start(trace=trace)
                with console.status(f"Crawling {target} (heuristic, no LLM)..."):
                    result = await crawler.run(page, recorder)
                if trace:
                    saved = await recorder.stop_and_save_trace(trace_dir / "heuristic.zip")
                    if saved:
                        trace_paths["heuristic"] = str(saved)
        coverage = result.graph.coverage_summary()
        coverage["unreached"] = [f"{role}: {name!r} ({template})" for template, role, name in result.graph.unreached_elements()]
        return result.findings, coverage, None

    async def _run_personas() -> tuple[list[Finding], dict, dict]:
        from qaura import personas as persona_registry
        from qaura.llm.gemini import GeminiProvider

        provider = GeminiProvider(cfg.gemini_api_key, cfg.model_tiers)
        resolved = persona_registry.resolve_all(persona_names)

        all_findings: list[Finding] = []
        merged_coverage = {"states": 0, "edges": 0, "total_elements": 0, "exercised_elements": 0, "unexplored_states": 0}
        merged_unreached: list[str] = []
        merged_usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "by_tier": {}}

        async with Driver(headless=headless) as driver:
            for persona in resolved:
                orchestrator = PersonaOrchestrator(
                    target, cfg, provider, persona, screenshot_dir=screenshot_dir,
                    limiter=run_limiter, budget=run_budget,
                )
                spec = ContextSpec(persona=persona.name, role=role, storage_state_path=storage_state)
                async with driver.context(spec) as (context, page):
                    recorder = Recorder(page, context)
                    await recorder.start(trace=trace)
                    with console.status(f"Running persona '{persona.name}' against {target}..."):
                        result = await orchestrator.run(page, recorder)
                    if trace:
                        saved = await recorder.stop_and_save_trace(trace_dir / f"{persona.name}.zip")
                        if saved:
                            trace_paths[persona.name] = str(saved)

                all_findings.extend(result.findings)
                persona_coverage = result.graph.coverage_summary()
                for k in merged_coverage:
                    merged_coverage[k] += persona_coverage.get(k, 0)
                merged_unreached.extend(
                    f"{role}: {name!r} ({template}) [{persona.name}]"
                    for template, role, name in result.graph.unreached_elements()
                )
                merged_usage["calls"] += result.llm_usage["calls"]
                merged_usage["input_tokens"] += result.llm_usage["input_tokens"]
                merged_usage["output_tokens"] += result.llm_usage["output_tokens"]
                merged_usage["total_tokens"] += result.llm_usage["total_tokens"]
                for tier, count in result.llm_usage["by_tier"].items():
                    merged_usage["by_tier"][tier] = merged_usage["by_tier"].get(tier, 0) + count

        merged_coverage["unreached"] = merged_unreached
        return all_findings, merged_coverage, merged_usage

    async def _check_cross_role() -> list[Finding]:
        """Phase E: wires the previously-implemented-but-never-called
        check_cross_role_access() (detectors/security.py). Opt-in — only runs when
        both a role was given for this run AND cfg.admin_paths is configured — and
        skipped for a role explicitly marked is_admin, since it's expected to reach
        those paths. One extra short-lived browser context, not the main crawl's."""
        if not role or not storage_state or not cfg.admin_paths:
            return []
        current_role_cfg = next((r for r in cfg.auth_roles if r.name == role), None)
        if current_role_cfg is not None and current_role_cfg.is_admin:
            return []

        from qaura.core.guardrails import guard_goto
        from qaura.detectors.security import check_cross_role_access

        findings: list[Finding] = []
        async with Driver(headless=headless) as cr_driver:
            spec = ContextSpec(persona="cross-role", role=role, storage_state_path=storage_state)
            async with cr_driver.context(spec) as (_context, cr_page):
                for path in cfg.admin_paths:
                    admin_url = target.rstrip("/") + "/" + path.lstrip("/") if not path.startswith("http") else path
                    try:
                        # An admin_paths entry given as a full URL used to be passed
                        # to goto() verbatim under this role's live session, with no
                        # scope check at all — guard_goto is what stops an
                        # authenticated cross-role probe from being pointed off-target.
                        await guard_goto(cr_page, admin_url, cfg.guardrails)
                    except Exception:
                        continue
                    finding = await check_cross_role_access(cr_page, admin_url, role, [], persona="cross-role")
                    if finding:
                        findings.append(finding)
        return findings

    async def _run() -> RunReport:
        started_at = datetime.now(timezone.utc).isoformat()
        if use_llm:
            findings, coverage, llm_usage = await _run_personas()
            mode = "gemini"
        else:
            findings, coverage, llm_usage = await _run_heuristic()
            mode = "heuristic"
        findings.extend(await _check_cross_role())
        finished_at = datetime.now(timezone.utc).isoformat()
        summary = RunSummary(
            target_url=target, started_at=started_at, finished_at=finished_at,
            mode=mode, personas=persona_names if use_llm else [], coverage=coverage,
            llm_usage=llm_usage, trace_paths=dict(trace_paths),
        )
        return RunReport(summary=summary, findings=findings)

    async def _analyze(report: RunReport) -> RunReport:
        """Phase 5: dedupe (always, no LLM needed) -> triage + fix suggestions (only
        with an LLM provider available) -> replay (always, unless disabled) ->
        localize (only if repo_path is configured) -> emit repro scripts for
        findings that survive. Order matters: dedupe FIRST so the expensive/LLM
        steps below run once per distinct bug, not once per near-duplicate."""
        from qaura.analysis.dedupe import dedupe
        from qaura.analysis.fix import apply_fix_suggestion
        from qaura.analysis.localize import apply_localization
        from qaura.analysis.replay import replay_finding
        from qaura.analysis.repo_index import build_index
        from qaura.analysis.triage import apply_triage, is_confident_false_positive
        from qaura.llm.budget import BudgetExceeded
        from qaura.reporting.repro import emit_repro_script

        dedupe_result = dedupe(report.findings)
        findings = dedupe_result.findings
        console.print(
            f"Dedupe: {dedupe_result.total_before} -> {dedupe_result.total_after} finding(s) "
            f"({dedupe_result.merged_count} merged)."
        )

        # --no-llm means no LLM calls anywhere this run, not just during crawling —
        # a user avoiding API cost/quota would be surprised to see triage/fix calls
        # fire anyway just because they were only trying to skip the persona crawl.
        provider = None
        if not no_llm and cfg.effective_llm_mode == "gemini":
            from qaura.llm.gemini import GeminiProvider
            provider = GeminiProvider(cfg.gemini_api_key, cfg.model_tiers)
        # Reuse the run-wide budget (shared across personas above) rather than
        # building a fresh one here — see run_budget's definition for why a separate
        # analysis budget used to let a run spend the configured cap twice over.
        analysis_budget = run_budget

        # Localize BEFORE triage (Phase F fix — was the other way around). Triage
        # appends an LLM-written note straight onto finding.description, and
        # localize's term extraction pulls quoted spans out of that same
        # description — running triage first meant LLM-generated prose could leak
        # into localization's search terms and skew which source file it picked.
        index = build_index(cfg.repo_path) if cfg.repo_path else None
        if index is not None:
            for f in findings:
                apply_localization(index, f)

        if provider is not None:
            surviving: list[Finding] = []
            triage_errors = 0
            for f in findings:
                try:
                    analysis_budget.check()
                except BudgetExceeded:
                    surviving.append(f)  # budget exhausted — keep remaining findings untouched
                    continue
                try:
                    verdict = await apply_triage(provider, f, budget=analysis_budget)
                except Exception:
                    # apply_triage/GeminiProvider.complete() had no timeout or retry
                    # for anything other than a 429 — one transient network error used
                    # to be unguarded here, which (before this) meant the outer
                    # try/except around the whole _analyze() call would abort dedupe/
                    # replay/localize/fix/repro-emission for every OTHER finding too,
                    # over a single bad LLM call. Skip triage for just this finding.
                    _log.debug("triage failed for finding %s", f.id, exc_info=True)
                    triage_errors += 1
                    surviving.append(f)
                    continue
                if is_confident_false_positive(verdict):
                    continue  # dropped from the report
                surviving.append(f)
            dropped = len(findings) - len(surviving)
            if dropped:
                console.print(f"Triage: dropped {dropped} confident false-positive(s).")
            if triage_errors:
                console.print(f"[yellow]Triage: {triage_errors} finding(s) skipped due to an LLM call error.[/yellow]")
            findings = surviving

        # Phase D fix: the premise this filter used to run on ("visual/a11y findings
        # have empty repro_steps by design") was false — both crawl loops pass the
        # full action history into the per-state check, so a visual/a11y finding
        # discovered on any state after the landing page DOES have repro_steps, and
        # replay_finding() used to stamp it a misleading "0/N, not confirmed" purely
        # because replay never re-ran the visual/a11y detectors at all — a structural
        # zero, not a real measurement. That's fixed now (see analysis/replay.py's
        # REPLAYABLE_DETECTORS and the gated re-detection in _attempt_once). What's
        # genuinely not replayable is a detector outside that set (flow needs an LLM
        # + an expectation string that isn't persisted; visual_baseline is a
        # cross-run diff) or a finding with no action sequence at all — those get an
        # explicit NOT_APPLICABLE status instead of a fabricated fraction.
        from qaura.analysis.replay import REPLAYABLE_DETECTORS
        from qaura.reporting.models import ReproStatus

        replayable = [f for f in findings if f.repro_steps and f.detector in REPLAYABLE_DETECTORS]
        replayable_ids = {f.id for f in replayable}
        for f in findings:
            if f.id not in replayable_ids:
                f.reproducibility_status = ReproStatus.NOT_APPLICABLE
        if replay_attempts > 0 and replayable:
            async with Driver(headless=headless) as replay_driver:
                with console.status(f"Replaying {len(replayable)} finding(s)..."):
                    for f in replayable:
                        await replay_finding(replay_driver, cfg, f, attempts=replay_attempts, storage_state_path=storage_state)

        if provider is not None:
            for f in findings:
                try:
                    analysis_budget.check()
                except BudgetExceeded:
                    break
                try:
                    await apply_fix_suggestion(provider, f, index=index, budget=analysis_budget)
                except Exception:
                    # Same reasoning as the triage loop above: a transient LLM/network
                    # error here shouldn't cost every OTHER finding its fix suggestion
                    # and repro script — just this one's.
                    _log.debug("fix suggestion failed for finding %s", f.id, exc_info=True)

        repro_dir = Path(out_dir) / "repro"
        emitted = 0
        for f in findings:
            script_path = emit_repro_script(f, repro_dir, invariants=cfg.invariants)
            if script_path is not None:
                f.repro_script_path = str(script_path)
                emitted += 1
        if emitted:
            console.print(f"Emitted {emitted} repro script(s) to {repro_dir}")

        report.findings = findings
        return report

    report = asyncio.run(_run())
    # Save immediately after the crawl, before analysis runs — analysis makes
    # unguarded LLM calls, launches a fresh browser for replay, and walks the
    # filesystem for localization, any of which can raise. Previously report.json
    # was written only after ALL of that succeeded, so one transient Gemini 500
    # discarded a completed, budget-consuming crawl entirely. This save is
    # overwritten below once analysis (or as much of it as completes) finishes.
    report.save_json(Path(out_dir) / "report.json")

    if not no_analysis:
        try:
            report = asyncio.run(_analyze(report))
        except Exception as e:
            console.print(
                f"[red]Analysis step failed: {e}[/red] — keeping the crawl's raw findings; "
                "dedupe/triage/replay/localize/fix/repro-scripts may be incomplete."
            )

    if baseline_run:
        from qaura.analysis.visual_baseline import compare_runs

        baseline_path = Path(baseline_run)
        if baseline_path.is_dir():
            baseline_path = baseline_path / "report.json"
        if not baseline_path.exists():
            console.print(f"[yellow]--baseline-run path {baseline_path} does not exist — skipping visual baseline comparison.[/yellow]")
        else:
            try:
                baseline_report = RunReport.load_json(baseline_path)
                regressions = compare_runs(baseline_report, report)
                if regressions:
                    console.print(f"[yellow]Visual baseline: {len(regressions)} recurring finding(s) with a changed screenshot.[/yellow]")
                report.findings.extend(regressions)
            except Exception as e:
                console.print(f"[yellow]Visual baseline comparison failed: {e} — skipping.[/yellow]")

    json_path = report.save_json(Path(out_dir) / "report.json")
    html_path = save_html(report, Path(out_dir) / "report.html")

    console.print(f"\n[bold]Run complete.[/bold] {len(report.findings)} finding(s).")
    console.print(f"Coverage: {report.summary.coverage}")
    if report.summary.llm_usage:
        console.print(f"LLM usage: {report.summary.llm_usage}")
    if report.summary.trace_paths:
        for name, path in report.summary.trace_paths.items():
            console.print(f"Trace ({name}): {path}")
    console.print(f"JSON: {json_path}")
    console.print(f"HTML: {html_path}")

    if ci:
        try:
            passed, offenders = _evaluate_ci_gate(report.findings, fail_on)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(code=2)
        if not passed:
            console.print(
                f"\n[bold red]CI gate: FAIL[/bold red] — {len(offenders)} finding(s) at or above "
                f"{fail_on} severity."
            )
            raise typer.Exit(code=1)
        console.print(f"\n[bold green]CI gate: PASS[/bold green] — nothing at or above {fail_on} severity.")


@app.command()
def replay(
    run_path: str = typer.Argument(..., help="Path to a run's report.json, or a run directory containing one"),
    finding_id: str = typer.Option(None, "--finding-id", help="Replay only this finding; default replays every finding with repro_steps"),
    attempts: int = typer.Option(3, "--attempts"),
    role: str = typer.Option(None, "--role", help="Replay a captured auth session by role (see qaura auth capture)"),
    headless: bool = typer.Option(True, "--headless/--headed"),
    config_path: str = typer.Option(None, "--config", "-c"),
) -> None:
    """Re-execute one or all findings' repro from a saved run, confirming
    reproducibility. Updates and re-saves report.json/report.html in place."""
    import asyncio
    from pathlib import Path

    from qaura.analysis.replay import REPLAYABLE_DETECTORS, replay_finding
    from qaura.browser.auth import resolve_role
    from qaura.browser.driver import Driver
    from qaura.reporting.html import save_html

    path = Path(run_path)
    if path.is_dir():
        path = path / "report.json"
    if not path.exists():
        console.print(f"[red]{path} does not exist[/red]")
        raise typer.Exit(code=1)

    run_report = _load_report_or_exit(path)
    targets = [
        f for f in run_report.findings
        if f.repro_steps and f.detector in REPLAYABLE_DETECTORS and (finding_id is None or f.id == finding_id)
    ]
    if not targets:
        console.print("[yellow]No matching findings with repro_steps to replay.[/yellow]")
        raise typer.Exit(code=1)

    cfg = _load_config_or_exit(config_path)

    storage_state = resolve_role(role) if role else None
    if role and not storage_state:
        console.print(f"[yellow]No captured session for role '{role}' — continuing unauthenticated.[/yellow]")

    async def _run() -> None:
        async with Driver(headless=headless) as driver:
            with console.status(f"Replaying {len(targets)} finding(s)..."):
                for f in targets:
                    await replay_finding(driver, cfg, f, attempts=attempts, storage_state_path=storage_state)

    asyncio.run(_run())

    for f in targets:
        status = "[green]confirmed[/green]" if f.confirmed else "[yellow]not reproduced[/yellow]"
        console.print(f"  {f.id}  {f.reproducibility}  {status}  {f.title}")

    run_report.save_json(path)
    html_path = save_html(run_report, path.with_suffix(".html"))
    console.print(f"\nUpdated: {path}")
    console.print(f"Updated: {html_path}")


@app.command()
def report(
    run_path: str = typer.Argument(..., help="Path to a run's report.json, or a run directory containing one"),
    out: str = typer.Option(None, "--out", help="Output HTML path; defaults next to the input JSON"),
) -> None:
    """Regenerate the HTML report from an existing run's report.json — useful after
    hand-editing a finding, or if reporting/html.py's template has changed since the
    run happened."""
    from pathlib import Path

    from qaura.reporting.html import save_html

    path = Path(run_path)
    if path.is_dir():
        path = path / "report.json"
    if not path.exists():
        console.print(f"[red]{path} does not exist[/red]")
        raise typer.Exit(code=1)

    run_report = _load_report_or_exit(path)
    out_path = Path(out) if out else path.with_suffix(".html")
    html_path = save_html(run_report, out_path)
    console.print(f"Regenerated: {html_path} ({len(run_report.findings)} finding(s))")


ml_app = typer.Typer(help="Trained-model, endpoint, and GenAI testing.")
app.add_typer(ml_app, name="ml")


@ml_app.command("test")
def ml_test(
    model: str = typer.Option(..., "--model", help="Path to the trained model file"),
    data: str = typer.Option(..., "--data", help="Path to a CSV with feature columns + label column"),
    label_col: str = typer.Option("label", "--label-col"),
    feature_cols: str = typer.Option(None, "--feature-cols", help="Comma-separated; defaults to all columns except label/slice"),
    slice_col: str = typer.Option(None, "--slice-col", help="Column to check subgroup fairness on, e.g. 'group'"),
    baseline: str = typer.Option(None, "--baseline", help="Path to a baseline model to check regression against"),
    out: str = typer.Option(None, "--out"),
) -> None:
    """Run the artifact test suite (metrics, slices, robustness, calibration, fairness,
    regression) against a trained model."""
    from datetime import datetime
    from pathlib import Path

    try:
        import pandas as pd
    except ImportError:
        console.print("[red]`qaura ml test` needs the 'ml' extra:[/red] pip install -e \".[ml]\"")
        raise typer.Exit(code=1)

    from qaura.mltest.registry import ModelLoadError, load_model
    from qaura.mltest.report import build_run_report, overall_gate
    from qaura.mltest.suites.artifact import run_artifact_suite
    from qaura.reporting.html import save_html

    try:
        loaded_model = load_model(model)
        loaded_baseline = load_model(baseline) if baseline else None
    except ModelLoadError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)

    try:
        df = pd.read_csv(data)
    except (FileNotFoundError, pd.errors.ParserError, pd.errors.EmptyDataError) as e:
        console.print(f"[red]Could not read {data} as CSV:[/red] {e}")
        raise typer.Exit(code=1)
    cols = [c.strip() for c in feature_cols.split(",")] if feature_cols else [
        c for c in df.columns if c not in {label_col, slice_col}
    ]

    result = run_artifact_suite(
        loaded_model, df, cols, label_col, slice_col=slice_col, baseline=loaded_baseline,
    )

    report = build_run_report(model, result.findings, mode="ml_artifact", metrics=result.metrics)
    gate = overall_gate(result.findings)
    out_dir = Path(out or "runs") / f"ml_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    json_path = report.save_json(out_dir / "report.json")
    html_path = save_html(report, out_dir / "report.html")

    console.print(f"\n[bold]Gate: {gate}[/bold]  ({len(result.findings)} finding(s))")
    console.print(f"Metrics: {result.metrics}")
    console.print(f"JSON: {json_path}")
    console.print(f"HTML: {html_path}")
    if gate == "FAIL":
        raise typer.Exit(code=1)


@ml_app.command("data")
def ml_data(
    reference: str = typer.Option(..., "--reference", help="Path to a reference CSV (e.g. training data)"),
    current: str = typer.Option(..., "--current", help="Path to a current CSV (e.g. serving/eval data) to compare against reference"),
    columns: str = typer.Option(None, "--columns", help="Comma-separated numeric columns to check for drift/null-rate shift; defaults to columns common to both CSVs"),
    label_col: str = typer.Option(None, "--label-col", help="If given, also runs the leakage check against `current` using this column as the label"),
    feature_cols: str = typer.Option(None, "--feature-cols", help="Comma-separated; only used with --label-col, defaults to all columns except it"),
    out: str = typer.Option(None, "--out"),
) -> None:
    """Run the data/drift suite (schema, feature drift, null-rate shift, and — with
    --label-col — a leakage correlation scan) between a reference and current CSV.
    Implemented in mltest/suites/data.py and fully tested, but had no CLI command
    exposing it until now."""
    from datetime import datetime
    from pathlib import Path

    try:
        import pandas as pd
    except ImportError:
        console.print("[red]`qaura ml data` needs the 'ml' extra:[/red] pip install -e \".[ml]\"")
        raise typer.Exit(code=1)

    from qaura.mltest.report import build_run_report, overall_gate
    from qaura.mltest.suites.data import check_drift, check_leakage, check_null_rate_shift, check_schema
    from qaura.reporting.html import save_html

    try:
        reference_df = pd.read_csv(reference)
        current_df = pd.read_csv(current)
    except (FileNotFoundError, pd.errors.ParserError, pd.errors.EmptyDataError) as e:
        console.print(f"[red]Could not read a CSV:[/red] {e}")
        raise typer.Exit(code=1)

    check_cols = (
        [c.strip() for c in columns.split(",")] if columns
        else sorted(set(reference_df.columns) & set(current_df.columns))
    )

    findings = []
    findings.extend(check_schema(reference_df, current_df, source=reference))
    findings.extend(check_drift(reference_df, current_df, check_cols, source=reference))
    findings.extend(check_null_rate_shift(reference_df, current_df, check_cols, source=reference))
    if label_col:
        leakage_cols = [c.strip() for c in feature_cols.split(",")] if feature_cols else [
            c for c in current_df.columns if c != label_col
        ]
        findings.extend(check_leakage(current_df, leakage_cols, label_col, source=current))

    report = build_run_report(current, findings, mode="ml_data")
    gate = overall_gate(findings)
    out_dir = Path(out or "runs") / f"ml_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    json_path = report.save_json(out_dir / "report.json")
    html_path = save_html(report, out_dir / "report.html")

    console.print(f"\n[bold]Gate: {gate}[/bold]  ({len(findings)} finding(s))")
    console.print(f"JSON: {json_path}")
    console.print(f"HTML: {html_path}")
    if gate == "FAIL":
        raise typer.Exit(code=1)


@ml_app.command("probe")
def ml_probe(
    endpoint: str = typer.Option(..., "--endpoint"),
    payload: str = typer.Option("{}", "--payload", help="JSON of a known-good request payload"),
    method: str = typer.Option("POST", "--method"),
    out: str = typer.Option(None, "--out"),
) -> None:
    """Probe a live inference endpoint with adversarial and malformed payloads."""
    import asyncio
    import json as json_lib
    from datetime import datetime
    from pathlib import Path

    from qaura.mltest.report import build_run_report, overall_gate
    from qaura.mltest.suites.endpoint import probe_endpoint
    from qaura.reporting.html import save_html

    try:
        base_payload = json_lib.loads(payload)
    except json_lib.JSONDecodeError as e:
        console.print(f"[red]--payload is not valid JSON: {e}[/red]")
        raise typer.Exit(code=1)

    with console.status(f"Probing {endpoint}..."):
        result = asyncio.run(probe_endpoint(endpoint, base_payload, method=method))

    report = build_run_report(endpoint, result.findings, mode="ml_endpoint")
    gate = overall_gate(result.findings)
    out_dir = Path(out or "runs") / f"ml_endpoint_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    json_path = report.save_json(out_dir / "report.json")
    html_path = save_html(report, out_dir / "report.html")

    console.print(f"\n[bold]Gate: {gate}[/bold]  ({len(result.findings)} finding(s))")
    console.print(f"Latencies (ms): {[round(l) for l in result.latencies_ms]}")
    console.print(f"JSON: {json_path}")
    console.print(f"HTML: {html_path}")
    if gate == "FAIL":
        raise typer.Exit(code=1)


@ml_app.command("genai")
def ml_genai(
    url: str = typer.Option(..., "--url"),
    input_ref: str = typer.Option(..., "--input-ref", help="PageModel ref of the chat input, e.g. e1 (run `qaura observe` first)"),
    send_ref: str = typer.Option(..., "--send-ref", help="PageModel ref of the send button"),
    response_selector: str = typer.Option(..., "--response-selector", help="CSS selector for the response text"),
    refusal_probe: str = typer.Option(None, "--refusal-probe", help="A request that should be refused, to check consistency"),
    role: str = typer.Option(None, "--role", help="Replay a captured auth session by role (see qaura auth capture) — needed for a GenAI feature behind a login"),
    headless: bool = typer.Option(True, "--headless/--headed"),
    out: str = typer.Option(None, "--out"),
) -> None:
    """Probe a GenAI feature in the target app for injection, jailbreak, PII leakage."""
    import asyncio
    from datetime import datetime
    from pathlib import Path

    from qaura.browser.auth import resolve_role
    from qaura.browser.driver import ContextSpec, Driver
    from qaura.mltest.report import build_run_report, overall_gate
    from qaura.mltest.suites.genai import GenAIProbeConfig, run_genai_suite
    from qaura.reporting.html import save_html

    storage_state = resolve_role(role) if role else None
    if role and not storage_state:
        console.print(f"[yellow]No captured session for role '{role}' — continuing unauthenticated.[/yellow]")

    config = GenAIProbeConfig(input_ref=input_ref, send_ref=send_ref, response_selector=response_selector)

    async def _run() -> list:
        async with Driver(headless=headless) as driver:
            spec = ContextSpec(persona="genai-probe", role=role, storage_state_path=storage_state)
            async with driver.context(spec) as (_, page):
                await page.goto(url)
                with console.status(f"Probing GenAI feature at {url}..."):
                    return await run_genai_suite(page, config, url, refusal_probe=refusal_probe)

    findings = asyncio.run(_run())
    report = build_run_report(url, findings, mode="ml_genai")
    gate = overall_gate(findings)
    out_dir = Path(out or "runs") / f"ml_genai_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    json_path = report.save_json(out_dir / "report.json")
    html_path = save_html(report, out_dir / "report.html")

    console.print(f"\n[bold]Gate: {gate}[/bold]  ({len(findings)} finding(s))")
    console.print(f"JSON: {json_path}")
    console.print(f"HTML: {html_path}")
    if gate == "FAIL":
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
