# How QAura works

Quick explainer of the pipeline, the moving parts, and how to read a report. For setup see `README.md`, for every flag of every command see `COMMANDS.md`, and for the full build history and design rationale see `docs/PROGRESS.md`.

## The idea

Point QAura at a running web app. It drives a real headless browser through the app, tries the kind of things a human tester would (clicking, filling forms, weird inputs, going back mid-request), and checks after every action whether something broke — a crash, a console error, a failed request, a visual defect, an accessibility violation, or a business-logic rule that got violated silently (like a cart total not updating after a discount). Anything it finds gets deduplicated, re-run a few times to confirm it's real and not a fluke, traced back to a likely source file, and written out as a self-contained HTML report plus a runnable pytest repro script.

A second half of the tool points the same idea at ML: trained model artifacts, live inference endpoints, and chat-shaped AI features.

## The full testing pipeline

This is the whole workflow, from a fresh checkout to QAura guarding every build. Each stage names the command that does it; "What each command does" further down describes every command in detail.

```
1. Set up         pip install, playwright install chromium, optional GEMINI_API_KEY in .env
2. Check          qaura doctor                          (only needed for LLM features)
3. Prepare        qaura auth capture  -> .qaura/auth/<role>.json         (login-gated apps)
                  qaura init          -> qaura.generated.yaml -> review -> qaura.yaml
                  qaura observe       -> confirm what the crawler will see
4. Explore        qaura run           -> runs/<timestamp>/  (report, screenshots, repro scripts)
5. Review         report.html, qaura replay, qaura report
6. Fix + retest   pytest runs/<timestamp>/repro          (fails while the bug exists, passes once fixed)
7. Guard          qaura run --ci --fail-on high           (CI gate)
                  qaura run --baseline-run <prior run>    (visual regressions)
                  qaura init --check -c qaura.yaml        (invariant selectors that stopped matching)

ML side           qaura ml test | ml data | ml probe | ml genai  -> runs/ml_*/
```

**1. Set up.** Install with `pip install -e ".[dev,ml]"` and `playwright install chromium`. A `GEMINI_API_KEY` in `.env` is optional. Without one, every command still works in heuristic mode at zero API cost; with one you also get persona exploration, triage, fix suggestions and invariant synthesis. Run QAura from the project folder, because `.env`, `qaura.yaml` and `.qaura/auth/` are all looked up relative to the current directory.

**2. Check the LLM setup.** `qaura doctor` confirms the key works and that every configured model tier is reachable and has quota. Skip it if you're only using `--no-llm`.

