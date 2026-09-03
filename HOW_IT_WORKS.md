# How QAura works

Quick explainer of the pipeline, the moving parts, and how to read a report. For setup and the command reference, see `README.md`. For the full build history and design rationale, see `docs/PROGRESS.md`.

## The idea

Point QAura at a running web app. It drives a real headless browser through the app, tries the kind of things a human tester would (clicking, filling forms, weird inputs, going back mid-request), and checks after every action whether something broke — a crash, a console error, a failed request, a visual defect, an accessibility violation, or a business-logic rule that got violated silently (like a cart total not updating after a discount). Anything it finds gets deduplicated, re-run a few times to confirm it's real and not a fluke, traced back to a likely source file, and written out as a self-contained HTML report plus a runnable pytest repro script.

A second half of the tool points the same idea at ML: trained model artifacts, live inference endpoints, and chat-shaped AI features.

## The one-run pipeline

```
observe -> decide -> act -> detect   (repeated, one browser session)
        |
        v
  dedupe -> triage -> replay -> localize -> fix -> repro-script
        |
        v
   report.html + report.json
```

**Observe.** Instead of sending raw HTML to anything, `browser/observe.py` builds a `PageModel`: every interactive element with a stable ref (`e12`), its role, accessible name, value, and visibility, taken from Playwright's ARIA snapshot. This keeps each page small and gives the crawler stable handles to act on.

**Decide.** Two modes:
- **Heuristic (`--no-llm`)**: `core/heuristic.py` fills and submits whole forms as a coherent unit (`core/forms.py` groups fields by their `<form>` ancestor) — once with realistic data per field (`valid_value_for`, what actually exercises business logic like creating a record or running a search), then a bounded number of times with one field corrupted and the rest valid (`hostile_value_for`, so a resulting error is attributable to that field). Elements outside any form get the older per-element treatment: nearest never-tried element, text/number/email inputs fuzzed with a corpus of boundary, encoding, injection, and format-mismatch values (`core/inputs.py`). When a state's known elements and forms are exhausted, it navigates to the nearest state the graph knows still has unexercised elements (frontier navigation) rather than stopping — it doesn't give up just because the *current* page is done. Below-the-fold elements get scrolled into view before being acted on. No API calls, fully deterministic given a seed.
- **Persona (Gemini)**: one or more of five personas (`personas/`) reason about what to try next via the Gemini Interactions API, and state an *expectation* for each action — what should be true afterward. If the resulting page diverges from that expectation, that divergence is itself a finding, which is how QAura catches broken flows that throw no error at all. Note: the persona path doesn't yet have the heuristic crawler's form-awareness or frontier navigation — it explores one element at a time and stops when the current page is exhausted.

**Act.** Every action passes through `core/guardrails.py` first: domain/path allowlist, a destructive-action classifier (delete, pay, deactivate, sign out, etc.), payment/email value substitution, and hard caps on actions, requests/sec, wall clock, and LLM calls. This runs in code, not as a prompt instruction, so it can't be talked out of it — confirmed live against a real app: it correctly refused to click "Delete" or "Sign out" mid-crawl while still exercising everything else.

**Detect.** After each action, several detectors run (`detectors/`):
- **crash** — uncaught JS errors, 5xx responses
- **console** — console.error output
- **network** — failed/unexpected requests
- **security** — reflected input (XSS-shaped); cross-role access checking exists (`check_cross_role_access`) and runs automatically at the end of a `--role`'d run against any paths listed in `admin_paths` (opt-in, since QAura can't infer which routes are privileged from the page alone); open redirect is not yet implemented
- **invariants** (`core/invariants.py`) — declarative rules from `qaura.yaml`, e.g. `total == subtotal - discount`; this is what catches the "stale cart total" class of bug that nothing crashes on
- **visual** and **a11y** run once per distinct page state — visual is deterministic geometry/contrast rules first (overflow, off-canvas elements — compared against the document's own bounds, not just the current viewport, so a normal below-the-fold element isn't a false positive — zero-size interactive elements, and contrast ratio), with axe-core for accessibility

**State graph.** `core/state.py` fingerprints each page as a URL template plus a hash of its interactive elements, so `/product/1` and `/product/2` collapse into one state. This is what coverage reporting (`unreached elements`) is based on.

## After the crawl: turning raw findings into a trustworthy report

Raw detector output is noisy, so nothing goes straight into the report:

1. **Dedupe** (`analysis/dedupe.py`) — near-identical findings (same detector, same URL template, same normalized title) merge into one.
2. **Localize** — if `repo_path` is set in `qaura.yaml`, candidate source files are matched by searching the target app's repo for strings tied to the finding. Runs before triage on purpose, so an LLM-written triage note can't leak into localization's search terms.
3. **Triage** (LLM mode only) — an LLM pass on severity and false-positive likelihood.
4. **Replay** (`analysis/replay.py`) — every finding whose detector supports it (crash, console, network, security, invariant, visual, a11y, performance) gets re-executed against a fresh browser (`--replay-attempts`, default 2) and tagged with a real reproducibility status: **confirmed** (reproduced every attempt), **flaky** (some but not all), **not reproduced** (checked, didn't reproduce), or **not applicable** (this finding's detector — e.g. `flow`, which needs an LLM and isn't persisted — genuinely isn't replayable; shown as its own honest badge rather than a fabricated `0/N`).
5. **Fix** (LLM mode only) — a suggested fix per finding.
6. **Repro script** (`reporting/repro.py`) — a runnable pytest file per finding that calls the same replay logic the CLI used, so it's provably not hand-waved: it fails against the buggy app and passes once fixed.