**3. Prepare.** If the app needs a login, capture a session per role with `qaura auth capture`, since QAura never scripts a login itself. Then run `qaura init` against the app. It crawls read-only and proposes a config: guardrails, personas, rate caps, admin paths and, with a key, business-rule invariants checked against real pages. Review `qaura.generated.yaml` and copy what you want into `qaura.yaml`, or pass it straight to `--config`. Make sure `guardrails.allowed_domains` lists every host the app redirects between; if it's left empty, `qaura run` limits itself to the `--url` host. Finally, `qaura observe <url> --role <name>` prints what the crawler will see, which is the quickest way to spot a session that didn't work (you'll see a login form).

**4. Explore.** `qaura run --url <url> --role <name> --no-llm` is the right first pass: deterministic, free, and it fills and submits every form it finds. Add `--personas curious,impatient,...` with a working key for LLM-driven exploration that also catches flows that silently do the wrong thing. Guardrails apply in both modes. Each run writes a timestamped folder under `runs/` (see the `qaura run` entry below for its contents), and analysis (dedupe, replay, repro scripts) runs automatically unless you pass `--no-analysis`.

**5. Review.** Open `report.html`. Findings are sorted critical-first, and each carries a reproducibility badge: confirmed, flaky, not reproduced, or not re-checked by replay. If the run stopped early (an error mid-crawl, a persona that failed), a note at the top says why, and everything found before that point is still in the report. Use `qaura replay` to re-check findings with more attempts or under a different role, and `qaura report` to rebuild the HTML after editing `report.json`.

**6. Fix and retest.** Every replayable finding gets a pytest file in `runs/<timestamp>/repro/`. With the app running, `pytest runs/<timestamp>/repro` replays each one: a test fails while its bug still reproduces and passes once the bug is fixed, so the folder doubles as a regression suite. Scripts for findings from a logged-in run replay under the same session file and skip if it's gone.

**7. Guard.** In CI, `qaura run --url <url> --no-llm --ci --fail-on high` exits 1 when any finding is at or above the chosen severity. `--baseline-run runs/<prior>/report.json` flags recurring findings whose screenshot changed noticeably since a known-good run. `qaura init --check -c qaura.yaml` exits 1 when an invariant's selectors no longer match the live site, which would otherwise make the rule silently stop protecting anything.

**ML side.** The `qaura ml` commands follow the same prepare, run, review, gate shape without a browser crawl (except `ml genai`): each writes a report under `runs/ml_*/` and exits 1 when its gate fails, so they drop into CI the same way.

## The one-run pipeline

This zooms into stage 4: what happens inside a single `qaura run`.

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

1. **Dedupe** (`analysis/dedupe.py`) — near-identical findings (same detector, same URL template, same normalized title) merge into one, keeping the most severe copy and counting how many times it was seen.
2. **Localize** — if `repo_path` is set in `qaura.yaml`, candidate source files are matched by searching the target app's repo for strings tied to the finding. Runs before triage on purpose, so an LLM-written triage note can't leak into localization's search terms.
3. **Triage** (needs a Gemini key, skipped with `--no-llm`) — an LLM pass on severity and false-positive likelihood.
4. **Replay** (`analysis/replay.py`) — every finding whose detector supports it (crash, console, network, security, invariant, visual, a11y, performance) gets re-executed against a fresh browser (`--replay-attempts`, default 2) and tagged with a real reproducibility status: **confirmed** (reproduced every attempt), **flaky** (some but not all), **not reproduced** (checked, didn't reproduce), or **not applicable** (this finding's detector — e.g. `flow`, which needs an LLM and isn't persisted — genuinely isn't replayable; shown as its own honest badge rather than a fabricated `0/N`).
5. **Fix** (needs a Gemini key, skipped with `--no-llm`) — a suggested fix per finding.
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

Run one or several with `qaura run --personas curious,impatient,malicious`. Without `--personas`, `qaura run` explores with the heuristic crawler even when a `GEMINI_API_KEY` is set, though the key is still used for triage and fix suggestions afterwards unless you pass `--no-llm`. With `--personas` but no working key (or `llm_mode: heuristic` in config), it prints a warning and falls back to heuristic mode rather than failing.

## ML testing (`qaura ml ...`)

Same "actually try to break it" philosophy, aimed at models instead of pages:

- `qaura ml test --model <file> --data <csv> [--slice-col group] [--baseline <file>]` — metrics, per-slice/subgroup performance, perturbation robustness, calibration, fairness gaps, and regression against a baseline model.
- `qaura ml data --reference <csv> --current <csv> [--label-col label]` — schema changes, feature drift and null-rate shift between two datasets, plus a leakage scan when a label column is given.
- `qaura ml probe --endpoint <url> --payload '{"x": 1}'` — hits a live inference endpoint with adversarial, malformed, empty, oversized, and wrong-type payloads; checks for crashes, schema violations, and nondeterminism on repeated identical input.
- `qaura ml genai --url <url> --input-ref <ref> --send-ref <ref> --response-selector <css>` — drives a chat-shaped AI feature in the browser, probing prompt injection, jailbreak, PII echo, and output-contract breakage.

## Generating a config (`qaura init`)

Writing `qaura.yaml` by hand means guessing selectors and guardrails for an app you may not know well. `qaura init --url <url>` does a bounded, read-only crawl first (`init/recon.py`): only GET and HEAD requests leave the browser, enforced by a route handler in `browser/readonly.py`, and destructive-looking paths are blocked even as GETs. From what it sees it infers guardrails, personas, rate caps and admin paths (`init/infer.py`). With a Gemini key it also proposes business-rule invariants from the numbers on the page (`init/candidates.py`), then validates each one against the HTML it captured before writing it.

The output is `qaura.generated.yaml`, never `qaura.yaml`. Every field notes how it was inferred and how confident that is, and low-confidence values are written commented out. `qaura init --check -c qaura.yaml` later reports whether an existing config's invariant selectors still match the live site. Flag details are in `COMMANDS.md`.

## Command reference

```
qaura doctor                              # verify Gemini key + reachable models
qaura init --url <url>                    # read-only crawl that proposes qaura.generated.yaml
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
qaura ml data --reference train.csv --current serving.csv [--label-col label]
qaura ml probe --endpoint http://localhost:8000/predict --payload '{"x": 1}'
qaura ml genai --url <url> --input-ref e1 --send-ref e2 --response-selector "#chat-response"
```

Authenticated runs matter for anything gated behind a login: `qaura run` without `--role` only ever sees what an anonymous visitor can, which on a login-gated app is just the login/register pages. Capture a session once with `qaura auth capture`, then pass `--role <name>` to `run`/`observe`/`replay`.

### What each command does

**`qaura doctor`** checks the LLM side before you spend anything on it. It confirms `GEMINI_API_KEY` is set (without printing it), lists the models your key can reach, makes one small live call per configured model tier to catch tiers with zero quota, and runs a final smoke test of the API call shape QAura depends on. It writes nothing. With no key it just tells you QAura will run in heuristic mode.

**`qaura init`** proposes a config for an app you haven't configured yet. It crawls up to `--max-pages` pages read-only (only GET and HEAD requests are allowed out, and logout or delete style paths are blocked even as GETs), infers guardrails, personas, rate caps and admin paths, and with a key proposes invariants that it validates against the pages it captured. It writes only `qaura.generated.yaml` (or `--out`), never `qaura.yaml`, and stops with exit 3 on a bot-protection challenge. With `--check` it instead reports whether an existing config's invariant selectors still match the site.

**`qaura observe <url>`** loads one page and prints its `PageModel`: every interactive element with its ref (`e3`), role, name, value and state. This is exactly what the crawler and personas act on. Use it to confirm a captured session works, or to find the refs `qaura ml genai` needs. It writes nothing.

**`qaura auth capture`** opens a visible browser at `--url`, waits while you log in by hand (MFA and SSO included), and saves the session to `.qaura/auth/<role>.json` when you press Enter. Every other command reuses it through `--role <name>`. Role names are limited to letters, digits, `_` and `-`.

**`qaura auth list`** prints the roles that have a captured session.

**`qaura run`** is the main test run. It drives a real browser through the target with the heuristic crawler (or LLM personas with `--personas`), checks detectors and invariants after every action, then runs analysis: dedupe, triage and fix suggestions when a key is set and `--no-llm` isn't, replay, source localization when `repo_path` is set, and repro-script generation. It writes `runs/<timestamp>/` containing `report.html`, `report.json`, `screenshots/`, `repro/` (one pytest file per replayable finding) and, with `--trace`, `traces/`. With `--ci` it exits 1 if any finding meets `--fail-on`. A bad `--fail-on` value or role name exits 2 before a browser starts.

**`qaura replay <run>`** re-executes the findings in a saved run against a fresh browser (every finding whose detector replay supports, or one with `--finding-id`) and updates each finding's reproducibility in `report.json` and `report.html` in place. Pass `--role` if the original run was logged in, or the replay will see the anonymous site.

**`qaura report <run>`** rebuilds `report.html` from a saved `report.json` without touching a browser, for example after hand-editing a finding. Screenshots are embedded only from the run's own folder.

**`qaura ml test`** tests a trained model file against a labelled CSV: overall metrics, per-slice performance (`--slice-col`), robustness to small perturbations, calibration, fairness gaps, and regression against `--baseline`. Model files are Python pickles, so only point it at files you trust. Writes `runs/ml_<timestamp>/`.

**`qaura ml data`** compares two datasets, typically training data against what the model sees in production: schema changes, per-column drift and null-rate shifts, plus a leakage scan for features suspiciously correlated with `--label-col`. Writes `runs/ml_data_<timestamp>/`.

**`qaura ml probe`** sends a live inference endpoint a known-good `--payload` plus malformed, empty, oversized and wrong-type variants of it, and checks for crashes, schema violations, slow responses and different answers to identical input. Writes `runs/ml_endpoint_<timestamp>/`.

**`qaura ml genai`** drives a chat feature inside the app through the browser, using the input and send-button refs from `qaura observe`, and probes it for prompt injection, jailbreaks, PII echo and broken output format. Writes `runs/ml_genai_<timestamp>/`.

All four `ml` commands exit 1 when their overall gate is FAIL.

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