The result is `report.json` (machine-readable) and `report.html` (one self-contained file, findings sorted critical-first, with a client-side severity/detector filter bar, visible screenshots, occurrence counts, and a coverage section naming exactly which elements were never reached).

## The five personas

| Persona | What it does |
|---|---|
| curious | Breadth-first, maximizes state coverage |
| impatient | Double-clicks, spams submit, navigates away mid-request — hunts races and double-submits |
| malicious | Injection payloads, oversized/unicode input, cross-role probing — reports only, never exploits |
| power_user | Keyboard-only nav, back/forward, refresh mid-flow, pagination edges |
| accessibility | axe-core, keyboard traps, focus order, zoom/contrast |

Run one or several with `qaura run --personas curious,impatient,malicious`. Without `--personas` and without `--no-llm`, if a `GEMINI_API_KEY` is present QAura runs a single default persona; with no key at all it falls back to heuristic mode automatically (`llm_mode: auto` in config).

## ML testing (`qaura ml ...`)

Same "actually try to break it" philosophy, aimed at models instead of pages:

- `qaura ml test --model <file> --data <csv> [--slice-col group] [--baseline <file>]` — metrics, per-slice/subgroup performance, perturbation robustness, calibration, fairness gaps, and regression against a baseline model.
- `qaura ml probe --endpoint <url> --payload '{"x": 1}'` — hits a live inference endpoint with adversarial, malformed, empty, oversized, and wrong-type payloads; checks for crashes, schema violations, and nondeterminism on repeated identical input.
- `qaura ml genai --url <url> --input-ref <ref> --send-ref <ref> --response-selector <css>` — drives a chat-shaped AI feature in the browser, probing prompt injection, jailbreak, PII echo, and output-contract breakage.

## Command reference

```
qaura doctor                              # verify Gemini key + reachable models
qaura observe <url> [--role <name>]       # print the distilled page model, optionally authenticated
qaura auth capture --url <url> --role user   # save a login session for reuse

qaura run --url <url> --no-llm            # heuristic crawl, zero API spend
qaura run --url <url> --role admin        # crawl authenticated, using a captured session
qaura run --url <url> --personas curious,impatient
qaura run --url <url> --no-llm --ci --fail-on high        # CI gate, exit 1 on high+
qaura run --url <url> --no-analysis                       # skip dedupe/replay/localize/fix/repro
qaura run --url <url> --baseline-run runs/<prior>/report.json   # flag visually-changed recurring findings
qaura run --url <url> --trace             # also save a Playwright trace.zip per browser context (slower, opt-in)

qaura replay runs/<timestamp> [--role <name>]   # re-confirm reproducibility for a saved run
qaura report runs/<timestamp>             # regenerate HTML from a saved run's JSON

qaura ml test --model model.joblib --data eval.csv --slice-col group --baseline prior.joblib
qaura ml probe --endpoint http://localhost:8000/predict --payload '{"x": 1}'
qaura ml genai --url <url> --input-ref e1 --send-ref e2 --response-selector "#chat-response"
```

Authenticated runs matter for anything gated behind a login: `qaura run` without `--role` only ever sees what an anonymous visitor can, which on a login-gated app is just the login/register pages. Capture a session once with `qaura auth capture`, then pass `--role <name>` to `run`/`observe`/`replay`.

## Configuring invariants

Business-logic rules live in `qaura.yaml`, not code. Each invariant names a `container_selector`, a set of named `values` (CSS selectors, each resolving to one number or a list of numbers), and a restricted comparison `expression` evaluated against them (no arbitrary Python — see `core/invariants.py` for the exact allowed grammar). See `qaura.example.yaml` for the cart-subtotal/discount example.

## Configuring cross-role checks

Set `admin_paths` in `qaura.yaml` to a list of paths that should require a more-privileged session (e.g. `["/admin", "/admin/settings"]`). Mark the privileged role with `is_admin: true` under `auth_roles`. Then `qaura run --role <non-admin>` visits each of those paths under that session after the main crawl and reports a finding if nothing looks like it blocked access (no login prompt, no 403/forbidden text). Off by default — QAura can't infer which routes are privileged from the page alone.

## Where things live

```
qaura/
  cli.py        all commands
  config.py     qaura.yaml + .env loading
  llm/          Gemini provider, budget tracking, structured-output schemas
  browser/      Playwright driver, page observation, actions, auth, evidence recording
  core/         state graph, guardrails, heuristic crawler, persona orchestrator, invariants, forms
  personas/     the five persona prompts
  detectors/    crash, console, network, visual, a11y, security, performance, flow
  analysis/     dedupe, triage, localization, fix suggestions, replay, visual baselines
  reporting/    Finding/RunReport models, HTML report, repro-script generation
  mltest/       model registry + artifact/data/endpoint/genai suites
tests/fixtures/
  buggy_app/    deliberately buggy app used to prove every detector actually fires
  ml/           deliberately degraded model used to prove every ML check actually fires
```
